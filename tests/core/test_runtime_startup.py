"""起動の順番（Runtime.start）のテスト（1.1〜1.7、11.2〜11.4）。"""

import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import Field

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import JST, FakeClock
from osekkai.core import FeatureConfig, FeatureContext, FeatureSpec, Priority, Resource
from osekkai.core.idle import IdleState
from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.paths import Paths
from osekkai.core.registry import FeatureRegistry
from osekkai.core.runtime import (
    ALREADY_RUNNING_MESSAGE,
    CONFIG_ERROR_TEXT,
    STARTUP_VOICE,
    VERSION_LOG_PREFIX,
    ExitCode,
    Runtime,
    within_hours,
)
from osekkai.core.supervisor import FeatureState
from shared_fixtures import version_log_prefix

WAIT_S = 3.0


def _row(text: str) -> tuple[int, ...]:
    codes = to_lcd_codes(text)[:16]
    return tuple(codes) + (0x20,) * (16 - len(codes))


def _wait_for(condition: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class _Probe:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.snapshots: dict[str, dict[str, Any]] = {}


class _StrictConfig(FeatureConfig):
    count: int = Field(default=1, ge=0)


def make_feature(name: str, probe: _Probe, fakes: FakeDeviceSet) -> type:
    """run の始めに、偽物の記録を写し取る機能。"""

    class _Feature:
        spec: ClassVar[FeatureSpec] = FeatureSpec(
            name=name,
            priority=Priority.BACKGROUND,
            resources=frozenset({Resource.VOICE}),
            config_model=_StrictConfig,
        )

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            self.ctx = ctx

        def run(self, stop: threading.Event) -> None:
            probe.snapshots[name] = {
                "motors": list(fakes.motors.calls),
                "servos": list(fakes.servos.calls),
                "led": list(fakes.led.calls),
            }
            probe.started.set()
            stop.wait()

    _Feature.__name__ = f"Feature_{name}"
    return _Feature


@pytest.fixture
def paths(xdg_dirs: dict[str, Path]) -> Paths:
    return Paths()


@pytest.fixture
def probe() -> _Probe:
    return _Probe()


@pytest.fixture
def runtimes() -> Iterator[list[Runtime]]:
    made: list[Runtime] = []
    yield made
    for runtime in made:
        runtime.shutdown(budget=1.0)


@pytest.fixture
def make_runtime(paths: Paths, runtimes: list[Runtime]) -> Callable[..., Runtime]:
    def build(
        fakes: FakeDeviceSet,
        clock: FakeClock,
        features: list[type],
        *,
        fail: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> Runtime:
        runtime = Runtime(fakes.to_devices(fail), paths, clock, FeatureRegistry(features), **kwargs)
        runtimes.append(runtime)
        return runtime

    return build


def write_config(paths: Paths, text: str) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")


# --- 時間帯 ---


@pytest.mark.parametrize(
    ("now", "hours", "expected"),
    [
        ("08:00", ("08:00", "20:00"), True),
        ("19:59", ("08:00", "20:00"), True),
        ("20:00", ("08:00", "20:00"), False),
        ("07:59", ("08:00", "20:00"), False),
        ("23:00", ("22:00", "06:00"), True),
        ("05:59", ("22:00", "06:00"), True),
        ("06:00", ("22:00", "06:00"), False),
        ("12:00", ("22:00", "06:00"), False),
        ("12:00", ("12:00", "12:00"), False),
    ],
)
def test_時間帯は始まりを含み終わりを含まず日をまたいでもよい(
    now: str, hours: tuple[str, str], expected: bool
) -> None:
    window = (dtime.fromisoformat(hours[0]), dtime.fromisoformat(hours[1]))
    assert within_hours(dtime.fromisoformat(now), window) is expected


# --- 起動の順番 ---


def test_中立の姿勢にしてから機能を開始する(
    fakes: FakeDeviceSet, clock: FakeClock, probe: _Probe, make_runtime: Callable[..., Runtime]
) -> None:
    runtime = make_runtime(fakes, clock, [make_feature("alpha", probe, fakes)])
    assert runtime.start() is None
    assert probe.started.wait(WAIT_S)
    snap = probe.snapshots["alpha"]
    motor_ops = [c.op for c in snap["motors"]]
    assert "stop_now" in motor_ops
    assert ("set_speeds", (0, 0)) in [(c.op, c.args) for c in snap["motors"]]
    angles = {c.args[0]: c.args[1] for c in snap["servos"] if c.op == "set_angle"}
    assert angles == {"pan": 0.0, "tilt": 0.0, "steering": 0.0}
    assert [(c.op, c.args) for c in snap["led"]] == [("set", (False,))]
    assert fakes.hat.ops() == ["reset"]


def test_起動の後に待機画面を出す(
    fakes: FakeDeviceSet, clock: FakeClock, make_runtime: Callable[..., Runtime]
) -> None:
    runtime = make_runtime(fakes, clock, [])
    assert runtime.start() is None
    assert runtime.idle is not None
    assert runtime.idle.state is IdleState.SHOWING
    assert fakes.display.rows[0][:11] == tuple(to_lcd_codes("01/01 09:00"))


def test_時刻が確かで時間帯の中なら起動の声を出す(
    fakes: FakeDeviceSet, clock: FakeClock, make_runtime: Callable[..., Runtime]
) -> None:
    runtime = make_runtime(fakes, clock, [])
    runtime.start()
    assert fakes.synth.lines == [STARTUP_VOICE]
    assert len(fakes.player.played) == 1


def test_時間帯の外では起動の声を出さない(
    paths: Paths, make_runtime: Callable[..., Runtime]
) -> None:
    clock = FakeClock(datetime(2026, 1, 1, 21, 0, tzinfo=JST))
    fakes = FakeDeviceSet.create(clock)
    runtime = make_runtime(fakes, clock, [])
    runtime.start()
    assert fakes.synth.lines == []
    assert fakes.player.played == []


def test_時刻が確かでなければ起動の声を出さない(
    make_runtime: Callable[..., Runtime],
) -> None:
    clock = FakeClock(synchronized=False)
    fakes = FakeDeviceSet.create(clock)
    runtime = make_runtime(fakes, clock, [])
    runtime.start()
    assert fakes.player.played == []


def test_起動の声に失敗しても起動を続ける(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    probe: _Probe,
    make_runtime: Callable[..., Runtime],
    caplog: pytest.LogCaptureFixture,
) -> None:
    fakes.player.fail_on.add("play")
    runtime = make_runtime(fakes, clock, [make_feature("alpha", probe, fakes)])
    with caplog.at_level(logging.WARNING, logger="osekkai"):
        assert runtime.start() is None
    assert probe.started.wait(WAIT_S)
    assert STARTUP_VOICE not in caplog.text
    assert "起動の声を出せませんでした" in caplog.text


def test_版をログに出す(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    make_runtime: Callable[..., Runtime],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("osekkai.version.get_version", lambda: "v9.9.9-test")
    runtime = make_runtime(fakes, clock, [])
    with caplog.at_level(logging.INFO, logger="osekkai"):
        runtime.start()
    assert "v9.9.9-test" in caplog.text


def test_版のログの文言の頭は共通の見本と同じ(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    make_runtime: Callable[..., Runtime],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 配置の道具はこの文言でログから版を探す（device-deployment 4.5）
    assert version_log_prefix() == VERSION_LOG_PREFIX
    monkeypatch.setattr("osekkai.version.get_version", lambda: "v9.9.9-test")
    runtime = make_runtime(fakes, clock, [])
    with caplog.at_level(logging.INFO, logger="osekkai.core.runtime"):
        runtime.start()
    assert f"{VERSION_LOG_PREFIX}v9.9.9-test" in caplog.messages


def test_LCDが使えなくてもほかの機能は開始し使えないデバイスをログに出す(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    probe: _Probe,
    make_runtime: Callable[..., Runtime],
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = make_runtime(fakes, clock, [make_feature("alpha", probe, fakes)], fail=("display",))
    with caplog.at_level(logging.INFO, logger="osekkai"):
        assert runtime.start() is None
    assert probe.started.wait(WAIT_S)
    assert fakes.display.calls == []
    assert "デバイス display が使えません" in caplog.text
    assert "機能 alpha を開始しました" in caplog.text


def test_HATのリセットに失敗しても起動を続ける(
    fakes: FakeDeviceSet, clock: FakeClock, probe: _Probe, make_runtime: Callable[..., Runtime]
) -> None:
    fakes.hat.fail_on.add("reset")
    runtime = make_runtime(fakes, clock, [make_feature("alpha", probe, fakes)])
    assert runtime.start() is None
    assert probe.started.wait(WAIT_S)


def test_2つ目の起動はデバイスに触らず75で終える(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    make_runtime: Callable[..., Runtime],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = make_runtime(fakes, clock, [])
    assert first.start() is None
    capsys.readouterr()

    other = FakeDeviceSet.create(clock)
    second = make_runtime(other, clock, [])
    with caplog.at_level(logging.ERROR, logger="osekkai"):
        assert second.start() is ExitCode.ALREADY_RUNNING
    assert ALREADY_RUNNING_MESSAGE in capsys.readouterr().err
    assert ALREADY_RUNNING_MESSAGE in caplog.text
    for device in (
        other.display,
        other.led,
        other.buttons,
        other.servos,
        other.motors,
        other.synth,
        other.player,
        other.hat,
    ):
        assert device.calls == []
    assert int(ExitCode.ALREADY_RUNNING) == 75


# --- 設定 ---


def test_設定ファイルがなければ見本から作って起動する(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    paths: Paths,
    probe: _Probe,
    make_runtime: Callable[..., Runtime],
) -> None:
    assert not paths.config_file.exists()
    runtime = make_runtime(fakes, clock, [make_feature("alpha", probe, fakes)])
    assert runtime.start() is None
    assert paths.config_file.exists()
    assert runtime.report is not None and runtime.report.created_from_example
    assert probe.started.wait(WAIT_S)


def test_致命の設定の誤りでは機能を開始せず設定の誤りを出し続ける(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    paths: Paths,
    probe: _Probe,
    make_runtime: Callable[..., Runtime],
) -> None:
    write_config(paths, "core: [こわれた\n")
    runtime = make_runtime(
        fakes, clock, [make_feature("alpha", probe, fakes)], config_error_seconds=0.05
    )
    assert runtime.start() is None
    assert runtime.supervisor is not None
    assert runtime.supervisor.states() == {}
    assert runtime.started_features == []
    assert fakes.display.rows[0] == _row(CONFIG_ERROR_TEXT)
    # 時間がたっても待機画面に戻らない
    time.sleep(0.2)
    assert runtime.idle is not None
    runtime.idle.tick()
    assert fakes.display.rows[0] == _row(CONFIG_ERROR_TEXT)
    assert not probe.started.is_set()


def test_見本から作れないときは致命の誤りとして扱う(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    paths: Paths,
    probe: _Probe,
    make_runtime: Callable[..., Runtime],
    caplog: pytest.LogCaptureFixture,
) -> None:
    paths.config_dir.parent.mkdir(parents=True, exist_ok=True)
    paths.config_dir.write_text("ディレクトリではない", encoding="utf-8")
    runtime = make_runtime(fakes, clock, [make_feature("alpha", probe, fakes)])
    with caplog.at_level(logging.ERROR, logger="osekkai"):
        assert runtime.start() is None
    assert runtime.report is not None and runtime.report.fatal_errors
    assert runtime.started_features == []
    assert fakes.display.rows[0] == _row(CONFIG_ERROR_TEXT)
    assert "見本から作れません" in caplog.text


def test_機能の節の誤りではその機能だけを除き設定の誤りを出してから待機画面に戻る(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    paths: Paths,
    make_runtime: Callable[..., Runtime],
) -> None:
    good, bad = _Probe(), _Probe()
    write_config(paths, "bad:\n  count: -1\n")
    runtime = make_runtime(
        fakes,
        clock,
        [make_feature("good", good, fakes), make_feature("bad", bad, fakes)],
        config_error_seconds=0.3,
    )
    assert runtime.start() is None
    assert fakes.display.rows[0] == _row(CONFIG_ERROR_TEXT)
    assert good.started.wait(WAIT_S)
    assert runtime.started_features == ["good"]
    assert runtime.supervisor is not None
    assert "bad" not in runtime.supervisor.states()
    assert runtime.supervisor.states()["good"] is FeatureState.RUNNING

    idle = runtime.idle
    assert idle is not None
    assert _wait_for(lambda: idle.state is IdleState.SHOWING)
    assert fakes.display.rows[0][:11] == tuple(to_lcd_codes("01/01 09:00"))
    assert not bad.started.is_set()


def test_停止で始めたものを止めてロックを放す(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    paths: Paths,
    probe: _Probe,
    make_runtime: Callable[..., Runtime],
) -> None:
    runtime = make_runtime(fakes, clock, [make_feature("alpha", probe, fakes)])
    runtime.start()
    assert probe.started.wait(WAIT_S)
    runtime.shutdown(budget=1.0)
    assert runtime.supervisor is not None
    assert runtime.supervisor.states()["alpha"] is FeatureState.STOPPED
    assert fakes.display.ops()[-1] == "set_backlight"
    assert fakes.display.backlight is False

    again = make_runtime(FakeDeviceSet.create(clock), clock, [])
    assert again.start() is None
