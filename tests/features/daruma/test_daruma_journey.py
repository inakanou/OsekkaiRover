"""だるまさんを、言葉から勝ちまで通して確かめる（E2E）。

要件 1.1、3.2、3.3、4.4、4.7、6.1、6.2、6.3、7.5、9.1。

本物の基盤の部品（調停役・ボタンの振り分け・静かな状態・待機画面）を偽のデバイスで組み、
本物の音声の入口（偽の聞き取り）・本物の見つけてあいさつ（台本のカメラと映像の処理）・
本物のだるまさんを、それぞれ別のスレッドで動かす。言葉の登録はプロセスの中の登録表を使う。

1. 「だるまさん しよう」→ 入口が言葉を渡す → その直後の押下で入口が先に資源を借りて聞き始める
   → 遊び（56）が入口（55）に中断を知らせて資源を得る（6.1）
2. 遊びの間の押下は遊びにだけ届き、入口の聞き取りを始めない。見つけてあいさつは顔を探さない
   （6.1、6.2）。唱えている間の押下で勝ち、首だけで踊って勝ちの言葉（3.3）
3. 終わると首が正面、カメラと資源が返され、入口と見つけてあいさつがまた動く（4.7）
4. もう 1 回遊び、唱えている間に日課の優先度の機能が資源を求めると、言い終えてから黙って
   資源を返す（4.4）
5. 通しのログと self-check の結果に、呼び名と動きの量の値が出ない（6.3、7.5）

同期は条件を待つ（実際の秒数の上限つき）。決まった秒数を眠って合わせることはしない。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import osekkai.features.presence.api as presence_api
from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Arbiter
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.config import ConfigLoader, FeatureConfig, MotionSettings, VoiceSettings
from osekkai.core.display import DisplayService, LedService
from osekkai.core.events import EventBus
from osekkai.core.feature import CoreFeatureContext, FeatureSpec
from osekkai.core.idle import IdleScreen
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.quiet import QuietMode
from osekkai.core.selfcheck import (
    CheckContext,
    SelfCheckRunner,
    render_lines,
    results_to_json,
)
from osekkai.core.status import ServiceSnapshot
from osekkai.core.storage import StateStore
from osekkai.core.types import Priority, Resource
from osekkai.core.voice import VoiceService
from osekkai.features.daruma import DarumaFeature, texts
from osekkai.features.daruma import feature as feature_module
from osekkai.features.daruma.checks import DARUMA_CHECKS
from osekkai.features.daruma.config import DarumaConfig
from osekkai.features.daruma.flow import GameResult
from osekkai.features.daruma.rules import EndReason
from osekkai.features.presence import PresenceFeature
from osekkai.features.presence.config import PresenceConfig
from osekkai.features.presence.faces import EMBEDDING_SIZE, Embedding, normalize
from osekkai.features.presence.ports import Frame
from osekkai.features.presence.tracking import FaceBox
from osekkai.voice import VoiceGatewayFeature, api
from osekkai.voice.config import VoiceConfig
from osekkai.voice.recognizer import ListenCancelled, RecognizerState
from osekkai.voice.texts import SAY_CALL, SAY_NOT_HEARD

WAIT_S = 5.0  # 別のスレッドを待つ上限（実際の秒）
POLL_S = 0.002  # 条件を見直す間（実際の秒）
STEP_S = 0.25  # 見つけてあいさつのために偽の時計を 1 回に進める秒数

CALL_NAME = "ぽぽまる"  # 外に出てはいけない呼び名（10 文字より短く）
MOTION = 0.00437  # 台本の動きの量（しきい値 0.01 の下）。ログに出てはいけない
MOTION_TEXTS = ("0.00437", "0.0044", "0.004")
HEARD_DARUMA = "だるまさん しよう"
ROUTINE = "routine"
ROUTINE_LINE = "はみがきの じかん"  # 日課の声かけ（偽）
FACE = FaceBox(0.5, 0.0, 0.3)
CHANT_SPEECHES = frozenset(chant.speech for chant in texts.CHANTS)
DARUMA_SPEECHES = frozenset(
    {
        texts.SAY_INTRO,
        texts.SAY_KORONDA,
        texts.SAY_MOVED,
        texts.SAY_MAX_ROUNDS,
        texts.SAY_NOBODY,
        texts.SAY_NEXT_TASK,
        texts.SAY_NEXT_TASK_BEFORE,
        texts.SAY_NO_CAMERA,
        texts.SAY_CAMERA_LOST,
        texts.say_with_name(texts.SAY_GO, CALL_NAME),
        texts.say_with_name(texts.SAY_WIN, CALL_NAME),
        *CHANT_SPEECHES,
    }
)


def wait_for(condition: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(POLL_S)
    return condition()


# --- 偽物（台本のカメラ・映像の処理・聞き取り・声の記録） ---


@dataclass(frozen=True)
class _Frame:
    index: int
    width: int = 640
    height: int = 480


class ScriptCamera:
    """台本のカメラ。撮るたびに番号の続くフレームを返す。"""

    def __init__(self) -> None:
        self.started = False
        self.capture_count = 0

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def capture(self) -> Frame:
        frame = _Frame(self.capture_count)
        self.capture_count += 1
        return frame

    def capture_still(self) -> bytes:
        raise AssertionError("静止画は求めない")


class ScriptVision:
    """台本の映像の処理。`show` で写っている人を決め、動きの量はいつも MOTION。"""

    def __init__(self) -> None:
        self.face: FaceBox | None = None
        self.emb: Embedding | None = None
        self.detect_count = 0
        self.motion_count = 0

    def show(self, emb: Embedding | None) -> None:
        self.emb = emb
        self.face = None if emb is None else FACE

    def detect(self, frame: Frame) -> tuple[FaceBox, ...]:
        self.detect_count += 1
        face = self.face
        return () if face is None else (face,)

    def embed(self, frame: Frame, face: FaceBox) -> Embedding | None:
        return self.emb

    def motion(self, before: Frame, after: Frame) -> float:
        self.motion_count += 1
        return MOTION

    def to_jpeg(self, frame: Frame, max_width: int, quality: int) -> bytes:
        raise AssertionError("映像は求めない")


@dataclass(frozen=True)
class Hold:
    """台本: 取り消されるまで録音を続ける（中断で取り消されるのを待つ）。"""


class JourneyRecognizer:
    """台本どおりに返す偽の聞き取り（`Recognizer` を満たす）。"""

    def __init__(self, script: Iterable[str | Hold]) -> None:
        self._lock = threading.Lock()
        self._script: deque[str | Hold] = deque(script)
        self._state = RecognizerState.PREPARING
        self.calls = 0
        self.listening = threading.Event()  # Hold で録音を始めた
        self.cancelled = threading.Event()  # Hold が取り消された

    @property
    def state(self) -> RecognizerState:
        with self._lock:
            return self._state

    def prepare(self) -> None:
        with self._lock:
            self._state = RecognizerState.READY

    def listen(
        self,
        seconds: float,
        *,
        hints: Sequence[str],
        should_continue: Callable[[], bool],
        on_recording: Callable[[bool], None],
    ) -> str:
        with self._lock:
            self.calls += 1
            if not self._script:
                raise AssertionError("偽の聞き取りの台本がもうありません")
            outcome = self._script.popleft()
        on_recording(True)
        try:
            if isinstance(outcome, Hold):
                self.listening.set()
                if not wait_for(lambda: not should_continue()):
                    return ""  # 取り消されなかった（テストが確かめて落とす）
                self.cancelled.set()
                raise ListenCancelled
            if not should_continue():
                raise ListenCancelled
        finally:
            on_recording(False)
        return outcome

    @property
    def remaining(self) -> int:
        with self._lock:
            return len(self._script)


class RecordingVoice(VoiceService):
    """本物の声の部品。話し終えた文章を順に残し、話し終えるたびに `after_say` を呼ぶ。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._said_lock = threading.Lock()
        self._said: list[str] = []
        self.after_say: Callable[[str], None] | None = None

    def speak(
        self,
        text: str,
        *,
        keep: bool = True,
        should_continue: Callable[[], bool] | None = None,
    ) -> bool:
        spoke = super().speak(text, keep=keep, should_continue=should_continue)
        with self._said_lock:
            self._said.append(text)
        hook = self.after_say
        if hook is not None:
            hook(text)
        return spoke

    @property
    def said(self) -> list[str]:
        with self._said_lock:
            return list(self._said)


