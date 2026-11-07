"""見つけてあいさつを通して確かめる（3.3、3.4、3.7、4.2、4.4、6.2、6.3、7.2、9.4）。

偽のカメラと偽の映像の処理と、本物の基盤の部品（調停役・静かな状態・待機画面・保存先）で、
1 つの流れを通す。

1. 朝、知らない人を見つけると、呼び名なしでおはよう
2. 続けて子ども（覚えた人）を見つけると、名前でおはよう（知らない人の朝は子どもの朝を使わない）
3. 20 分の間に見つけ直しても、あいさつしない
4. 120 分の後に見つけると、おかえり
5. 日課（優先度 SCHEDULED）が首を求めると、見るのをやめて返す。その間は見ない
6. おやすみ（静かな状態）の間は、カメラを止めて見ない
7. ほかの機能がカメラを借りている間は、見ない・あいさつしない

機能は別のスレッドで動かす。偽の時計を少しずつ進め、そのたびに新しいフレームを
2 枚撮るまで待つ（進めた時刻を見る流れが必ず見る）。実際の時間で待つのは短い間だけ。
流れ全体のログを集め、名前・呼び名・あいさつの言葉が出ていないことを確かめる。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from presence_fakes import FakeCamera, FakeVision, embedding

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import JST, FakeClock
from osekkai.core.arbiter import Arbiter, Lease
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.config import MotionSettings, VoiceSettings
from osekkai.core.display import DisplayService, LedService
from osekkai.core.events import EventBus
from osekkai.core.feature import CoreFeatureContext
from osekkai.core.idle import IdleScreen
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.quiet import QuietMode
from osekkai.core.storage import StateStore
from osekkai.core.types import Priority, Resource
from osekkai.core.voice import VoiceService
from osekkai.features.presence import PresenceFeature, api, texts
from osekkai.features.presence.config import PresenceConfig
from osekkai.features.presence.faces import Embedding, FaceBook, Person
from osekkai.features.presence.ports import Frame
from osekkai.features.presence.tracking import FaceBox

WAIT_S = 5.0
LOOP_S = 0.005  # 機能の回りの間（実際の秒数）
RETRY_S = 0.02  # 首を借り直す間（実際の秒数）
STEP_S = 0.25  # 偽の時計を 1 回に進める秒数
QUIET_WAIT_S = 0.2  # 止まっていることを確かめる実際の秒数
FACE = FaceBox(0.5, 0.0, 0.3)
CHILD_ID = "child"
CHILD_NAME = "テストのよびな"
CHILD_EMB = embedding(1)
STRANGER_EMB = embedding(99)  # 覚えた人のどれとも似ていない
ROUTINE = "routine"
BORROWER = "tester"
START = datetime(2026, 1, 1, 7, 30, tzinfo=JST)
MINUTE = 60.0


def wait_for(condition: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.002)
    return condition()


class SceneVision(FakeVision):
    """偽の映像の処理。フレームの番号ではなく、いま写っている人（台本）で答える。

    face が None なら顔なし。emb は認識の特徴（写っている人ごと）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.face: FaceBox | None = None
        self.emb: Embedding | None = None

    def show(self, emb: Embedding | None) -> None:
        self.emb = emb
        self.face = None if emb is None else FACE

    def detect(self, frame: Frame) -> tuple[FaceBox, ...]:
        super().detect(frame)  # 呼ばれた回数を数える
        face = self.face
        return () if face is None else (face,)

    def embed(self, frame: Frame, face: FaceBox) -> Embedding | None:
        super().embed(frame, face)
        return self.emb


