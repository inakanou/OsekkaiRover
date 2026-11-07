"""カメラを借りる口（7.1、7.2、7.6、7.7、7.8）。

本物の見る流れを偽のカメラと偽の映像で作り、置き場に登録して、見るスレッドで回して確かめる。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from presence_fakes import FakeCamera, FakeVision

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.storage import StateStore
from osekkai.core.types import Face
from osekkai.features.presence import api, watcher
from osekkai.features.presence.api import (
    CameraBusy,
    CameraLoan,
    CameraNotAvailable,
    CameraNotBorrowed,
    borrow_camera,
)
from osekkai.features.presence.config import PresenceConfig
from osekkai.features.presence.faces import FaceBook
from osekkai.features.presence.greeting import GreetingPolicy, GreetingRecords
from osekkai.features.presence.ports import CameraUnavailable
from osekkai.features.presence.tracking import Tracker
from osekkai.features.presence.watcher import PresenceIO, Watcher

JST = timezone(timedelta(hours=9), "JST")
MORNING = datetime(2026, 3, 10, 9, 0, tzinfo=JST)


class _IO:
    def look(self, pan: float, tilt: float) -> None:
        pass

    def set_idle_face(self, face: Face, gaze: float) -> None:
        pass

    def greet(self, lcd_line1: str, face_text: str, speech: str) -> bool:
        return True


@dataclass
class Rig:
    watcher: Watcher
    camera: FakeCamera
    vision: FakeVision
    stop: threading.Event
    thread: threading.Thread | None = None

    def start(self) -> None:
        """見るスレッドを始める（`run_once` を回し続ける）。"""

        def loop() -> None:
            while not self.stop.is_set():
                try:
                    self.watcher.run_once()
                except CameraUnavailable:
                    return  # 機能の本体と同じく、見ることを止める
                self.stop.wait(0.005)

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()


def _make_watcher(
    tmp_path: Path, *, no_camera: bool = False, vision: FakeVision | None = None
) -> tuple[Watcher, FakeCamera, FakeVision]:
    clock = FakeClock(MORNING, synchronized=True)
    store = StateStore(tmp_path / "presence", clock)
    config = PresenceConfig()
    camera = FakeCamera()
    vision = vision or FakeVision()
    io: PresenceIO = _IO()
    made = Watcher(
        None if no_camera else camera,
        None if no_camera else vision,
        FaceBook(store, ()),
        GreetingRecords(store),
        GreetingPolicy(config, choose=lambda options: str(options[0])),
        Tracker(),
        config,
        clock,
        io,
        logging.getLogger("test.presence.api"),
    )
    return made, camera, vision


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    rigs: list[Rig] = []

    def make(*, run: bool = True, no_camera: bool = False, vision: FakeVision | None = None) -> Rig:
        made, camera, fake_vision = _make_watcher(tmp_path, no_camera=no_camera, vision=vision)
        rig = Rig(made, camera, fake_vision, threading.Event())
        api.register_watcher(made)
        if run:
            rig.start()
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.stop.set()
        if rig.thread is not None:
            rig.thread.join(5.0)
        api.clear_watcher(rig.watcher)


def _alive_flag() -> tuple[list[bool], Callable[[], bool]]:
    flag = [True]
    return flag, lambda: flag[0]


def _wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    waiter = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if condition():
            return True
        waiter.wait(0.01)
    return condition()


# --- 借りる・使う・返す（7.1） ---


def test_借りると動きの量とフレームと静止画が返る(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(vision=FakeVision(motions=(0.4,)))
    _, alive = _alive_flag()
    loan = borrow_camera("daruma", alive=alive)
    assert isinstance(loan, CameraLoan)
    assert rig.watcher.loaned
    assert loan.motion(0.5) == 0.4
    frame = loan.frame(max_width=320, quality=50)
    assert frame.startswith(b"jpeg:") and frame.endswith(b":320:50")
    assert loan.still() == b"still:0"
    loan.release()
    assert not rig.watcher.loaned


def test_フレームの既定の幅と品質(make_rig: Callable[..., Rig]) -> None:
    make_rig()
    _, alive = _alive_flag()
    loan = borrow_camera("daruma", alive=alive)
    assert loan.frame().endswith(b":640:70")
    loan.release()


def test_値の誤りはそのまま知らせる(make_rig: Callable[..., Rig]) -> None:
    make_rig()
    _, alive = _alive_flag()
    loan = borrow_camera("daruma", alive=alive)
    with pytest.raises(ValueError):
        loan.motion(0.0)
    with pytest.raises(ValueError):
        loan.frame(max_width=0)
    loan.release()


def test_2つ目の借り手は断る(make_rig: Callable[..., Rig]) -> None:
    make_rig()
    _, alive = _alive_flag()
    loan = borrow_camera("daruma", alive=alive)
    with pytest.raises(CameraBusy):
        borrow_camera("remote", alive=alive)
    loan.release()
    second = borrow_camera("remote", alive=alive)
    assert second.still().startswith(b"still:")
    second.release()


# --- 借りていない機能は断る（7.6） ---


def test_返した後の要求は断る_2回返してもよい(make_rig: Callable[..., Rig]) -> None:
    make_rig()
    _, alive = _alive_flag()
    loan = borrow_camera("daruma", alive=alive)
    loan.release()
    loan.release()
    with pytest.raises(CameraNotBorrowed):
        loan.motion(0.5)
    with pytest.raises(CameraNotBorrowed):
        loan.frame()
    with pytest.raises(CameraNotBorrowed):
        loan.still()


def test_with_の後は返される(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    _, alive = _alive_flag()
    with borrow_camera("daruma", alive=alive) as loan:
        assert loan.still().startswith(b"still:")
        inside = rig.watcher.loaned
    assert inside
    assert not rig.watcher.loaned
    with pytest.raises(CameraNotBorrowed):
        loan.still()


def test_with_の中で例外が出ても返される(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    _, alive = _alive_flag()
    with pytest.raises(RuntimeError), borrow_camera("daruma", alive=alive):
        raise RuntimeError
    assert not rig.watcher.loaned


# --- 止まった借り手（7.7） ---


def test_借り手が止まると要求は断り_貸し出しが終わる(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    flag, alive = _alive_flag()
    loan = borrow_camera("daruma", alive=alive)
    flag[0] = False
    with pytest.raises(CameraNotBorrowed):
        loan.still()
    assert not rig.watcher.loaned
    # 次の借り手が借りられる
    _, alive2 = _alive_flag()
    second = borrow_camera("remote", alive=alive2)
    second.release()


def test_借り手が止まると見るスレッドも貸し出しを終える(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    flag, alive = _alive_flag()
    borrow_camera("daruma", alive=alive)
    flag[0] = False
    assert _wait_for(lambda: not rig.watcher.loaned)


# --- 使えないとき（7.8） ---


def test_presence_が動いていなければ使えない() -> None:
    assert api.running_watcher() is None
    _, alive = _alive_flag()
    with pytest.raises(CameraNotAvailable):
        borrow_camera("daruma", alive=alive)


def test_カメラがなければ使えない(make_rig: Callable[..., Rig]) -> None:
    make_rig(no_camera=True)
    _, alive = _alive_flag()
    with pytest.raises(CameraNotAvailable):
        borrow_camera("daruma", alive=alive)


def test_貸し出しの間に_presence_が止まると使えない(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    _, alive = _alive_flag()
    loan = borrow_camera("daruma", alive=alive)
    api.clear_watcher(rig.watcher)
    with pytest.raises(CameraNotAvailable):
        loan.still()
    loan.release()  # 返してもよい


def test_貸し出しの間にカメラが使えなくなると使えない(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    _, alive = _alive_flag()
    loan = borrow_camera("daruma", alive=alive)
    rig.camera.unavailable = True
    with pytest.raises(CameraNotAvailable):
        loan.still()
    with pytest.raises(CameraNotAvailable):
        loan.frame()


# --- 時間切れ ---


def test_行われない要求は時間切れになる(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    make_rig(run=False)  # 見るスレッドが回っていない
    monkeypatch.setattr(api, "REQUEST_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(api, "MOTION_MARGIN_SECONDS", 0.01)
    _, alive = _alive_flag()
    loan = borrow_camera("daruma", alive=alive)
    with pytest.raises(TimeoutError) as error:
        loan.still()
    assert str(error.value) == watcher.MSG_TIMEOUT
    with pytest.raises(TimeoutError):
        loan.frame()
    with pytest.raises(TimeoutError):
        loan.motion(0.01)
    loan.release()


def test_待つ秒数は動きの量の秒数より長い() -> None:
    assert api.MOTION_MARGIN_SECONDS >= 1.0
    assert api.REQUEST_TIMEOUT_SECONDS >= 1.0


def test_例外は_watcher_と同じもの() -> None:
    assert CameraBusy is watcher.CameraBusy
    assert CameraNotAvailable is watcher.CameraNotAvailable
    assert CameraNotBorrowed is watcher.CameraNotBorrowed
