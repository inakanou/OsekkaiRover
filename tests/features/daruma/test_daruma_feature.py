"""だるまさんの機能（言葉の登録・遊びは 1 つだけ・借りる・後始末）のテスト。

要件 1.1、1.6、3.2、3.5、4.4、4.5、4.7、4.9、5.1、5.3、6.1、7.2。
偽のデバイスで組んだ基盤の部品（資源の調停・静かな状態）で動かす。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Arbiter, Lease
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.clock import Clock
from osekkai.core.config import MotionSettings, VoiceSettings
from osekkai.core.display import DisplayService
from osekkai.core.events import EventBus
from osekkai.core.feature import CoreFeatureContext, FeatureSpec
from osekkai.core.idle import IdleScreen
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.quiet import QuietMode
from osekkai.core.storage import StateStore
from osekkai.core.types import Priority, Resource
from osekkai.core.voice import VoiceService
from osekkai.features.daruma import DarumaFeature, texts
from osekkai.features.daruma import feature as feature_module
from osekkai.features.daruma.camera import BorrowCamera
from osekkai.features.daruma.checks import DARUMA_CHECKS
from osekkai.features.daruma.config import DarumaConfig
from osekkai.features.daruma.feature import RESOURCES, run_game
from osekkai.features.daruma.flow import GameResult
from osekkai.features.daruma.lease_io import LeaseGameIO
from osekkai.features.daruma.rules import EndReason
from osekkai.features.presence.api import CameraLoan, CameraNotAvailable
from osekkai.voice import api
from osekkai.voice.api import Heard
from osekkai.voice.intents import Nobody, Words

WAIT_S = 5.0  # 別のスレッドを待つ上限（実際の秒）
POLL_S = 0.01  # テストで待ち行列を見る間隔
OTHER_SPEC = FeatureSpec(name="other", priority=Priority.SCHEDULED, resources=frozenset(Resource))


@pytest.fixture(autouse=True)
def _empty_registry() -> Iterator[None]:
    api._reset_for_tests()
    yield
    api._reset_for_tests()


@pytest.fixture(autouse=True)
def _short_acquire_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    # 借りられないときに実際の 2 秒を待たない
    monkeypatch.setattr(feature_module, "ACQUIRE_WAIT_S", 0.05)


def _wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        assert time.monotonic() < deadline, "待ちきれませんでした"
        time.sleep(0.005)


class FakeLoan:
    """偽の借りたカメラ。動きの量はいつも 0（止まっている）。"""

    def __init__(self) -> None:
        self.releases = 0

    def motion(self, seconds: float) -> float:
        return 0.0

    def frame(self, max_width: int = 640, quality: int = 70) -> bytes:
        raise AssertionError("映像は求めない")

    def still(self) -> bytes:
        raise AssertionError("静止画は求めない")

    def release(self) -> None:
        self.releases += 1

    def __enter__(self) -> FakeLoan:
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


class FakeBorrow:
    """偽のカメラを借りる口。`gate` が決まっていれば、放されるまで止まる。"""

    def __init__(self, *, camera: bool = True, gate: threading.Event | None = None) -> None:
        self.camera = camera
        self.gate = gate
        self.calls = 0
        self.entered = threading.Event()
        self.loan = FakeLoan()

    def __call__(self, owner: str, alive: Callable[[], bool]) -> CameraLoan:
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(WAIT_S)
        if not self.camera:
            raise CameraNotAvailable
        return self.loan


class RecordingIO(LeaseGameIO):
    """本物の遊びの口に、話した言葉と後始末の数を覚えさせたもの。

    `fail_on_look` なら首を向けた直後に思いがけない例外を投げる。
    `after_say` は話し終えた言葉を受ける。
    """

    def __init__(
        self,
        kit: Kit,
        lease: Lease,
        *,
        borrow: BorrowCamera,
        clock: Clock,
        interrupted: Callable[[], EndReason | None],
        stop: threading.Event,
    ) -> None:
        super().__init__(lease, borrow=borrow, clock=clock, interrupted=interrupted, stop=stop)
        self._kit = kit

    def say(self, text: str) -> None:
        super().say(text)
        self._kit.spoken.append(text)
        if self._kit.after_say is not None:
            self._kit.after_say(text)

    def look(self, pan: float, tilt: float) -> None:
        super().look(pan, tilt)
        if self._kit.fail_on_look:
            raise RuntimeError("boom")

    def close(self) -> None:
        super().close()
        self._kit.closes += 1


class Kit:
    """基盤の部品（資源の調停・静かな状態）を偽のデバイスで組んだ一式。"""

    def __init__(self, fakes: FakeDeviceSet, clock: FakeClock) -> None:
        self.fakes = fakes
        self.clock = clock
        self.paths = Paths()
        self.router = ButtonRouter(fakes.buttons, clock)
        self.cache = CacheManager.from_paths(self.paths, 10, clock)
        self.voice = VoiceService(
            fakes.synth, fakes.player, self.cache, VoiceSettings(sink="speaker")
        )
        self.arbiter = Arbiter(
            clock,
            MotionService(fakes.servos, fakes.motors, MotionSettings(), clock),
            voice=self.voice,
            display=DisplayService(fakes.display),
            buttons=self.router,
        )
        self.idle = IdleScreen(self.arbiter, clock)
        self.bus = EventBus()
        self.quiet = QuietMode(
            self.idle, self.bus, StateStore.for_feature(self.paths, "core", clock), clock
        )
        self.ctx = self.context(DarumaFeature.spec)
        self.spoken: list[str] = []
        self.closes = 0
        self.fail_on_look = False
        self.after_say: Callable[[str], None] | None = None
        self.results: list[GameResult] = []

    def context(self, spec: FeatureSpec) -> CoreFeatureContext:
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

    def io_factory(
        self,
        lease: Lease,
        *,
        borrow: BorrowCamera,
        clock: Clock,
        interrupted: Callable[[], EndReason | None],
        stop: threading.Event,
    ) -> RecordingIO:
        return RecordingIO(
            self, lease, borrow=borrow, clock=clock, interrupted=interrupted, stop=stop
        )


@pytest.fixture
def kit(
    xdg_dirs: dict[str, Path],
    fakes: FakeDeviceSet,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> Kit:
    made = Kit(fakes, clock)
    original = feature_module.run_game

    def counted(*args: Any, **kwargs: Any) -> GameResult:
        result = original(*args, **kwargs)
        made.results.append(result)
        return result

    monkeypatch.setattr(feature_module, "run_game", counted)
    return made


class Driver:
    """機能を別のスレッドで動かし、入口のように言葉を渡す。"""

    def __init__(self, kit: Kit, borrow: FakeBorrow, config: DarumaConfig | None = None) -> None:
        self.kit = kit
        self.feature = DarumaFeature(
            kit.ctx,
            config or DarumaConfig(),
            borrow=borrow,
            io_factory=kit.io_factory,
            poll_s=POLL_S,
        )
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.feature.run, args=(self.stop,), daemon=True)

    def start(self) -> Driver:
        self.thread.start()
        _wait_until(lambda: api.registry().owner_alive("daruma"))
        return self

    def hear(self, text: str = "だるま") -> float:
        """入口と同じに、語句の登録の口に渡す。戻るまでの実際の秒を返す。"""
        route = api.registry().route(text)
        assert isinstance(route, Words)
        assert route.registration.owner == "daruma"
        began = time.monotonic()
        route.registration.handler(Heard(text, False))
        return time.monotonic() - began

    def play(self, text: str = "だるま") -> GameResult:
        """言葉を渡し、遊びが終わるまで待つ。"""
        before = len(self.kit.results)
        self.hear(text)
        _wait_until(lambda: len(self.kit.results) > before)
        return self.kit.results[-1]

    def finish(self) -> None:
        self.stop.set()
        self.thread.join(WAIT_S)
        assert not self.thread.is_alive()


# 申告


def test_spec_declares_priority_resources_and_checks() -> None:
    spec = DarumaFeature.spec
    assert spec.name == "daruma"
    assert spec.priority == 56  # 入口（55）より上、日課（80）より下（6.1）
    assert spec.resources == {
        Resource.VOICE,
        Resource.DISPLAY,
        Resource.BUTTON,
        Resource.NECK,
    }
    assert Resource.DRIVE not in spec.resources  # 5.1
    assert spec.config_model is DarumaConfig
    assert spec.checks == DARUMA_CHECKS
    assert spec.resources == RESOURCES


def test_package_exports_feature() -> None:
    import osekkai.features.daruma as package

    assert "DarumaFeature" in package.__all__


# 言葉と登録（1.1、7.2）


@pytest.mark.parametrize("phrase", texts.START_PHRASES)
def test_start_words_start_a_game(kit: Kit, phrase: str) -> None:
    borrow = FakeBorrow(camera=False)
    driver = Driver(kit, borrow).start()
    try:
        result = driver.play(phrase)
    finally:
        driver.finish()
    assert result.reason is EndReason.NO_CAMERA
    assert borrow.calls == 1
    assert kit.spoken == [texts.SAY_NO_CAMERA]
    assert kit.closes == 1  # 4.7


def test_stop_removes_registration(kit: Kit) -> None:
    driver = Driver(kit, FakeBorrow()).start()
    assert isinstance(api.registry().route("だるま"), Words)
    driver.finish()
    assert not api.registry().owner_alive("daruma")
    assert isinstance(api.registry().route("だるま"), Nobody)


def test_handler_returns_at_once_and_second_word_is_ignored(kit: Kit) -> None:
    gate = threading.Event()
    borrow = FakeBorrow(camera=False, gate=gate)
    driver = Driver(kit, borrow).start()
    try:
        assert driver.hear() < 1.0
        assert borrow.entered.wait(WAIT_S)
        # 遊んでいる間の言葉には答えない（4.9）
        assert driver.hear("転んだ") < 1.0
        gate.set()
        _wait_until(lambda: len(kit.results) == 1)
        time.sleep(POLL_S * 20)  # 捨てた言葉で遊びが始まらないのを見る
    finally:
        gate.set()
        driver.finish()
    assert borrow.calls == 1
    assert len(kit.results) == 1
    assert kit.spoken == [texts.SAY_NO_CAMERA]


def test_next_game_can_start_after_one_ends(kit: Kit) -> None:
    borrow = FakeBorrow(camera=False)
    driver = Driver(kit, borrow).start()
    try:
        driver.play()
        driver.play()
    finally:
        driver.finish()
    assert borrow.calls == 2


# 借りる（1.6、6.1）


def test_busy_resources_end_silently(kit: Kit) -> None:
    other = kit.context(OTHER_SPEC).acquire({Resource.VOICE})
    borrow = FakeBorrow()
    driver = Driver(kit, borrow).start()
    try:
        result = driver.play()
    finally:
        driver.finish()
        other.release()
    assert result.reason is EndReason.BUSY
    assert borrow.calls == 0
    assert kit.spoken == []
    assert kit.fakes.synth.lines == []  # 話さない（1.6）


def test_game_holds_resources_until_it_ends(kit: Kit) -> None:
    gate = threading.Event()
    borrow = FakeBorrow(camera=False, gate=gate)
    driver = Driver(kit, borrow).start()
    try:
        driver.hear()
        assert borrow.entered.wait(WAIT_S)
        held = {r: kit.arbiter.holder(r) for r in Resource}
        gate.set()
        _wait_until(lambda: len(kit.results) == 1)
        after = {r: kit.arbiter.holder(r) for r in Resource}
    finally:
        gate.set()
        driver.finish()
    assert {r for r, owner in held.items() if owner == "daruma"} == RESOURCES
    assert "daruma" not in after.values()


# 誤り（5.3、4.7）


def test_error_ends_only_that_game_and_head_returns(kit: Kit) -> None:
    kit.fail_on_look = True
    borrow = FakeBorrow()
    driver = Driver(kit, borrow).start()
    try:
        result = driver.play()
        assert driver.thread.is_alive()  # 機能は動き続ける
        assert kit.fakes.servos.angles == {"pan": 0.0, "tilt": 0.0}  # 首は正面（4.7）
        assert borrow.loan.releases == 1  # カメラを返した
        assert kit.closes == 1
        kit.fail_on_look = False
        borrow.camera = False
        second = driver.play()  # 次の言葉でまた遊べる
    finally:
        driver.finish()
    assert result.reason is EndReason.ERROR
    assert second.reason is EndReason.NO_CAMERA
    assert all(kit.arbiter.holder(r) != "daruma" for r in Resource)


# run_game の中断（4.4、4.5）


def _run(kit: Kit, borrow: FakeBorrow, stop: threading.Event | None = None) -> GameResult:
    return run_game(
        kit.ctx,
        DarumaConfig(),
        stop or threading.Event(),
        borrow=borrow,
        status=lambda: None,
        io_factory=kit.io_factory,
    )


def test_quiet_ends_without_borrowing(kit: Kit) -> None:
    kit.ctx.request_quiet()
    borrow = FakeBorrow()
    result = _run(kit, borrow)
    assert result.reason is EndReason.QUIET
    assert borrow.calls == 0
    assert kit.spoken == []


def test_stop_flag_ends_without_borrowing(kit: Kit) -> None:
    stop = threading.Event()
    stop.set()
    borrow = FakeBorrow()
    assert _run(kit, borrow, stop).reason is EndReason.STOPPED
    assert borrow.calls == 0


def test_preempt_notice_ends_after_the_current_line(kit: Kit) -> None:
    high: list[Lease] = []
    taker = threading.Thread(
        target=lambda: high.append(kit.context(OTHER_SPEC).acquire({Resource.VOICE}, wait=5.0)),
        daemon=True,
    )

    def _preempt_noticed() -> bool:
        lease = kit.arbiter.lease_for(Resource.VOICE)
        return lease is not None and lease.preempt_requested.is_set()

    def after_say(text: str) -> None:
        if text == texts.SAY_INTRO and not taker.is_alive():
            taker.start()
            _wait_until(_preempt_noticed)

    kit.after_say = after_say
    borrow = FakeBorrow()
    result = _run(kit, borrow)
    taker.join(WAIT_S)
    assert result.reason is EndReason.PREEMPTED
    assert kit.spoken == [texts.SAY_INTRO]  # 言い終えた後は話さない（4.4）
    assert borrow.loan.releases == 1
    assert kit.fakes.servos.angles.get("pan", 0.0) == 0.0
    assert len(high) == 1
    high[0].release()


def test_quiet_during_game_ends_silently(kit: Kit) -> None:
    def after_say(text: str) -> None:
        if text == texts.SAY_INTRO:
            kit.ctx.request_quiet()

    kit.after_say = after_say
    borrow = FakeBorrow()
    result = _run(kit, borrow)
    assert result.reason is EndReason.QUIET
    assert kit.spoken == [texts.SAY_INTRO]  # 4.5
    assert borrow.loan.releases == 1


# 保存しない（3.2、3.5）


def test_game_writes_nothing_to_storage(kit: Kit, xdg_dirs: dict[str, Path]) -> None:
    borrow = FakeBorrow()
    kit.fail_on_look = True
    _run(kit, borrow)
    found = (p for d in xdg_dirs.values() for p in d.rglob("*"))
    written = [p for p in found if "daruma" in p.parts and p.is_file()]
    assert written == []