class RecordingVoice(VoiceService):
    """本物の声の部品。話した文章を、句に分ける前のまま残す（同じ句はキャッシュで合成を省くため）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.spoken: list[str] = []

    def speak(
        self,
        text: str,
        *,
        keep: bool = True,
        should_continue: Callable[[], bool] | None = None,
    ) -> bool:
        self.spoken.append(text)
        return super().speak(text, keep=keep, should_continue=should_continue)


class Journey:
    """基盤の部品を偽のデバイスで組み、見つけてあいさつの機能を 1 つ動かす一式。"""

    def __init__(self, fakes: FakeDeviceSet, clock: FakeClock) -> None:
        self.fakes = fakes
        self.clock = clock
        self.paths = Paths()
        self.router = ButtonRouter(fakes.buttons, clock)
        self.cache = CacheManager.from_paths(self.paths, 10, clock)
        self.voice = RecordingVoice(
            fakes.synth, fakes.player, self.cache, VoiceSettings(sink="speaker")
        )
        self.arbiter = Arbiter(
            clock,
            MotionService(fakes.servos, fakes.motors, MotionSettings(), clock),
            voice=self.voice,
            display=DisplayService(fakes.display),
            led=LedService(fakes.led),
            buttons=self.router,
        )
        self.idle = IdleScreen(self.arbiter, clock)
        self.bus = EventBus()
        self.quiet = QuietMode(
            self.idle, self.bus, StateStore.for_feature(self.paths, "core", clock), clock
        )
        self.camera = FakeCamera()
        self.vision = SceneVision()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.heard = 0  # 聞き終えた言葉の数
        self.greeted: Callable[[], int] = lambda: 0  # あいさつした回数（テストが記録から数える）

    def store(self) -> StateStore:
        return StateStore.for_feature(self.paths, "presence", self.clock)

    def start(self) -> None:
        ctx = CoreFeatureContext(
            PresenceFeature.spec,
            arbiter=self.arbiter,
            buttons=self.router,
            idle=self.idle,
            quiet=self.quiet,
            bus=self.bus,
            clock=self.clock,
            paths=self.paths,
            cache=self.cache,
        )
        feature = PresenceFeature(
            ctx,
            PresenceConfig(),
            camera=self.camera,
            vision=self.vision,
            neck_retry=RETRY_S,
            loop_interval=LOOP_S,
        )
        self.thread = threading.Thread(target=feature.run, args=(self.stop,), daemon=True)
        self.thread.start()

    def holder(self, resource: Resource = Resource.NECK) -> str | None:
        return self.arbiter.holder(resource)

    def step(self, seconds: float = STEP_S) -> None:
        """偽の時計を進め、その後にフレームを 2 枚撮るまで待つ（進めた時刻で 1 回は見る）。"""
        self.clock.advance(seconds)
        count = self.camera.capture_count
        assert wait_for(lambda: self.camera.capture_count >= count + 2), "見る流れが回りません"

    def watch(self, seconds: float) -> None:
        """偽の時計で `seconds` 秒の間、見る流れに見させる。"""
        for _ in range(round(seconds / STEP_S)):
            self.step()

    def leave(self) -> None:
        """写っている人がいなくなり、見失うまで見させる（2.5 秒より長く）。"""
        self.vision.show(None)
        self.watch(3.0)

    def unheard(self) -> list[str]:
        """まだ確かめていない言葉。"""
        return self.voice.spoken[self.heard :]

    def hear_greeting(self, limit: float = 10.0) -> str:
        """偽の時計を進めながら、次のあいさつを待って返す（話し終えて記録するまで）。"""
        before = self.greeted()
        for _ in range(round(limit / STEP_S)):
            if self.greeted() > before:
                break
            self.step()
        assert self.greeted() == before + 1, "あいさつしませんでした"
        assert wait_for(lambda: self.holder(Resource.VOICE) is None)
        spoken = self.unheard()
        assert len(spoken) == 1
        self.heard = len(self.voice.spoken)
        return spoken[0]

    def close(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(WAIT_S)
            assert not self.thread.is_alive(), "止める旗で戻りませんでした"


@pytest.fixture
def journey(xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock) -> Iterator[Journey]:
    clock.set_now(START)
    kit = Journey(fakes, clock)
    yield kit
    kit.close()
    kit.router.close()


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """外へつなごうとしたら記録して断る（6.2）。"""
    tried: list[object] = []

    def refuse(_self: socket.socket, address: object) -> None:
        tried.append(address)
        raise OSError("テストでは外へつなぎません")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    return tried


def _greeted(caplog: pytest.LogCaptureFixture) -> int:
    """あいさつした回数（記録の数）。"""
    return sum(1 for r in caplog.records if r.getMessage().startswith("あいさつしました"))


def _acquire_in_thread(journey: Journey, priority: int) -> list[Lease]:
    got: list[Lease] = []

    def take() -> None:
        got.append(journey.arbiter.acquire(ROUTINE, priority, {Resource.NECK}))

    threading.Thread(target=take, daemon=True).start()
    return got


def test_見つけてあいさつを通して確かめる(
    journey: Journey,
    no_network: list[object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="osekkai")
    book = FaceBook(journey.store())
    book.put(Person(CHILD_ID, CHILD_NAME, (CHILD_EMB,)))
    book.save()
    camera, vision = journey.camera, journey.vision
    journey.greeted = lambda: _greeted(caplog)
    hello_known = {texts.fill(t, CHILD_NAME) for t in texts.HELLO_KNOWN}

    journey.start()
    assert wait_for(lambda: journey.holder() == "presence" and camera.started)
    journey.watch(1.0)  # 誰も写っていない間は、あいさつしない
    assert journey.unheard() == []

    # 1. 朝、知らない人を見つけると、呼び名なしでおはよう（3.6、3.7）
    vision.show(STRANGER_EMB)
    assert journey.hear_greeting() == texts.MORNING_UNKNOWN
    journey.leave()

    # 2. 続けて子どもを見つけると、名前でおはよう。知らない人の朝は子どもの朝を使わない（3.3、3.7）
    vision.show(CHILD_EMB)
    assert journey.hear_greeting() == texts.fill(texts.MORNING_KNOWN, CHILD_NAME)
    journey.watch(5.0)  # 見つけている間のあいさつは 1 回だけ
    assert journey.unheard() == []
    journey.leave()

    # 3. 20 分の間に見つけ直しても、あいさつしない（3.1）
    journey.clock.advance(10 * MINUTE)
    vision.show(CHILD_EMB)
    journey.watch(8.0)
    assert journey.unheard() == []
    journey.leave()

    # 4. 120 分の後に見つけると、おかえり（朝のあいさつは済んでいる。3.4）
    journey.clock.advance(121 * MINUTE)
    assert journey.clock.now().hour < 11  # まだ朝の時間帯の中でも、おはようを繰り返さない
    vision.show(CHILD_EMB)
    assert journey.hear_greeting() == texts.fill(texts.OKAERI_KNOWN, CHILD_NAME)
    journey.watch(2.0)

    # 知らない人の記録と子どもの記録は、別の鍵で、それぞれその日の朝を覚えている（3.7）
    saved = journey.store().load("greetings")
    assert saved is not None
    assert saved["morning"] == {"_unknown": "2026-01-01", CHILD_ID: "2026-01-01"}

    # 5. 日課（SCHEDULED）が首を求めると、見るのをやめてすぐ返す（4.2）。その間は見ない（4.3）
    begin = time.monotonic()
    got = _acquire_in_thread(journey, Priority.SCHEDULED)
    assert wait_for(lambda: bool(got))
    assert time.monotonic() - begin < 1.0  # 取り上げ（猶予の 2 秒）を待たずに返した
    assert journey.holder() == ROUTINE
    assert wait_for(lambda: not camera.started)
    captures = camera.capture_count
    journey.clock.advance(30 * MINUTE)  # 子どもは写ったまま。見ていれば、見た記録が続く
    time.sleep(QUIET_WAIT_S)
    assert camera.capture_count == captures
    assert journey.unheard() == []
    assert journey.holder() == ROUTINE
    # 返されたら借り直して、また見る。見ていなかったので、30 分ぶりのやっほー（4.6、3.5）
    got[0].release()
    assert wait_for(lambda: journey.holder() == "presence" and camera.started)
    assert journey.hear_greeting() in hello_known
    journey.watch(2.0)

    # 6. おやすみの間は、カメラを止め、顔を探さず、あいさつしない（4.4）
    journey.quiet.request_quiet()
    assert wait_for(lambda: not camera.started and journey.holder() is None)
    captures = camera.capture_count
    journey.clock.advance(30 * MINUTE)
    time.sleep(QUIET_WAIT_S)
    assert camera.capture_count == captures
    assert journey.unheard() == []
    assert journey.holder() is None
    journey.quiet.end_quiet()
    assert wait_for(lambda: journey.holder() == "presence" and camera.started)
    assert journey.hear_greeting() in hello_known
    journey.watch(2.0)

    # 7. カメラを借りている間は、顔を探さず、あいさつしない（7.2）
    with api.borrow_camera(BORROWER, alive=lambda: True) as loan:
        assert loan.frame().startswith(b"jpeg:")  # 見るスレッドが貸し出しに切り替わった
        detects = len(vision.detect_calls)
        journey.clock.advance(30 * MINUTE)
        time.sleep(QUIET_WAIT_S)
        assert loan.still().startswith(b"still:")
        assert len(vision.detect_calls) == detects
        assert journey.unheard() == []
    assert journey.hear_greeting() in hello_known  # 返されたら、また見る

    journey.close()
    assert api.running_watcher() is None
    assert _greeted(caplog) == 6  # 見つけている間のあいさつは、それぞれ 1 回だけ

    # 6.2: 外へつなごうとしていない
    assert no_network == []

    # 6.3: 流れ全体のログに、名前・呼び名・あいさつの言葉・LCD の文字が出ていない
    assert "あいさつしました" in caplog.text  # ログは集まっている
    hidden_values = [
        CHILD_ID,
        CHILD_NAME,
        texts.LCD_FOUND,
        texts.MORNING_UNKNOWN,
        texts.OKAERI_UNKNOWN,
        *texts.HELLO_UNKNOWN,
        texts.fill(texts.MORNING_KNOWN, CHILD_NAME),
        texts.fill(texts.OKAERI_KNOWN, CHILD_NAME),
        *hello_known,
    ]
    messages = [record.getMessage() for record in caplog.records]
    for hidden in hidden_values:
        assert hidden not in caplog.text
        assert all(hidden not in message for message in messages)
