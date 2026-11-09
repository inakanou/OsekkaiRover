"""ラジコンの self-check の項目（remote.setup・remote.sensors・remote.usb_camera）のテスト。

要件 1.4、1.5、13.4、13.5、13.6。
一時の保存先（XDG）と設定ファイルから基盤の ConfigLoader で報告を作り、
基盤の SelfCheckRunner で動かす。センサー・時計・カメラの見つけ方・Flask の有無は、
checks のモジュールの関数を差し替える。
"""

from __future__ import annotations

import importlib.util
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from remote_fakes import CLEAR_READING, FakeClock, ScriptedSensors

from osekkai.core.clock import Clock
from osekkai.core.config import ConfigLoader, ConfigReport, FeatureConfig
from osekkai.core.paths import Paths
from osekkai.core.selfcheck import (
    REASON_SKIPPED_BUSY,
    CheckContext,
    CheckResult,
    CheckStatus,
    SelfCheckRunner,
    render_lines,
    results_to_json,
)
from osekkai.core.status import ServiceSnapshot
from osekkai.features.remote import checks
from osekkai.features.remote.adapters.sensors import FLOOR_WARMUP_S
from osekkai.features.remote.checks import (
    REASON_CONFIG_ERROR,
    REASON_CONFIG_UNUSABLE,
    REASON_DISABLED,
    REASON_NO_CAMERA,
    REASON_NO_FLASK,
    REASON_NO_LIBRARY,
    REASON_NO_SENSOR_PARTS,
    REASON_PIN_UNUSABLE,
    REASON_PORT_UNUSABLE,
    REASON_SENSORS_UNREADABLE,
    REMOTE_CHECKS,
)
from osekkai.features.remote.config import RemoteConfig
from osekkai.features.remote.ports import SensorPort, SensorReading

SETUP = "remote.setup"
SENSORS = "remote.sensors"
CAMERA = "remote.usb_camera"
NAMES = (SETUP, SENSORS, CAMERA)

# 出力に出てはいけない、目立つ偽の値（10 文字より短く）
HIDDEN_PIN = "48151623"
HIDDEN_CAM = "/dev/hid9"
HIDDEN_FLOOR = 1357  # 床の値も出さない
OK = CheckResult(CheckStatus.OK)


@pytest.fixture
def paths(xdg_dirs: dict[str, Path]) -> Paths:
    return Paths()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(checks, "make_clock", lambda: fake)
    return fake


class _Sensors(ScriptedSensors):
    """台本のセンサー。作るときに渡された時計を覚える。"""

    def __init__(self) -> None:
        super().__init__()
        self.made: list[Clock] = []

    def make(self, given: Clock) -> SensorPort:
        self.made.append(given)
        return self


@pytest.fixture
def sensors(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> _Sensors:
    """センサーの台本。ライブラリがあることにする。"""
    fake = _Sensors()
    monkeypatch.setattr(checks, "sensors_available", lambda: True)
    monkeypatch.setattr(checks, "make_sensors", fake.make)
    return fake


@pytest.fixture(autouse=True)
def _flask_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks, "flask_available", lambda: True)


