"""実機のデバイス一式の組み立て（1.6）のテスト。

部品とデバイスの作り方を差し替えて、どれかが例外を出してもほかが残ることを確かめる。
smbus2 と lgpio は読み込まない。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import fields, replace
from pathlib import Path
from typing import Any, cast

import pytest

from osekkai.adapters.pi import PiFactories, build_pi_devices
from osekkai.adapters.pi.gpio import Gpio
from osekkai.adapters.pi.hat import HatMcu
from osekkai.adapters.pi.i2c_bus import I2cBus
from osekkai.adapters.pi.picarx import (
    MOTOR_PERIOD,
    MOTOR_PRESCALER,
    MotorMappingUnverified,
    PiCarXMotors,
    PiCarXServos,
)
from osekkai.core.config import CoreConfig, HardwareSettings
from osekkai.core.lcd_text import GLYPH_PATTERNS
from osekkai.core.ports import Devices, Unavailable

DEVICE_NAMES = ("display", "led", "buttons", "servos", "motors", "synth", "player", "hat")


class Part:
    """作られた部品・デバイスの身代わり。作られたときの引数を持つ。"""

    def __init__(self, kind: str, *args: object) -> None:
        self.kind = kind
        self.args = args

    def reset(self) -> None:
        pass

    def hat_version(self) -> str:
        return "v4"

    def is_synchronized(self) -> bool:
        return True


def _raises(error: BaseException) -> Callable[..., Any]:
    def make(*args: object) -> Any:
        raise error

    return make


def _part(kind: str) -> Callable[..., Any]:
    return lambda *args: Part(kind, *args)


def _factories(speaker_calls: list[tuple[object, ...]] | None = None) -> PiFactories:
    calls = speaker_calls if speaker_calls is not None else []
    return PiFactories(
        i2c_bus=_part("i2c"),
        gpio=_part("gpio"),
        hat_mcu=_part("mcu"),
        hat_version=lambda: "v4",
        display=_part("display"),
        buttons=_part("buttons"),
        led=_part("led"),
        servos=_part("servos"),
        motors=_part("motors"),
        enable_speaker=lambda *args: calls.append(args),
        synth=_part("synth"),
        player=_part("player"),
        timesync=_part("timesync"),
        has_library=lambda name: True,
    )


def _build(factories: PiFactories, core: CoreConfig | None = None) -> Devices:
    return build_pi_devices(core or CoreConfig(), GLYPH_PATTERNS, factories=factories)


def _unavailable(devices: Devices) -> dict[str, str]:
    found: dict[str, str] = {}
    for field in fields(Devices):
        value = getattr(devices, field.name)
        if isinstance(value, Unavailable):
            assert value.device == field.name
            found[field.name] = value.reason
    return found


# ---- すべて準備できる ----


def test_すべて準備できればすべて使える() -> None:
    devices = _build(_factories())
    assert _unavailable(devices) == {}
    assert devices.timesync.is_synchronized()


def test_設定の配線と絵の文字の表をデバイスに渡す() -> None:
    core = CoreConfig(
        hardware=HardwareSettings(lcd_address=0x3F, extra_button_pin="D0", motor_driver="pwm_dir")
    )
    devices = _build(_factories(), core)
    display = cast(Part, devices.display)
    assert display.args[1] is GLYPH_PATTERNS
    assert display.args[2] == 0x3F
    assert cast(Part, devices.buttons).args[1] == "D0"
    assert cast(Part, devices.motors).args[2] == "pwm_dir"
    # 共有の部品は 1 つずつ
    bus = display.args[0]
    mcu = cast(Part, devices.servos).args[0]
    assert cast(Part, mcu).args[0] is bus
    assert cast(Part, devices.led).args[0] is cast(Part, devices.buttons).args[0]


# ---- 1 つの失敗を隔離する ----


@pytest.mark.parametrize(
    ("factory", "device", "error", "reason"),
    [
        ("display", "display", OSError(121, "Remote I/O error", "/dev/i2c-1"), "OSError"),
        ("buttons", "buttons", ValueError("unknown pin name: 'D99'"), "ValueError"),
        ("led", "led", RuntimeError("GPIO busy"), "RuntimeError"),
        ("servos", "servos", OSError(5, "Input/output error"), "OSError"),
        ("motors", "motors", OSError(5, "Input/output error"), "OSError"),
        ("synth", "synth", RuntimeError("x"), "RuntimeError"),
        ("player", "player", FileNotFoundError("/usr/bin/paplay"), "FileNotFoundError"),
    ],
)
def test_準備で例外が出たデバイスだけを使えないにして残りを続ける(
    factory: str, device: str, error: Exception, reason: str
) -> None:
    devices = _build(replace(_factories(), **{factory: _raises(error)}))
    assert _unavailable(devices) == {device: reason}


def test_LCD_がなくてもほかのデバイスで続ける() -> None:
    lcd_missing = OSError(121, "Remote I/O error")
    devices = _build(replace(_factories(), display=_raises(lcd_missing)))
    assert devices.display == Unavailable(device="display", reason="OSError")
    for name in DEVICE_NAMES:
        if name != "display":
            assert not isinstance(getattr(devices, name), Unavailable), name


def test_理由に設定の値やパスを入れない() -> None:
    secret = "/home/someone/.config/osekkai/config.yaml 0x3f D99"
    factories = _factories()
    for name in ("display", "buttons", "led", "servos", "motors", "synth", "player"):
        factories = replace(factories, **{name: _raises(OSError(2, secret, secret))})
    devices = _build(factories)
    reasons = _unavailable(devices)
    assert set(reasons) == {"display", "buttons", "led", "servos", "motors", "synth", "player"}
    for reason in reasons.values():
        assert reason == "FileNotFoundError"  # errno 2 の OSError


# ---- 共有の部品の失敗 ----


def test_GPIO_が開けなければボタンとLEDとモーターとリセットは使えないがI2Cの方は続ける() -> None:
    devices = _build(replace(_factories(), gpio=_raises(ModuleNotFoundError("lgpio"))))
    reason = "ModuleNotFoundError"
    assert _unavailable(devices) == {
        "buttons": reason,
        "led": reason,
        "motors": reason,
        "hat": reason,
    }
    # マイコンは GPIO の代わり（リセットできないもの）で作り、サーボは使える
    assert isinstance(devices.servos, Part)
    assert isinstance(devices.display, Part)


def test_GPIO_がないマイコンは書けるがリセットのピンは断る() -> None:
    made: list[Any] = []

    def mcu(bus: object, pins: Any) -> Any:
        made.append(pins)
        return Part("mcu", bus, pins)

    _build(replace(_factories(), gpio=_raises(OSError()), hat_mcu=mcu))
    with pytest.raises(OSError):
        made[0].claim_output(5, 1)
    with pytest.raises(OSError):
        made[0].write(5, 0)


def test_HAT_のマイコンが応えなければサーボとモーターとHATは使えない() -> None:
    devices = _build(replace(_factories(), hat_mcu=_raises(OSError("no answer"))))
    assert _unavailable(devices) == {"servos": "OSError", "motors": "OSError", "hat": "OSError"}
    assert isinstance(devices.display, Part)
    assert isinstance(devices.buttons, Part)
    assert isinstance(devices.led, Part)


def test_I2C_のバスが作れなければI2Cのデバイスはすべて使えない() -> None:
    devices = _build(replace(_factories(), i2c_bus=_raises(PermissionError("/dev/i2c-1"))))
    reason = "PermissionError"
    assert _unavailable(devices) == {
        "display": reason,
        "servos": reason,
        "motors": reason,
        "hat": reason,
    }
    assert isinstance(devices.buttons, Part)


# ---- モーターの方式 ----


def test_確かめていないモーターの割り当てはその理由で使えない() -> None:
    error = MotorMappingUnverified("v5 motor mapping is unverified")
    devices = _build(replace(_factories(), motors=_raises(error)))
    assert devices.motors == Unavailable(device="motors", reason="v5 motor mapping is unverified")
    assert isinstance(devices.servos, Part)


# ---- スピーカー ----


def test_スピーカーを_HAT_の版と設定のピンで有効にする() -> None:
    calls: list[tuple[object, ...]] = []
    core = CoreConfig(hardware=HardwareSettings(speaker_enable_pin=20))
    _build(_factories(calls), core)
    assert calls == [("v4", 20)]


def test_マイコンがなくても版を読んでスピーカーを有効にする() -> None:
    calls: list[tuple[object, ...]] = []
    factories = replace(_factories(calls), hat_mcu=_raises(OSError()), hat_version=lambda: "v5")
    _build(factories)
    assert calls == [("v5", "auto")]


def test_スピーカーを有効にできなくても声のデバイスは残し警告だけ出す(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="osekkai")
    secret = "pinctrl failed /usr/bin/pinctrl 20"
    devices = _build(replace(_factories(), enable_speaker=_raises(OSError(secret))))
    assert isinstance(devices.synth, Part)
    assert isinstance(devices.player, Part)
    assert "OSError" in caplog.text
    assert secret not in caplog.text


# ---- 時刻の同期 ----


def test_時刻の同期の口が作れなくても確かでない側の口を置く() -> None:
    devices = _build(replace(_factories(), timesync=_raises(RuntimeError())))
    assert devices.timesync.is_synchronized() is False
    assert "timesync" not in _unavailable(devices)


# ---- ログ ----


def test_使えないデバイスのログは組み立てでは出さない(caplog: pytest.LogCaptureFixture) -> None:
    # Runtime が起動で 1 回だけ出す（二重に出さない）
    caplog.set_level(logging.DEBUG, logger="osekkai")
    _build(replace(_factories(), display=_raises(OSError(2, "x", "/dev/i2c-1"))))
    assert "display" not in caplog.text
    assert "/dev/i2c-1" not in caplog.text


# ---- 実機でない ----


def test_ハードのライブラリがどちらもなければ_ImportError() -> None:
    with pytest.raises(ImportError):
        _build(replace(_factories(), has_library=lambda name: False))


def test_ライブラリが片方だけなければ使えないデバイスにして続ける() -> None:
    factories = replace(
        _factories(),
        has_library=lambda name: name == "smbus2",
        gpio=_raises(ModuleNotFoundError("lgpio")),
    )
    devices = _build(factories)
    assert isinstance(devices.display, Part)
    assert devices.led == Unavailable(device="led", reason="ModuleNotFoundError")


# ---- HAT のリセットの後の設定し直し（本物のマイコンと偽の smbus） ----


class FakeSMBus:
    def __init__(self) -> None:
        self.writes: list[tuple[int, int]] = []

    def write_quick(self, addr: int) -> None:
        if addr != 0x14:
            raise OSError(121, "Remote I/O error")

    def write_byte(self, addr: int, value: int) -> None:
        pass

    def write_i2c_block_data(self, addr: int, reg: int, data: list[int]) -> None:
        self.writes.append((reg, (data[0] << 8) | data[1]))

    def close(self) -> None:
        pass


class FakePins:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int]] = []

    def claim_output(self, pin: int, level: int) -> None:
        self.events.append(("claim", pin, level))

    def write(self, pin: int, level: int) -> None:
        self.events.append(("write", pin, level))


def test_HAT_のリセットの後にサーボとモーターのタイマーを設定し直す(tmp_path: Path) -> None:
    smbus = FakeSMBus()
    pins = FakePins()
    bus = I2cBus(opener=lambda n: smbus)

    def mcu(b: I2cBus, p: Any) -> HatMcu:
        return HatMcu(b, p, sleep=lambda s: None, device_tree=tmp_path)

    factories = replace(
        _factories(),
        i2c_bus=lambda: bus,
        gpio=lambda: cast(Gpio, pins),
        hat_mcu=mcu,
        servos=PiCarXServos,
        motors=lambda h, g, d: PiCarXMotors(h, g, motor_driver=d),
    )
    devices = _build(factories, CoreConfig(hardware=HardwareSettings(motor_driver="pwm_dir")))
    assert isinstance(devices.servos, PiCarXServos)
    assert isinstance(devices.motors, PiCarXMotors)
    hat = devices.hat
    assert not isinstance(hat, Unavailable)

    smbus.writes.clear()
    pins.events.clear()
    hat.reset()
    # リセットのピン（BCM 5）を Low→High にしてから、タイマーを書き直す
    assert ("write", 5, 0) in pins.events
    assert ("write", 5, 1) in pins.events
    assert (0x44 + 0, 4095) in smbus.writes  # サーボ（timer 0）の周期
    assert (0x44 + 3, MOTOR_PERIOD) in smbus.writes  # モーター（timer 3）の周期
    assert (0x40 + 3, MOTOR_PRESCALER - 1) in smbus.writes
    assert (0x20 + 12, 0) in smbus.writes  # モーターは 0 のまま
    assert (0x20 + 13, 0) in smbus.writes


def test_設定し直しに失敗してもリセットの後の残りを書き最初の失敗を投げる() -> None:
    order: list[str] = []
    servos_made = 0

    def servos(mcu: object) -> Any:
        nonlocal servos_made
        servos_made += 1
        if servos_made > 1:
            order.append("servos")
            raise OSError("write failed")
        return Part("servos", mcu)

    def motors(*args: object) -> Any:
        order.append("motors")
        return Part("motors", *args)

    devices = _build(replace(_factories(), servos=servos, motors=motors))
    order.clear()
    hat = devices.hat
    assert not isinstance(hat, Unavailable)
    with pytest.raises(OSError):
        hat.reset()
    assert order == ["servos", "motors"]