def _embedding(seed: int) -> Embedding:
    rng = random.Random(seed)
    return normalize([rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_SIZE)])


# --- 一式 ---


class Rover:
    """基盤の部品を偽のデバイスで組み、入口・見つけてあいさつ・だるまさんを動かす一式。"""

    def __init__(
        self, fakes: FakeDeviceSet, clock: FakeClock, recognizer: JourneyRecognizer
    ) -> None:
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
        self.camera = ScriptCamera()
        self.vision = ScriptVision()
        self.recognizer = recognizer
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.router.start()
        self.presence = PresenceFeature(
            self._context(PresenceFeature.spec),
            PresenceConfig(),
            camera=self.camera,
            vision=self.vision,
            neck_retry=0.02,
            loop_interval=0.005,
        )
        self.gateway = VoiceGatewayFeature(
            self._context(VoiceGatewayFeature.spec),
            VoiceConfig(),
            recognizer=recognizer,
            registry=api.registry(),  # ほかの機能と同じ、プロセスの中の登録表
            acquire_wait=0.2,
            job_poll=0.05,
        )
        config = DarumaConfig(call_name=CALL_NAME, watch_seconds=1.0, max_rounds=3)
        self.daruma = DarumaFeature(self._context(DarumaFeature.spec), config, poll_s=0.01)

    def _context(self, spec: FeatureSpec) -> CoreFeatureContext:
        return CoreFeatureContext(
            spec,
            arbiter=self.arbiter,
            buttons=self.router,
            idle=self.idle,
            quiet=self.quiet,
            bus=self.bus,
            clock=self.clock,
            paths=self.paths,
            cache=self.cache,
        )

    def start(self) -> None:
        for feature in (self.presence, self.gateway, self.daruma):
            thread = threading.Thread(target=feature.run, args=(self.stop,), daemon=True)
            thread.start()
            self.threads.append(thread)
        assert wait_for(lambda: self.recognizer.state is RecognizerState.READY)
        assert wait_for(lambda: bool(self.router._waiters)), "入口が待ち手になっていません"
        assert wait_for(lambda: api.registry().owner_alive("daruma"))
        assert wait_for(lambda: self.holder(Resource.NECK) == "presence" and self.camera.started)

    def holder(self, resource: Resource) -> str | None:
        return self.arbiter.holder(resource)

    def press(self) -> None:
        """押す。前の押下と重ならないよう、時計を進めてから押す（チャタリングの除去）。"""
        self.clock.advance(1.0)
        self.fakes.buttons.press()

    def gateway_idle(self) -> bool:
        return wait_for(lambda: not self.gateway.busy)

    def step(self) -> None:
        """偽の時計を進め、見つけてあいさつがフレームを 2 枚撮るまで待つ。"""
        self.clock.advance(STEP_S)
        count = self.camera.capture_count
        assert wait_for(lambda: self.camera.capture_count >= count + 2), "見る流れが回りません"

    def close(self) -> None:
        self.stop.set()
        for thread in self.threads:
            thread.join(WAIT_S)
            assert not thread.is_alive(), "止める旗で戻りませんでした"
        self.router.close()


