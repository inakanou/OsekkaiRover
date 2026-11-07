"""見る流れ（撮る → 見つける → 追従 → 認識 → あいさつ、カメラの貸し出し）。

1.1、1.4、2.1、2.2、2.5、3.8、6.3、7.2〜7.7。
偽のカメラと偽の映像の処理と偽の時計で、1 回ずつ回して確かめる。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from presence_fakes import FakeCamera, FakeVision, embedding

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.storage import StateStore
from osekkai.core.types import Face
from osekkai.features.presence import texts
from osekkai.features.presence.config import PresenceConfig
from osekkai.features.presence.faces import FaceBook, Person
from osekkai.features.presence.greeting import UNKNOWN, GreetingPolicy, GreetingRecords
from osekkai.features.presence.ports import CameraUnavailable
from osekkai.features.presence.tracking import FaceBox, Tracker
from osekkai.features.presence.watcher import (
    GREET_RETRY_SECONDS,
    CameraBusy,
    CameraNotAvailable,
    CameraNotBorrowed,
    PresenceIO,
    Watcher,
)

JST = timezone(timedelta(hours=9), "JST")
MORNING = datetime(2026, 3, 10, 9, 0, tzinfo=JST)  # 時間帯の中で、朝
NOON = datetime(2026, 3, 10, 13, 0, tzinfo=JST)  # 時間帯の中で、朝でない
NIGHT = datetime(2026, 3, 10, 22, 0, tzinfo=JST)  # 時間帯の外
STEP = 0.1  # 回りの間（秒）
# ログに出てはいけない、目立つ名前と呼び名
HIDDEN_ID = "zzhiddenid"
HIDDEN_NAME = "ひみつのよびな"
FACE = FaceBox(0.5, 0.0, 0.3)
EMB = embedding(1)
OTHER_EMB = embedding(2)


@dataclass
class FakeIO:
    """首・目・あいさつの口の偽物。呼ばれた順に残す。"""

    greet_ok: bool = True
    looks: list[tuple[float, float]] = field(default_factory=list)
    idle_faces: list[tuple[Face, float]] = field(default_factory=list)
    greets: list[tuple[str, str, str]] = field(default_factory=list)

    def look(self, pan: float, tilt: float) -> None:
        self.looks.append((pan, tilt))

    def set_idle_face(self, face: Face, gaze: float) -> None:
        self.idle_faces.append((face, gaze))

    def greet(self, lcd_line1: str, face_text: str, speech: str) -> bool:
        self.greets.append((lcd_line1, face_text, speech))
        return self.greet_ok


def _first(options: object) -> str:
    assert isinstance(options, tuple)
    value = options[0]
    assert isinstance(value, str)
    return value


def _always(face: FaceBox | None, count: int = 2000) -> dict[int, tuple[FaceBox, ...]]:
    return {i: (() if face is None else (face,)) for i in range(count)}


@dataclass
class Rig:
    watcher: Watcher
    camera: FakeCamera
    vision: FakeVision
    io: FakeIO
    clock: FakeClock
    records: GreetingRecords
    book: FaceBook

    def run(self, seconds: float) -> None:
        """`seconds` 秒の間、回りを回す（回りごとに時計を進める）。"""
        for _ in range(round(seconds / STEP)):
            self.watcher.run_once()
            self.clock.advance(STEP)


def _rig(
    tmp_path: Path,
    *,
    vision: FakeVision | None = None,
    camera: FakeCamera | None = None,
    people: tuple[Person, ...] = (Person(HIDDEN_ID, HIDDEN_NAME, (EMB,)),),
    start: datetime = MORNING,
    synced: bool = True,
    greet_ok: bool = True,
    no_camera: bool = False,
) -> Rig:
    clock = FakeClock(start, synchronized=synced)
    store = StateStore(tmp_path / "presence", clock)
    config = PresenceConfig()
    book = FaceBook(store, people)
    records = GreetingRecords(store)
    camera = camera or FakeCamera()
    vision = vision or FakeVision()
    io = FakeIO(greet_ok=greet_ok)
    io_port: PresenceIO = io
    watcher = Watcher(
        None if no_camera else camera,
        None if no_camera else vision,
        book,
        records,
        GreetingPolicy(config, choose=_first),
        Tracker(),
        config,
        clock,
        io_port,
        logging.getLogger("test.presence.watcher"),
    )
    return Rig(watcher, camera, vision, io, clock, records, book)


def _seen_vision(emb: object = EMB) -> FakeVision:
    embeddings = {i: emb for i in range(2000)}
    return FakeVision(faces=_always(FACE), embeddings=embeddings)  # type: ignore[arg-type]


def _alive_flag() -> tuple[list[bool], Callable[[], bool]]:
    flag = [True]
    return flag, lambda: flag[0]


# --- 見つけて向く（1.1、1.4） ---


def test_顔を見つけると首と待機画面の目がその方へ向く(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=FakeVision(faces=_always(FACE)), people=())
    rig.run(0.5)
    assert rig.camera.started
    assert rig.vision.detect_calls[:5] == [0, 1, 2, 3, 4]
    # 2 回目で採って、右（+）へ少しずつ向く
    pans = [pan for pan, _ in rig.io.looks]
    assert pans[-1] > 0.0
    assert pans == sorted(pans)
    face, gaze = rig.io.idle_faces[-1]
    assert face is Face.HAPPY
    assert gaze > 0.0


def test_顔を見失うと待機画面はふつうの顔に戻る(tmp_path: Path) -> None:
    faces = {i: ((FACE,) if i < 5 else ()) for i in range(200)}
    rig = _rig(tmp_path, vision=FakeVision(faces=faces), people=())
    rig.run(0.5)
    assert rig.io.idle_faces[-1][0] is Face.HAPPY
    rig.run(3.0)
    assert rig.io.idle_faces[-1] == (Face.NORMAL, 0.0)


def test_同じ首の角度と目は送り直さない(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=FakeVision(faces=_always(None)), people=())
    rig.run(1.0)
    assert rig.io.looks == [(0.0, 0.0)]
    assert rig.io.idle_faces == [(Face.NORMAL, 0.0)]


# --- 認識とあいさつ（2.1、2.2、2.5、3.8） ---


def test_認識は1秒に1回まで(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision(), greet_ok=False)
    rig.run(3.0)
    assert 3 <= len(rig.vision.embed_calls) <= 4


def test_2回同じ人と分かってから名前で朝のあいさつをする(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision())
    rig.run(0.9)
    assert rig.io.greets == []  # 1 票目だけではまだ決まらない
    rig.run(1.5)
    expected = texts.fill(texts.MORNING_KNOWN, HIDDEN_NAME)
    assert rig.io.greets == [(texts.LCD_FOUND, Face.HAPPY.text(), expected)]
    assert rig.records.morning_done(HIDDEN_ID, MORNING.date())
    rig.run(5.0)
    assert len(rig.io.greets) == 1  # 見続けても、もう一度はあいさつしない


def test_知らない人は3秒待ってから呼び名なしであいさつする(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision(OTHER_EMB))
    rig.run(3.0)
    assert rig.io.greets == []
    rig.run(1.5)
    assert [g[2] for g in rig.io.greets] == [texts.MORNING_UNKNOWN]
    assert rig.records.morning_done(UNKNOWN, MORNING.date())
    assert not rig.records.morning_done(HIDDEN_ID, MORNING.date())


def test_認識のモデルがなければ知らない人としてあいさつする(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision(None))
    rig.run(4.5)
    assert [g[2] for g in rig.io.greets] == [texts.MORNING_UNKNOWN]


def test_覚えた人がいなければ待たずに1秒であいさつする(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision(), people=())
    rig.run(1.5)
    assert [g[2] for g in rig.io.greets] == [texts.MORNING_UNKNOWN]
    assert rig.vision.embed_calls == []  # 比べる人がいなければ特徴にしない


def test_声と画面を借りられなければあいさつも記録もしない(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision(), greet_ok=False)
    rig.run(2.5)
    assert len(rig.io.greets) == 1
    assert rig.records.last_seen(HIDDEN_ID) is None
    assert not rig.records.morning_done(HIDDEN_ID, MORNING.date())
    # 少し待って、借りられたらあいさつする
    rig.io.greet_ok = True
    rig.run(GREET_RETRY_SECONDS + 0.5)
    assert len(rig.io.greets) == 2
    assert rig.records.morning_done(HIDDEN_ID, MORNING.date())


def test_見えなかった時間が短ければあいさつしない_長ければもう一度(tmp_path: Path) -> None:
    faces = {i: ((FACE,) if i < 30 else ()) for i in range(200)}
    embeddings = {i: EMB for i in range(200)}
    vision = FakeVision(faces=faces, embeddings=embeddings)
    rig = _rig(tmp_path, vision=vision, start=NOON)
    rig.run(3.0)
    assert [g[2] for g in rig.io.greets] == [texts.fill(texts.OKAERI_KNOWN, HIDDEN_NAME)]
    rig.run(5.0)  # 見失う
    # 25 分後に、また見つける
    rig.clock.advance(25 * 60)
    rig.vision.faces = _always(FACE, 2000)
    rig.vision.embeddings = {i: EMB for i in range(2000)}
    rig.run(3.0)
    assert [g[2] for g in rig.io.greets][1:] == [texts.fill(texts.HELLO_KNOWN[0], HIDDEN_NAME)]


def test_ずっと見えていた人が10分離れて戻ってもあいさつしない(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision(), start=NOON)
    rig.run(30 * 60.0)  # 30 分見え続ける（あいさつは 1 回だけ）
    assert len(rig.io.greets) == 1
    rig.vision.faces = {}
    rig.run(5.0)  # 見失う
    rig.clock.advance(10 * 60)
    rig.vision.faces = _always(FACE, 4000)
    rig.vision.embeddings = {i: EMB for i in range(4000)}
    rig.run(5.0)
    assert len(rig.io.greets) == 1


def test_知らない人としてあいさつした後に誰か分かっても_もう一度あいさつしない(
    tmp_path: Path,
) -> None:
    # 初めの 4 秒は特徴がなく（知らない人）、その後に覚えた人と分かる
    faces = _always(FACE, 2000)
    embeddings = {i: EMB for i in range(40, 2000)}
    vision = FakeVision(faces=faces, embeddings=embeddings)
    rig = _rig(tmp_path, vision=vision, start=NOON)
    rig.run(10.0)
    assert [g[2] for g in rig.io.greets] == [texts.OKAERI_UNKNOWN]


def test_時刻が確かでない間はあいさつせず_見た記録も残さない(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision(), synced=False)
    rig.run(5.0)
    assert rig.io.greets == []
    assert rig.records.last_seen(HIDDEN_ID) is None
    rig.clock.set_synchronized(True)
    rig.run(1.0)
    assert len(rig.io.greets) == 1


def test_時間帯の外ではあいさつしないが_見た記録は残す(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision(), start=NIGHT)
    rig.run(5.0)
    assert rig.io.greets == []
    assert rig.records.last_seen(HIDDEN_ID) is not None


# --- 止める（静かな状態・中断） ---


def test_止めるとカメラを止めて待機画面をふつうに戻す(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision(), greet_ok=False)
    rig.run(1.0)
    rig.watcher.pause()
    assert not rig.camera.started
    assert rig.camera.stop_count == 1
    assert rig.io.idle_faces[-1] == (Face.NORMAL, 0.0)
    rig.watcher.pause()  # 止まっていれば何もしない
    assert rig.camera.stop_count == 1
    rig.run(0.1)
    assert rig.camera.started  # 回すとまた始める


# --- カメラの貸し出し（7.2〜7.7） ---


def test_貸し出しの間は見ず_首も動かさず_あいさつしない(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision())
    _, alive = _alive_flag()
    rig.watcher.start_loan("daruma", alive)
    assert rig.watcher.loaned
    rig.run(5.0)
    assert rig.vision.detect_calls == []
    assert rig.vision.embed_calls == []
    assert rig.io.looks == []
    assert rig.io.greets == []


def test_貸し出しが始まると見ていた状態を戻す(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision(), greet_ok=False)
    rig.run(1.0)
    assert rig.io.idle_faces[-1][0] is Face.HAPPY
    _, alive = _alive_flag()
    rig.watcher.start_loan("daruma", alive)
    rig.run(0.1)
    assert rig.io.idle_faces[-1] == (Face.NORMAL, 0.0)


def test_2つ目の借り手は断る(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _, alive = _alive_flag()
    rig.watcher.start_loan("daruma", alive)
    with pytest.raises(CameraBusy):
        rig.watcher.start_loan("remote", alive)


def test_返すとまた見る_返した後の要求は断る(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision(), greet_ok=False)
    _, alive = _alive_flag()
    lent = rig.watcher.start_loan("daruma", alive)
    rig.run(0.5)
    rig.watcher.end_loan(lent)
    rig.watcher.end_loan(lent)  # 2 回返してもよい
    assert not rig.watcher.loaned
    rig.run(0.5)
    assert rig.vision.detect_calls != []
    with pytest.raises(CameraNotBorrowed):
        rig.watcher.request_still(lent)
    # 次の機能は借りられる
    rig.watcher.start_loan("remote", alive)


def test_止まった借り手の貸し出しは終わる(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision(), greet_ok=False)
    flag, alive = _alive_flag()
    lent = rig.watcher.start_loan("daruma", alive)
    ticket = rig.watcher.request_still(lent)
    flag[0] = False
    rig.run(0.1)
    assert not rig.watcher.loaned
    with pytest.raises(CameraNotBorrowed):
        ticket.wait(0.0)  # 止まった借り手の要求は行わない
    assert rig.camera.still_count == 0
    with pytest.raises(CameraNotBorrowed):
        rig.watcher.request_frame(lent)
    rig.run(0.5)
    assert rig.vision.detect_calls != []


def test_閉じると残った要求にすぐ使えないと知らせ_カメラを止める(tmp_path: Path) -> None:
    rig = _rig(tmp_path, vision=_seen_vision(), greet_ok=False)
    _, alive = _alive_flag()
    rig.run(0.2)  # カメラを始める
    lent = rig.watcher.start_loan("daruma", alive)
    ticket = rig.watcher.request_still(lent)
    rig.watcher.close()
    with pytest.raises(CameraNotAvailable):
        ticket.wait(0.0)  # 時間切れを待たずに分かる
    assert not rig.watcher.loaned
    assert not rig.camera.started
    with pytest.raises(CameraNotAvailable):
        rig.watcher.request_frame(lent)
    with pytest.raises(CameraNotAvailable):
        rig.watcher.start_loan("remote", alive)
    rig.watcher.close()  # 2 回閉じてもよい


def test_止まった借り手があれば次の借り手に貸す(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    flag, alive = _alive_flag()
    rig.watcher.start_loan("daruma", alive)
    flag[0] = False
    _, alive2 = _alive_flag()
    rig.watcher.start_loan("remote", alive2)
    assert rig.watcher.loaned


def test_借り手が動いているかを確かめられなければ止まったとする(tmp_path: Path) -> None:
    rig = _rig(tmp_path)

    def broken() -> bool:
        raise RuntimeError

    rig.watcher.start_loan("daruma", broken)
    rig.run(0.1)
    assert not rig.watcher.loaned


def test_借りた機能に静止画を返す(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _, alive = _alive_flag()
    lent = rig.watcher.start_loan("daruma", alive)
    ticket = rig.watcher.request_still(lent)
    assert not ticket.done
    rig.watcher.run_once()
    assert ticket.wait(0.0) == b"still:0"


def test_借りた機能にフレームを幅と品質を当ててJPEGで返す(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _, alive = _alive_flag()
    lent = rig.watcher.start_loan("daruma", alive)
    ticket = rig.watcher.request_frame(lent, max_width=320, quality=50)
    rig.watcher.run_once()
    assert ticket.wait(0.0) == b"jpeg:0:320:50"
    assert rig.vision.jpeg_calls == [(0, 320, 50)]
    assert rig.vision.detect_calls == []


def test_借りた機能に動きの量を返す(tmp_path: Path) -> None:
    # 直近の 2 つの小さい方の最大（1 回だけの大きな値は数えない）
    vision = FakeVision(motions=(0.1, 0.9, 0.2, 0.5, 0.6, 0.3))
    rig = _rig(tmp_path, vision=vision)
    _, alive = _alive_flag()
    lent = rig.watcher.start_loan("daruma", alive)
    start = rig.clock.monotonic()
    ticket = rig.watcher.request_motion(lent, 0.5)
    rig.watcher.run_once()
    assert ticket.wait(0.0) == pytest.approx(0.5)
    assert rig.clock.monotonic() - start == pytest.approx(0.5, abs=0.1)
    calls = rig.vision.motion_calls
    assert 5 <= len(calls) <= 7
    # 続いた 2 枚ずつを比べる
    assert all(after == before + 1 for before, after in calls)


def test_動きの量とフレームの値を確かめる(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _, alive = _alive_flag()
    lent = rig.watcher.start_loan("daruma", alive)
    with pytest.raises(ValueError):
        rig.watcher.request_motion(lent, 0.0)
    with pytest.raises(ValueError):
        rig.watcher.request_motion(lent, float("nan"))
    with pytest.raises(ValueError):
        rig.watcher.request_frame(lent, max_width=0)
    with pytest.raises(ValueError):
        rig.watcher.request_frame(lent, quality=101)


def test_ほかのスレッドの要求を見るスレッドが行う(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _, alive = _alive_flag()
    lent = rig.watcher.start_loan("daruma", alive)
    results: list[bytes] = []

    def borrower() -> None:
        results.append(rig.watcher.request_still(lent).wait(5.0))

    thread = threading.Thread(target=borrower)
    thread.start()
    for _ in range(500):
        if results:
            break
        rig.watcher.run_once()
        threading.Event().wait(0.01)
    thread.join(5.0)
    assert results == [b"still:0"]


def test_行われない要求は時間切れになる(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _, alive = _alive_flag()
    lent = rig.watcher.start_loan("daruma", alive)
    with pytest.raises(TimeoutError):
        rig.watcher.request_still(lent).wait(0.01)


# --- カメラが使えない（7.8、8.5） ---


def test_カメラがなければ何もせず_貸し出しもしない(tmp_path: Path) -> None:
    rig = _rig(tmp_path, no_camera=True)
    rig.run(1.0)
    assert not rig.watcher.available
    assert rig.io.looks == []
    assert rig.io.idle_faces == []
    _, alive = _alive_flag()
    with pytest.raises(CameraNotAvailable):
        rig.watcher.start_loan("daruma", alive)


def test_カメラが使えなくなると見るのを止め_要求も断る(tmp_path: Path) -> None:
    rig = _rig(tmp_path, camera=FakeCamera(unavailable=True))
    with pytest.raises(CameraUnavailable):
        rig.watcher.run_once()
    assert not rig.watcher.available
    rig.watcher.run_once()  # 次の回りは何もしない
    _, alive = _alive_flag()
    with pytest.raises(CameraNotAvailable):
        rig.watcher.start_loan("daruma", alive)


def test_貸し出しの間にカメラが使えなくなると要求に知らせる(tmp_path: Path) -> None:
    camera = FakeCamera()
    rig = _rig(tmp_path, camera=camera)
    _, alive = _alive_flag()
    lent = rig.watcher.start_loan("daruma", alive)
    ticket = rig.watcher.request_still(lent)
    camera.unavailable = True
    with pytest.raises(CameraUnavailable):
        rig.watcher.run_once()
    with pytest.raises(CameraNotAvailable):
        ticket.wait(0.0)
    assert not rig.watcher.loaned


# --- ログ（6.3） ---


def test_ログに名前と呼び名とあいさつの言葉を出さない(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = _rig(tmp_path, vision=_seen_vision(), greet_ok=False)
    rig.run(2.5)
    rig.io.greet_ok = True
    rig.run(GREET_RETRY_SECONDS + 1.0)
    _, alive = _alive_flag()
    lent = rig.watcher.start_loan("daruma", alive)
    rig.watcher.request_still(lent)
    rig.run(0.2)
    rig.watcher.end_loan(lent)
    assert rig.io.greets
    text = caplog.text
    assert caplog.records  # 何かは記録している
    for hidden in (HIDDEN_ID, HIDDEN_NAME, *(g[2] for g in rig.io.greets), texts.LCD_FOUND):
        assert hidden not in text
