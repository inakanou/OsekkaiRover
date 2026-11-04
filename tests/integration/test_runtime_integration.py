"""偽のデバイスで Runtime の全体をつなぐ結合テスト。

要件: 2.5、3.1、3.5、4.3、4.5、9.4、9.5、10.3、12.6。

テスト用の機能をエントリーポイントの代わりに `FeatureRegistry` に渡し、本物の Runtime を動かす。
機能とテストの間は `threading.Event` で合図し、イベントの配達は `bus.wait_idle` で待つ。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import Field

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core import (
    ButtonPress,
    Feature,
    FeatureConfig,
    FeatureContext,
    FeatureSpec,
    Lease,
    LeaseRevoked,
    Priority,
    QuietChanged,
    Resource,
    TimeSynced,
)
from osekkai.core.cache import MIB
from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.paths import Paths
from osekkai.core.registry import FeatureRegistry
from osekkai.core.runtime import Runtime
from osekkai.core.supervisor import FeatureState

WAIT_S = 3.0


def _wait_for(condition: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


def _row(text: str) -> tuple[int, ...]:
    codes = to_lcd_codes(text)[:16]
    return tuple(codes) + (0x20,) * (16 - len(codes))


@dataclass
class Probe:
    """機能とテストの間の合図と記録。"""

    events: dict[str, threading.Event] = field(default_factory=dict)
    records: dict[str, list[object]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def ev(self, name: str) -> threading.Event:
        with self.lock:
            return self.events.setdefault(name, threading.Event())

    def add(self, key: str, value: object) -> None:
        with self.lock:
            self.records.setdefault(key, []).append(value)

    def get(self, key: str) -> list[object]:
        with self.lock:
            return list(self.records.get(key, []))


def _spec(
    name: str,
    priority: int,
    resources: set[Resource],
    model: type[FeatureConfig] = FeatureConfig,
) -> FeatureSpec:
    return FeatureSpec(
        name=name, priority=priority, resources=frozenset(resources), config_model=model
    )


# --- テスト用の機能 ---


def low_driver(probe: Probe, fakes: FakeDeviceSet) -> type[Feature]:
    """低い優先度で首と走行を借りて走り、中断を知らされたら返す。"""

    class LowDriver:
        spec: ClassVar[FeatureSpec] = _spec(
            "low", Priority.BACKGROUND, {Resource.NECK, Resource.DRIVE}
        )

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            self.ctx = ctx

        def _on_preempt(self) -> None:
            # 知らせを受けた時点のモーターの速さを残す（4.5）
            probe.add("speeds_at_notice", fakes.motors.speeds)
            probe.ev("low.preempted").set()

        def run(self, stop: threading.Event) -> None:
            lease = self.ctx.acquire([Resource.NECK, Resource.DRIVE], on_preempt=self._on_preempt)
            lease.neck.look(20.0, 10.0)
            lease.drive.drive(10.0, 50)
            probe.ev("low.driving").set()
            while not (probe.ev("low.preempted").is_set() or stop.is_set()):
                stop.wait(0.01)
            try:
                lease.drive.drive(10.0, 50)
            except LeaseRevoked:
                probe.add("low.refused", True)
            lease.release()
            probe.ev("low.released").set()
            stop.wait()

    return LowDriver


def high_speaker(probe: Probe) -> type[Feature]:
    """合図で首と声を借りる（低い方を中断する）。合図で返す。"""

    class HighSpeaker:
        spec: ClassVar[FeatureSpec] = _spec(
            "high", Priority.SCHEDULED, {Resource.NECK, Resource.VOICE}
        )

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            self.ctx = ctx

        def run(self, stop: threading.Event) -> None:
            go = probe.ev("high.go")
            while not (go.is_set() or stop.is_set()):
                go.wait(0.01)
            if stop.is_set():
                return
            lease = self.ctx.acquire([Resource.NECK, Resource.VOICE])
            probe.ev("high.granted").set()
            lease.neck.look(-15.0, 5.0)
            lease.voice.say("おはよう")
            probe.ev("high.moved").set()
            release = probe.ev("high.release")
            while not (release.is_set() or stop.is_set()):
                release.wait(0.01)
            lease.release()
            probe.ev("high.released").set()
            stop.wait()

    return HighSpeaker


def crasher(probe: Probe) -> type[Feature]:
    """走行を借りて走り、合図で例外を出す。"""

    class Crasher:
        spec: ClassVar[FeatureSpec] = _spec("crasher", Priority.BACKGROUND + 1, {Resource.DRIVE})

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            self.ctx = ctx

        def run(self, stop: threading.Event) -> None:
            lease = self.ctx.acquire([Resource.DRIVE])
            lease.drive.drive(0.0, 60)
            probe.ev("crasher.driving").set()
            crash = probe.ev("crasher.go")
            while not (crash.is_set() or stop.is_set()):
                crash.wait(0.01)
            raise RuntimeError("テストの例外")

    return Crasher


def button_waiter(probe: Probe) -> type[Feature]:
    """ボタンの押下を待ち、合図で走行を借りられるか確かめる。"""

    class ButtonWaiter:
        spec: ClassVar[FeatureSpec] = _spec(
            "waiter", Priority.INTERACTIVE, {Resource.BUTTON, Resource.DRIVE}
        )

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            self.ctx = ctx

        def _on_press(self, press: ButtonPress) -> None:
            probe.add("presses", press.source)
            probe.ev("waiter.pressed").set()

        def run(self, stop: threading.Event) -> None:
            self.ctx.want_button_presses(self._on_press)
            probe.ev("waiter.ready").set()
            borrow = probe.ev("waiter.borrow")
            while not (borrow.is_set() or stop.is_set()):
                borrow.wait(0.01)
            if stop.is_set():
                return
            with self.ctx.acquire([Resource.DRIVE], wait=0.5) as lease:
                lease.drive.drive(0.0, 30)
                probe.add("waiter.drove", True)
            probe.ev("waiter.borrowed").set()
            stop.wait()

    return ButtonWaiter


def greedy(probe: Probe) -> type[Feature]:
    """高い優先度でボタンと知らせを待ち、合図で例外を出すか、正常に終わる。"""

    class Greedy:
        spec: ClassVar[FeatureSpec] = _spec("greedy", Priority.SCHEDULED, {Resource.VOICE})

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            self.ctx = ctx
            ctx.subscribe(QuietChanged, lambda e: probe.add("greedy.quiet", e.quiet))

        def run(self, stop: threading.Event) -> None:
            self.ctx.want_button_presses(lambda p: probe.add("greedy.presses", p.source))
            probe.ev("greedy.ready").set()
            crash, leave = probe.ev("greedy.crash"), probe.ev("greedy.leave")
            while not (crash.is_set() or leave.is_set() or stop.is_set()):
                stop.wait(0.01)
            if crash.is_set():
                raise RuntimeError("テストの例外")

    return Greedy


def quiet_toggler(probe: Probe) -> type[Feature]:
    """合図で静かな状態に入り、次の合図で終える。自分でも知らせを受ける。"""

    class QuietToggler:
        spec: ClassVar[FeatureSpec] = _spec("toggler", Priority.BACKGROUND, {Resource.VOICE})

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            self.ctx = ctx
            ctx.subscribe(QuietChanged, lambda e: probe.add("toggler.quiet", e.quiet))

        def run(self, stop: threading.Event) -> None:
            probe.ev("toggler.ready").set()
            for step, action in (("on", self.ctx.request_quiet), ("off", self.ctx.end_quiet)):
                go = probe.ev(f"toggler.{step}")
                while not (go.is_set() or stop.is_set()):
                    go.wait(0.01)
                if stop.is_set():
                    return
                action()
                probe.ev(f"toggler.{step}.done").set()
            stop.wait()

    return QuietToggler


def listener(name: str, probe: Probe) -> type[Feature]:
    """静かな状態と時刻の同期の知らせを受けて残すだけの機能。"""

    class Listener:
        spec: ClassVar[FeatureSpec] = _spec(name, Priority.REMOTE, {Resource.VOICE})

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            ctx.subscribe(QuietChanged, lambda e: probe.add(f"{name}.quiet", e.quiet))
            ctx.subscribe(TimeSynced, lambda e: probe.add(f"{name}.synced", e))

        def run(self, stop: threading.Event) -> None:
            probe.ev(f"{name}.ready").set()
            stop.wait()

    Listener.__name__ = f"Listener_{name}"
    return Listener


class _SpeakerConfig(FeatureConfig):
    texts: list[str] = Field(default=[])


def speaker(probe: Probe) -> type[Feature]:
    """古いキャッシュを置いてから、設定の文章を順に話す。"""

    class Speaker:
        spec: ClassVar[FeatureSpec] = _spec(
            "speaker", Priority.BACKGROUND, {Resource.VOICE}, _SpeakerConfig
        )

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            assert isinstance(config, _SpeakerConfig)
            self.ctx = ctx
            self.texts = config.texts

        def _put_old(self, name: str, size: int, age_s: float) -> None:
            path = self.ctx.cache_dir / name
            with open(path, "wb") as f:
                f.truncate(size)  # 中身のない大きなファイル
            stamp = self.ctx.clock.now().timestamp() - age_s
            os.utime(path, (stamp, stamp))

        def run(self, stop: threading.Event) -> None:
            self._put_old("oldest.bin", 6 * MIB, 300.0)
            self._put_old("older.bin", 4 * MIB, 200.0)
            go = probe.ev("speaker.go")
            while not (go.is_set() or stop.is_set()):
                go.wait(0.01)
            if stop.is_set():
                return
            with self.ctx.acquire([Resource.VOICE], wait=1.0) as lease:
                for text in self.texts:
                    lease.voice.say(text)
            probe.ev("speaker.done").set()
            stop.wait()

    return Speaker


class _GreeterConfig(FeatureConfig):
    word: str = Field(default="")
    times: int = Field(default=1, ge=1)


def greeter(name: str, probe: Probe) -> type[Feature]:
    """受け取った設定を残すだけの機能。"""

    class Greeter:
        spec: ClassVar[FeatureSpec] = _spec(
            name, Priority.INTERACTIVE, {Resource.VOICE}, _GreeterConfig
        )

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            assert isinstance(config, _GreeterConfig)
            probe.add(f"{name}.config", (config.word, config.times))

        def run(self, stop: threading.Event) -> None:
            probe.ev(f"{name}.started").set()
            stop.wait()

    Greeter.__name__ = f"Greeter_{name}"
    return Greeter


def stubborn(probe: Probe) -> type[Feature]:
    """走行を借りて走り、停止に応えない。テストの最後に `stubborn.exit` で終わる。"""

    class Stubborn:
        spec: ClassVar[FeatureSpec] = _spec("stubborn", Priority.BACKGROUND, {Resource.DRIVE})

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            self.ctx = ctx

        def run(self, stop: threading.Event) -> None:
            lease: Lease = self.ctx.acquire([Resource.DRIVE])
            lease.drive.drive(0.0, 40)
            probe.ev("stubborn.driving").set()
            probe.ev("stubborn.exit").wait(WAIT_S * 2)

    return Stubborn


# --- 準備 ---


@pytest.fixture
def paths(xdg_dirs: dict[str, Path]) -> Paths:
    return Paths()


@pytest.fixture
def probe() -> Iterator[Probe]:
    made = Probe()
    yield made
    made.ev("stubborn.exit").set()  # 応えない機能のスレッドを終わらせる


@pytest.fixture
def runtimes() -> Iterator[list[Runtime]]:
    made: list[Runtime] = []
    yield made
    for runtime in made:
        runtime.shutdown(budget=1.0)


@pytest.fixture
def start_runtime(paths: Paths, runtimes: list[Runtime], tmp_path: Path) -> Callable[..., Runtime]:
    def build(
        fakes: FakeDeviceSet,
        clock: FakeClock,
        features: list[type[Feature]],
        *,
        config: str | None = None,
        stop_budget: float = 1.0,
    ) -> Runtime:
        override: Path | None = None
        if config is not None:
            override = tmp_path / "integration.yaml"
            override.write_text(config, encoding="utf-8")
        runtime = Runtime(
            fakes.to_devices(),
            paths,
            clock,
            FeatureRegistry(features),
            config_override=override,
            stop_budget=stop_budget,
        )
        runtimes.append(runtime)
        assert runtime.start() is None
        return runtime

    return build


def _backlight(fakes: FakeDeviceSet) -> bool:
    return fakes.display.backlight


def _states(runtime: Runtime) -> dict[str, FeatureState]:
    assert runtime.supervisor is not None
    return dict(runtime.supervisor.states())


# --- 中断（4.3、4.5、2.5） ---


def test_高い優先度の機能が低い方の走行と首を中断して取り上げる(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    probe: Probe,
    start_runtime: Callable[..., Runtime],
) -> None:
    runtime = start_runtime(fakes, clock, [low_driver(probe, fakes), high_speaker(probe)])
    assert runtime.arbiter is not None
    assert probe.ev("low.driving").wait(WAIT_S)
    assert fakes.motors.speeds != (0, 0)
    assert runtime.arbiter.holder(Resource.DRIVE) == "low"

    probe.ev("high.go").set()

    # 低い方に中断が届き、届いた時点でモーターはもう止まっている（4.3、4.5）
    assert probe.ev("low.preempted").wait(WAIT_S)
    assert probe.get("speeds_at_notice") == [(0, 0)]
    assert probe.ev("low.released").wait(WAIT_S)
    assert probe.get("low.refused") == [True]  # 中断の後は走れない

    # 高い方が借り、低い方の走行は返った（2.5）
    assert probe.ev("high.granted").wait(WAIT_S)
    assert probe.ev("high.moved").wait(WAIT_S)
    assert runtime.arbiter.holder(Resource.NECK) == "high"
    assert runtime.arbiter.holder(Resource.DRIVE) is None
    assert fakes.motors.speeds == (0, 0)
    assert fakes.servos.angles["pan"] == pytest.approx(-15.0)
    assert "おはよう" in fakes.synth.lines

    # 高い方が首を返したら、首は正面に戻る
    probe.ev("high.release").set()
    assert probe.ev("high.released").wait(WAIT_S)
    assert fakes.servos.angles["pan"] == pytest.approx(0.0)
    assert fakes.servos.angles["tilt"] == pytest.approx(0.0)
    assert runtime.arbiter.holder(Resource.NECK) is None
    assert _states(runtime) == {"low": FeatureState.RUNNING, "high": FeatureState.RUNNING}


# --- 機能の異常の隔離（3.5、2.5） ---


def test_機能の異常でモーターが止まりほかの機能は動き続ける(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    probe: Probe,
    start_runtime: Callable[..., Runtime],
) -> None:
    runtime = start_runtime(fakes, clock, [crasher(probe), button_waiter(probe)])
    assert runtime.arbiter is not None
    assert probe.ev("crasher.driving").wait(WAIT_S)
    assert probe.ev("waiter.ready").wait(WAIT_S)
    assert fakes.motors.speeds != (0, 0)

    probe.ev("crasher.go").set()

    assert _wait_for(lambda: _states(runtime).get("crasher") is FeatureState.FAILED)
    # 走行の持ち主がいなくなったのでモーターが止まり、資源は返った（2.5、3.5）
    assert fakes.motors.speeds == (0, 0)
    assert runtime.arbiter.holder(Resource.DRIVE) is None
    assert _states(runtime)["waiter"] is FeatureState.RUNNING

    # ほかの機能は押下を受け取り、返った走行を借りられる
    fakes.buttons.press("usr")
    assert probe.ev("waiter.pressed").wait(WAIT_S)
    assert probe.get("presses") == ["usr"]
    probe.ev("waiter.borrow").set()
    assert probe.ev("waiter.borrowed").wait(WAIT_S)
    assert probe.get("waiter.drove") == [True]
    assert fakes.motors.speeds == (0, 0)  # 返したら止まる
    assert _states(runtime)["waiter"] is FeatureState.RUNNING


@pytest.mark.parametrize(
    ("signal", "state"),
    [("greedy.crash", FeatureState.FAILED), ("greedy.leave", FeatureState.STOPPED)],
)
def test_終わった機能の待ち手と受け手は外れてほかの機能に届く(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    probe: Probe,
    start_runtime: Callable[..., Runtime],
    signal: str,
    state: FeatureState,
) -> None:
    runtime = start_runtime(fakes, clock, [greedy(probe), button_waiter(probe)])
    assert runtime.bus is not None and runtime.quiet is not None
    assert probe.ev("greedy.ready").wait(WAIT_S)
    assert probe.ev("waiter.ready").wait(WAIT_S)

    probe.ev(signal).set()
    assert _wait_for(lambda: _states(runtime).get("greedy") is state)

    # 終わった高い方の待ち手が押下を飲み込まず、動いている低い方に届く（5.2、5.3）
    fakes.buttons.press("usr")
    assert probe.ev("waiter.pressed").wait(WAIT_S)
    assert probe.get("presses") == ["usr"]
    assert probe.get("greedy.presses") == []

    # 終わった機能には知らせが届かない（9.4）
    runtime.quiet.request_quiet()
    assert runtime.bus.wait_idle(WAIT_S)
    assert probe.get("greedy.quiet") == []
    assert _states(runtime)["waiter"] is FeatureState.RUNNING


# --- 静かな状態（9.4、9.5） ---


def test_静かな状態の切り替えが全機能に届き画面が消えて戻る(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    probe: Probe,
    start_runtime: Callable[..., Runtime],
) -> None:
    runtime = start_runtime(
        fakes, clock, [quiet_toggler(probe), listener("alpha", probe), listener("beta", probe)]
    )
    bus = runtime.bus
    assert bus is not None
    for name in ("toggler", "alpha", "beta"):
        assert probe.ev(f"{name}.ready").wait(WAIT_S)
    assert _backlight(fakes) is True
    assert fakes.display.rows[0][:11] == tuple(to_lcd_codes("01/01 09:00"))

    probe.ev("toggler.on").set()
    assert probe.ev("toggler.on.done").wait(WAIT_S)
    assert bus.wait_idle(WAIT_S)
    for key in ("toggler.quiet", "alpha.quiet", "beta.quiet"):
        assert probe.get(key) == [True]
    assert _backlight(fakes) is False
    assert fakes.display.rows == [_row(""), _row("")]

    probe.ev("toggler.off").set()
    assert probe.ev("toggler.off.done").wait(WAIT_S)
    assert bus.wait_idle(WAIT_S)
    for key in ("toggler.quiet", "alpha.quiet", "beta.quiet"):
        assert probe.get(key) == [True, False]
    assert _backlight(fakes) is True
    assert fakes.display.rows[0][:11] == tuple(to_lcd_codes("01/01 09:00"))


# --- 時刻の同期（10.3） ---


def test_時刻の同期の知らせが全機能に1回だけ届き待機画面に時刻が出る(
    probe: Probe, start_runtime: Callable[..., Runtime]
) -> None:
    clock = FakeClock(synchronized=False)
    fakes = FakeDeviceSet.create(clock)
    runtime = start_runtime(fakes, clock, [listener("alpha", probe), listener("beta", probe)])
    bus, monitor, idle = runtime.bus, runtime.monitor, runtime.idle
    assert bus is not None and monitor is not None and idle is not None
    assert probe.ev("alpha.ready").wait(WAIT_S)
    assert probe.ev("beta.ready").wait(WAIT_S)
    assert fakes.display.rows[0][:11] == tuple(to_lcd_codes("01/01 --:--"))

    assert monitor.check_once() is False
    assert bus.wait_idle(WAIT_S)
    assert probe.get("alpha.synced") == []

    clock.set_synchronized(True)
    assert monitor.check_once() is True
    assert monitor.check_once() is True  # 2 回目は知らせない
    assert bus.wait_idle(WAIT_S)
    assert probe.get("alpha.synced") == [TimeSynced()]
    assert probe.get("beta.synced") == [TimeSynced()]

    idle.tick()
    assert fakes.display.rows[0][:11] == tuple(to_lcd_codes("01/01 09:00"))


# --- キャッシュの上限（12.6） ---


def test_キャッシュの上限を超えると古いものから消える(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    paths: Paths,
    probe: Probe,
    start_runtime: Callable[..., Runtime],
) -> None:
    config = "core:\n  cache_max_mb: 10\nspeaker:\n  texts: [いち, に, さん]\n"
    runtime = start_runtime(fakes, clock, [speaker(probe)], config=config)
    assert runtime.report is not None and runtime.report.core.cache_max_mb == 10
    own = paths.cache_dir / "speaker"
    assert _wait_for(lambda: (own / "older.bin").exists())
    clock.advance(1.0)  # 話した声のほうを新しくする

    probe.ev("speaker.go").set()
    assert probe.ev("speaker.done").wait(WAIT_S)

    assert fakes.synth.lines[-3:] == ["いち", "に", "さん"]
    # 合計 10 MiB を超えたので、最も古い 6 MiB だけが消えた
    assert not (own / "oldest.bin").exists()
    assert (own / "older.bin").exists()
    tts = list((paths.cache_dir / "core" / "tts").glob("*.wav"))
    assert len(tts) == 4  # 起動の声と 3 つの文章
    total = sum(p.stat().st_size for p in paths.cache_dir.rglob("*") if p.is_file())
    assert total <= 10 * MIB


# --- 機能の登録と設定（3.1） ---


def test_渡した機能が設定の節を受け取って開始する(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    probe: Probe,
    start_runtime: Callable[..., Runtime],
) -> None:
    config = "hello:\n  word: やあ\n  times: 3\nquiet_one:\n  enabled: false\n"
    runtime = start_runtime(
        fakes,
        clock,
        [greeter("hello", probe), greeter("plain", probe), greeter("quiet_one", probe)],
        config=config,
    )
    assert probe.ev("hello.started").wait(WAIT_S)
    assert probe.ev("plain.started").wait(WAIT_S)
    assert runtime.started_features == ["hello", "plain"]
    assert probe.get("hello.config") == [("やあ", 3)]
    assert probe.get("plain.config") == [("", 1)]  # 節がなければ既定値
    assert probe.get("quiet_one.config") == []
    assert _states(runtime) == {"hello": FeatureState.RUNNING, "plain": FeatureState.RUNNING}


# --- 停止に応えない機能 ---


def test_停止に応えない機能がいても片付けてモーターが止まる(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    probe: Probe,
    start_runtime: Callable[..., Runtime],
) -> None:
    runtime = start_runtime(fakes, clock, [stubborn(probe), listener("alpha", probe)])
    assert probe.ev("stubborn.driving").wait(WAIT_S)
    assert probe.ev("alpha.ready").wait(WAIT_S)
    assert fakes.motors.speeds != (0, 0)

    started = time.monotonic()
    runtime.shutdown(budget=0.3)
    assert time.monotonic() - started < 2.0
    assert runtime.abandoned == ["stubborn"]
    assert _states(runtime)["alpha"] is FeatureState.STOPPED
    assert fakes.motors.speeds == (0, 0)