@pytest.fixture
def camera_found(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []

    def find(configured: str) -> str | None:
        asked.append(configured)
        return HIDDEN_CAM

    monkeypatch.setattr(checks, "find_usb_camera", find)
    return asked


@pytest.fixture
def busy_port() -> Iterator[int]:
    """使われているポート（ほかが 0.0.0.0 で待ち受けている）。"""
    server = socket.create_server(("0.0.0.0", 0))
    try:
        yield int(server.getsockname()[1])
    finally:
        server.close()


@pytest.fixture
def free_port() -> int:
    with socket.create_server(("0.0.0.0", 0)) as probe:
        return int(probe.getsockname()[1])


def _report(paths: Paths, text: str | None) -> ConfigReport:
    if text is not None:
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        paths.config_file.write_text(text, encoding="utf-8")
    models: dict[str, type[FeatureConfig]] = {"remote": RemoteConfig}
    return ConfigLoader(paths, models).load(create_missing=False)


def _remote(body: str, port: int | None = None) -> str:
    port_line = f"  port: {port}\n" if port is not None else ""
    return f"remote:\n  enabled: true\n  pin: '{HIDDEN_PIN}'\n{port_line}{body}"


def _snapshot() -> ServiceSnapshot:
    return ServiceSnapshot(
        pid=1, version="v0.1.0", heartbeat=1.0, driving=False, features=("remote",)
    )


def _run_all(
    paths: Paths,
    text: str | None,
    *,
    hardware_allowed: bool = True,
    service: ServiceSnapshot | None = None,
) -> dict[str, CheckResult]:
    ctx = CheckContext(
        paths=paths,
        report=_report(paths, text),
        service=service,
        hardware_allowed=hardware_allowed,
    )
    results = SelfCheckRunner().run(REMOTE_CHECKS, ctx)
    # pin・カメラの道・センサーの値は、表示にも JSON にも出ない（13.6）
    shown = "\n".join(render_lines(results)) + results_to_json(results)
    for hidden in (HIDDEN_PIN, HIDDEN_CAM, str(HIDDEN_FLOOR)):
        assert hidden not in shown
    return dict(results)


def _run(
    paths: Paths,
    name: str,
    text: str | None,
    *,
    hardware_allowed: bool = True,
    service: ServiceSnapshot | None = None,
) -> CheckResult:
    return _run_all(paths, text, hardware_allowed=hardware_allowed, service=service)[name]


# --- 申告 ---


def test_項目の名前と順と種類() -> None:
    assert tuple(check.name for check in REMOTE_CHECKS) == NAMES
    by_name = {check.name: check for check in REMOTE_CHECKS}
    assert by_name[SETUP].hardware is False
    assert by_name[SENSORS].hardware is True
    assert by_name[CAMERA].hardware is False
    assert all(check.owner == "remote" for check in REMOTE_CHECKS)


def test_センサーの項目の時間は床の待ちより長い() -> None:
    by_name = {check.name: check for check in REMOTE_CHECKS}
    assert by_name[SENSORS].timeout_s > FLOOR_WARMUP_S + 1


# --- 無効・設定 ---


def test_無効ならすべて飛ばす(paths: Paths, sensors: _Sensors, camera_found: list[str]) -> None:
    results = _run_all(paths, f"remote:\n  enabled: false\n  pin: '{HIDDEN_PIN}'\n")
    assert results == {name: CheckResult(CheckStatus.SKIPPED, REASON_DISABLED) for name in NAMES}
    assert sensors.reads == 0
    assert camera_found == []


def test_節がなければ既定の無効で飛ばす(paths: Paths) -> None:
    results = _run_all(paths, "{}\n")
    assert results[SETUP] == CheckResult(CheckStatus.SKIPPED, REASON_DISABLED)


def test_設定の誤りは_setup_がだめで_ほかは飛ばす(
    paths: Paths, sensors: _Sensors, camera_found: list[str]
) -> None:
    results = _run_all(paths, _remote("  stop_cm: 999\n"))
    assert results[SETUP] == CheckResult(CheckStatus.FAILED, REASON_CONFIG_ERROR)
    assert results[SENSORS] == CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)
    assert results[CAMERA] == CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)
    assert sensors.reads == 0
    assert camera_found == []


def test_設定ファイルがなければ設定を使えないで飛ばす(paths: Paths) -> None:
    results = _run_all(paths, None)
    assert results[SETUP] == CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)


# --- remote.setup ---


def test_pin_と_Web_の部品とポートが使えればよい(paths: Paths, free_port: int) -> None:
    assert _run(paths, SETUP, _remote("", free_port)) == OK