class GameLog:
    """遊びの 1 回ごとの終わり方を残す。始める前に、テストが決めた待ちを入れられる。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.results: list[GameResult] = []
        self.entered = threading.Event()
        self.before: deque[Callable[[], None]] = deque()
        original = feature_module.run_game

        def run_game(*args: Any, **kwargs: Any) -> GameResult:
            self.entered.set()
            if self.before:
                self.before.popleft()()
            result = original(*args, **kwargs)
            self.results.append(result)
            return result

        monkeypatch.setattr(feature_module, "run_game", run_game)

    def wait(self, count: int) -> GameResult:
        assert wait_for(lambda: len(self.results) >= count), "遊びが終わりません"
        return self.results[count - 1]


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    api._reset_for_tests()
    yield
    api._reset_for_tests()


def _greeted(caplog: pytest.LogCaptureFixture) -> int:
    return sum(1 for r in caplog.records if r.getMessage().startswith("あいさつしました"))


def _self_check_text(paths: Paths) -> str:
    """呼び名を書いた設定で、だるまさんの self-check を動かした結果の文字。"""
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(
        f"presence: {{}}\ndaruma:\n  call_name: {CALL_NAME}\n",
        encoding="utf-8",
    )
    models: dict[str, type[FeatureConfig]] = {"daruma": DarumaConfig, "presence": PresenceConfig}
    report = ConfigLoader(paths, models).load(create_missing=False)
    service = ServiceSnapshot(
        pid=1, version="v0.1.0", heartbeat=1.0, driving=False, features=("presence", "daruma")
    )
    ctx = CheckContext(paths=paths, report=report, service=service, hardware_allowed=False)
    results = SelfCheckRunner().run(DARUMA_CHECKS, ctx)
    assert [name for name, _ in results] == ["daruma.camera"]
    return "\n".join([*render_lines(results), results_to_json(results), repr(results)])


def test_言葉から勝ちまで通し_日課に譲って_入口と見つけてあいさつが戻る(
    xdg_dirs: dict[str, Path],
    fakes: FakeDeviceSet,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    recognizer = JourneyRecognizer([HEARD_DARUMA, Hold(), "", HEARD_DARUMA, ""])
    rover = Rover(fakes, clock, recognizer)
    games = GameLog(monkeypatch)
    voice, vision = rover.voice, rover.vision
    try:
        rover.start()

        # --- 1. 言葉を渡した直後の押下で、入口が先に資源を借りても、遊びが資源を得る（6.1） ---
        # 遊びは、入口が 2 回目の聞き取りを始めるまで借りに行かない（入口を先にする）
        games.before.append(lambda: assert_true(recognizer.listening.wait(WAIT_S)))
        rover.press()
        assert games.entered.wait(WAIT_S), "言葉が遊びに届きません"  # 1.1
        assert rover.gateway_idle()
        assert voice.said == [SAY_CALL]

        # 遊びの間の押下と見つけてあいさつの様子を、遊びの言葉に合わせて残す
        in_game: dict[str, int] = {}
        chants = 0

        def during_first_game(text: str) -> None:
            nonlocal chants
            if text == texts.SAY_INTRO:
                in_game["detects"] = vision.detect_count
                in_game["greeted"] = _greeted(caplog)
                in_game["calls"] = recognizer.calls
                in_game["loaned"] = int(_watcher_loaned())
                rover.press()  # 始めの言葉の間の押下は、数えずに捨てる（1.3）
            elif text in CHANT_SPEECHES:
                chants += 1
                if chants == 2:
                    in_game["detects_late"] = vision.detect_count
                    in_game["motions"] = vision.motion_count
                    rover.press()  # 唱えている間の押下で勝ち（3.1）

        voice.after_say = during_first_game
        rover.press()  # 言葉を渡した直後の押下。入口が先に資源を借りて聞き始める
        first = games.wait(1)
        voice.after_say = None

        assert recognizer.listening.is_set()
        assert recognizer.cancelled.is_set(), "入口の聞き取りが取り消されていません"
        assert first.reason is EndReason.WIN
        assert first.rounds == 1  # 1 回目は止まっていて、2 回目に勝ち

        said = voice.said
        game_said = said[2:]
        # 入口は「なあに？」の後、何も話さずに資源を返した（取り消し）
        assert said[:2] == [SAY_CALL, SAY_CALL]
        assert game_said[0] == texts.SAY_INTRO
        assert game_said[1] == texts.say_with_name(texts.SAY_GO, CALL_NAME)
        assert game_said[-1] == texts.say_with_name(texts.SAY_WIN, CALL_NAME)  # 3.3
        assert all(text in DARUMA_SPEECHES for text in game_said)
        assert game_said.count(texts.SAY_KORONDA) == 1
        assert sum(text in CHANT_SPEECHES for text in game_said) == 2

        # 遊びの間の押下は、入口の聞き取りを始めなかった（6.1）
        assert in_game["calls"] == 2
        assert recognizer.calls == 2
        # 遊びの間は、見つけてあいさつが顔を探さず、あいさつもしない。カメラは貸し出し中（6.2）
        assert in_game["loaned"] == 1
        assert in_game["detects_late"] == in_game["detects"]
        assert _greeted(caplog) == in_game["greeted"] == 0
        assert in_game["motions"] > 0  # 動きの量は、見つけてあいさつから借りたカメラで見た

        # 首だけで踊った（タイヤは回さない）。終わると首は正面（3.3、4.7）
        assert "set_speeds" not in fakes.motors.ops()
        assert wait_for(lambda: not _watcher_loaned()), "カメラが返されていません"  # 4.7
        assert rover.holder(Resource.VOICE) is None
        assert rover.holder(Resource.BUTTON) is None
        assert fakes.servos.angles.get("pan") == 0.0
        assert fakes.servos.angles.get("tilt") == 0.0

        # --- 2. 終わった後、入口の待ち手に押下が届き、見つけてあいさつがまた動く（4.7） ---
        assert rover.gateway_idle()
        rover.press()
        assert wait_for(lambda: recognizer.calls == 3), "入口が聞き取りを始めません"
        assert rover.gateway_idle()
        assert voice.said[-2:] == [SAY_CALL, SAY_NOT_HEARD]

        assert wait_for(lambda: rover.holder(Resource.NECK) == "presence" and rover.camera.started)
        detects = vision.detect_count
        rover.step()
        assert vision.detect_count > detects  # また顔を探している
        vision.show(_embedding(99))
        for _ in range(round(10.0 / STEP_S)):
            if _greeted(caplog) > 0:
                break
            rover.step()
        assert _greeted(caplog) == 1, "見つけてあいさつがあいさつしません"
        assert wait_for(lambda: rover.holder(Resource.VOICE) is None)
        vision.show(None)
        for _ in range(round(3.0 / STEP_S)):
            rover.step()

        # --- 3. もう 1 回遊び、日課の優先度の機能が資源を求めると、黙って終える（4.4） ---
        chanting = threading.Event()
        preempt_seen: list[bool] = []

        def during_second_game(text: str) -> None:
            if text in CHANT_SPEECHES and not chanting.is_set():
                lease = rover.arbiter.lease_for(Resource.VOICE)
                chanting.set()
                # 唱えている一言を言い終える前に、日課が資源を求める
                preempt_seen.append(
                    lease is not None
                    and lease.owner == "daruma"
                    and lease.preempt_requested.wait(WAIT_S)
                )

        voice.after_say = during_second_game
        assert rover.gateway_idle()
        rover.press()
        assert chanting.wait(WAIT_S), "2 回目の遊びが唱え始めません"
        before = len(voice.said)
        routine = rover.arbiter.acquire(
            ROUTINE, Priority.SCHEDULED + 5, {Resource.VOICE, Resource.DISPLAY}
        )
        try:
            routine.voice.say(ROUTINE_LINE)  # 日課の声かけが始まる
        finally:
            routine.release()
        second = games.wait(2)
        voice.after_say = None

        assert preempt_seen == [True]
        assert second.reason is EndReason.PREEMPTED
        # 唱えている一言を言い終えた後は、何も話さずに資源を返した
        assert voice.said[before - 1] in CHANT_SPEECHES
        assert voice.said[before:] == [ROUTINE_LINE]
        assert wait_for(lambda: not _watcher_loaned())
        assert recognizer.calls == 4

        # 譲った後も、入口と見つけてあいさつがまた動く（4.7）
        assert rover.gateway_idle()
        rover.press()
        assert wait_for(lambda: recognizer.calls == 5)
        assert rover.gateway_idle()
        assert voice.said[-2:] == [SAY_CALL, SAY_NOT_HEARD]
        assert wait_for(lambda: rover.holder(Resource.NECK) == "presence" and rover.camera.started)
        assert recognizer.remaining == 0
    finally:
        voice.after_say = None
        rover.close()

    # 遊びは入口に中断を知らせて資源を得た（6.1）。日課は遊びに中断を知らせた（4.4）
    messages = [r.getMessage() for r in caplog.records]
    assert "中断を知らせました: voice" in messages
    assert "中断を知らせました: daruma" in messages
    assert "遊びを始めました" in messages

    # 通しのログと self-check の結果に、呼び名と動きの量が出ない（6.3、7.5）
    hidden = (CALL_NAME, *MOTION_TEXTS)
    shown = [f"{r.getMessage()} {r.args!r}" for r in caplog.records]
    shown.append(_self_check_text(rover.paths))
    for text in shown:
        for value in hidden:
            assert value not in text


def assert_true(value: bool) -> None:
    assert value, "入口が聞き始めません"


def _watcher_loaned() -> bool:
    watcher = presence_api.running_watcher()
    return watcher is not None and watcher.loaned