@pytest.mark.parametrize(
    "pin_line",
    [
        "",  # 書いていない
        "  pin: ''\n",
        "  pin: 4815\n",  # 引用符のない数は空の pin になる
        "  pin: '481'\n",
        "  pin: '481516234'\n",
        "  pin: 'abcd'\n",
        "  pin: '４８１５'\n",  # 全角の数字
    ],
)
def test_pin_が使えなければだめ(paths: Paths, pin_line: str, free_port: int) -> None:
    text = f"remote:\n  enabled: true\n  port: {free_port}\n{pin_line}"
    assert _run(paths, SETUP, text) == CheckResult(CheckStatus.FAILED, REASON_PIN_UNUSABLE)


def test_pin_の理由は引用符で囲むことを案内する() -> None:
    assert "引用符で囲んだ 4〜8 けたの数字" in REASON_PIN_UNUSABLE


def test_Web_の部品がなければだめ(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, free_port: int
) -> None:
    monkeypatch.setattr(checks, "flask_available", lambda: False)
    assert _run(paths, SETUP, _remote("", free_port)) == CheckResult(
        CheckStatus.FAILED, REASON_NO_FLASK
    )


def test_Web_の部品の有無は_flask_を探して決める(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    asked: list[str] = []

    def find_spec(name: str) -> object | None:
        asked.append(name)
        return None

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    assert checks.flask_available() is False
    assert asked == ["flask"]


def test_ポートを使えなければだめ(paths: Paths, busy_port: int) -> None:
    assert _run(paths, SETUP, _remote("", busy_port)) == CheckResult(
        CheckStatus.FAILED, REASON_PORT_UNUSABLE
    )


def test_ポートを確かめた後は閉じている(paths: Paths, free_port: int) -> None:
    assert _run(paths, SETUP, _remote("", free_port)) == OK
    # もう一度開ける（確かめたソケットを残していない）
    with socket.create_server(("0.0.0.0", free_port)):
        pass


def test_ロボットが動いているときはポートを確かめない(paths: Paths, busy_port: int) -> None:
    result = _run(paths, SETUP, _remote("", busy_port), hardware_allowed=False, service=_snapshot())
    assert result == OK


# --- remote.sensors ---


class _WarmingSensors:
    """開いてから FLOOR_WARMUP_S の間は床を読めない台本のセンサー（HatSensors と同じ）。"""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._ready_at = clock.monotonic() + FLOOR_WARMUP_S
        self.reads = 0
        self.closed = False

    def read(self) -> SensorReading:
        self.reads += 1
        if self._clock.monotonic() < self._ready_at:
            return SensorReading(distance_cm=50.0, floor=(None, None, None))
        return CLEAR_READING

    def close(self) -> None:
        self.closed = True


def test_止まっているときは床の待ちの後に読んでよい(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    made: list[_WarmingSensors] = []

    def make(given: Clock) -> SensorPort:
        made.append(_WarmingSensors(given))
        return made[-1]

    monkeypatch.setattr(checks, "sensors_available", lambda: True)
    monkeypatch.setattr(checks, "make_sensors", make)
    assert _run(paths, SENSORS, _remote("")) == OK
    (warming,) = made
    assert warming.reads >= 1
    assert warming.closed


def test_センサーは渡した時計で作り_読んだら閉じる(
    paths: Paths, sensors: _Sensors, clock: FakeClock
) -> None:
    assert _run(paths, SENSORS, _remote("")) == OK
    assert sensors.made == [clock]
    assert sensors.closed


def test_物がなければ距離が読めなくてもよい(paths: Paths, sensors: _Sensors) -> None:
    sensors.set(SensorReading(distance_cm=None, floor=(800, 700, HIDDEN_FLOOR), far=True))
    assert _run(paths, SENSORS, _remote("")) == OK


@pytest.mark.parametrize(
    "reading",
    [
        SensorReading(distance_cm=None, floor=(800, 700, HIDDEN_FLOOR)),
        SensorReading(distance_cm=30.0, floor=(800, None, HIDDEN_FLOOR)),
        SensorReading(distance_cm=30.0, floor=(None, None, None)),
        SensorReading(distance_cm=float("nan"), floor=(800, 700, HIDDEN_FLOOR)),
    ],
)
def test_距離か床のどれかが読めなければだめ(
    paths: Paths, sensors: _Sensors, reading: SensorReading
) -> None:
    sensors.set(reading)
    assert _run(paths, SENSORS, _remote("")) == CheckResult(
        CheckStatus.FAILED, REASON_SENSORS_UNREADABLE
    )
    assert sensors.closed


def test_読みの例外もだめで閉じる(paths: Paths, sensors: _Sensors) -> None:
    sensors.set(OSError(HIDDEN_PIN))
    assert _run(paths, SENSORS, _remote("")) == CheckResult(
        CheckStatus.FAILED, REASON_SENSORS_UNREADABLE
    )
    assert sensors.closed


def test_ライブラリがなければ飛ばす(
    paths: Paths, sensors: _Sensors, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checks, "sensors_available", lambda: False)
    monkeypatch.setattr(checks, "on_raspberry_pi", lambda: False)
    assert _run(paths, SENSORS, _remote("")) == CheckResult(CheckStatus.SKIPPED, REASON_NO_LIBRARY)
    assert sensors.reads == 0


def test_Pi_でライブラリがなければだめ(
    paths: Paths, sensors: _Sensors, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checks, "sensors_available", lambda: False)
    monkeypatch.setattr(checks, "on_raspberry_pi", lambda: True)
    assert _run(paths, SENSORS, _remote("")) == CheckResult(
        CheckStatus.FAILED, REASON_NO_SENSOR_PARTS
    )
    assert sensors.reads == 0


def test_Pi_での部品がない理由は部品の名前を言う() -> None:
    assert REASON_NO_SENSOR_PARTS == "センサーの部品（smbus2・lgpio）がありません"


def test_ロボットが動いているときはセンサーに触らず飛ばす(paths: Paths, sensors: _Sensors) -> None:
    result = _run(paths, SENSORS, _remote(""), hardware_allowed=False, service=_snapshot())
    assert result == CheckResult(CheckStatus.SKIPPED, REASON_SKIPPED_BUSY)
    assert sensors.reads == 0


def test_本物のセンサーは時計を渡して作る() -> None:
    from osekkai.features.remote.adapters.sensors import HatSensors

    made = checks.make_sensors(FakeClock())
    try:
        assert isinstance(made, HatSensors)
    finally:
        made.close()


# --- remote.usb_camera ---


def test_カメラのデバイスがあればよい(paths: Paths, camera_found: list[str]) -> None:
    assert _run(paths, CAMERA, _remote("")) == OK
    assert camera_found == [""]


def test_カメラの指定を見つけ方に渡す(paths: Paths, camera_found: list[str]) -> None:
    assert _run(paths, CAMERA, _remote(f"  usb_camera: {HIDDEN_CAM}\n")) == OK
    assert camera_found == [HIDDEN_CAM]


def test_カメラのデバイスがなければだめ(paths: Paths) -> None:
    # 本物の見つけ方。指定の道がなければ、ほかに倒さない
    text = _remote(f"  usb_camera: {HIDDEN_CAM}\n")
    assert _run(paths, CAMERA, text) == CheckResult(CheckStatus.FAILED, REASON_NO_CAMERA)


def test_ロボットが動いていてもカメラのデバイスは確かめる(
    paths: Paths, camera_found: list[str]
) -> None:
    result = _run(paths, CAMERA, _remote(""), hardware_allowed=False, service=_snapshot())
    assert result == OK


# --- 理由の文 ---


def test_理由は決まった文だけで値を含まない() -> None:
    reasons = (
        REASON_CONFIG_ERROR,
        REASON_CONFIG_UNUSABLE,
        REASON_DISABLED,
        REASON_NO_CAMERA,
        REASON_NO_FLASK,
        REASON_NO_LIBRARY,
        REASON_NO_SENSOR_PARTS,
        REASON_PIN_UNUSABLE,
        REASON_PORT_UNUSABLE,
        REASON_SENSORS_UNREADABLE,
    )
    assert len(set(reasons)) == len(reasons)
    for reason in reasons:
        assert reason
        assert "{" not in reason and "%" not in reason
