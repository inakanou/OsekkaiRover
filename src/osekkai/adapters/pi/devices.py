"""実機のデバイス一式を、1 つずつ失敗を隔離して組み立てる（1.6）。

準備で例外が出たデバイスは `Unavailable(名前, 理由)` にして、残りを続ける。
理由は例外の型の名前だけにする（メッセージにはパスや設定の値が入りうる）。
使えないデバイスのログは Runtime が起動で 1 回だけ出すので、ここでは出さない。
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, TypeVar

from osekkai.adapters.pi.gpio import Gpio, HatButtons, HatLed
from osekkai.adapters.pi.gpio import enable_speaker as pinctrl_enable_speaker
from osekkai.adapters.pi.hat import HatMcu, HatVersion, read_hat_version
from osekkai.adapters.pi.i2c_bus import I2cBus
from osekkai.adapters.pi.lcd1602 import Lcd1602Display
from osekkai.adapters.pi.openjtalk import OpenJTalkSynth
from osekkai.adapters.pi.picarx import (
    MotorDriver,
    MotorMappingUnverified,
    PiCarXMotors,
    PiCarXServos,
)
from osekkai.adapters.pi.pipewire import PaplayPlayer
from osekkai.adapters.pi.timesync import TimedatectlTimeSync
from osekkai.core.ports import (
    AudioPlayerPort,
    ButtonPort,
    Devices,
    DisplayPort,
    LedPort,
    MotorPort,
    ServoPort,
    SpeechSynthPort,
    TimeSyncPort,
    Unavailable,
)
from osekkai.core.types import Glyph

if TYPE_CHECKING:
    from osekkai.core.config import CoreConfig

logger = logging.getLogger(__name__)

I2C_BUS_NUMBER: Final = 1
HARDWARE_LIBS: Final[tuple[str, ...]] = ("smbus2", "lgpio")

NOT_NEEDED: Final = "not needed"

T = TypeVar("T")
Glyphs = Mapping[Glyph, Sequence[int]]
SpeakerPin = Literal["auto"] | int


class _NoGpio:
    """GPIO が開けなかったときにマイコンへ渡す代わり。PWM は書けるがリセットはできない。"""

    def claim_output(self, pin: int, level: int) -> None:
        raise OSError("GPIO is not available")

    def write(self, pin: int, level: int) -> None:
        raise OSError("GPIO is not available")


class _PiHat:
    """HatPort。マイコンのリセットでタイマーの設定が消えるので、リセットの後に設定し直す。"""

    def __init__(self, mcu: HatMcu) -> None:
        self._mcu = mcu
        self._reconfigure: list[Callable[[], object]] = []

    def add_reconfigure(self, step: Callable[[], object]) -> None:
        self._reconfigure.append(step)

    def reset(self) -> None:
        self._mcu.reset()
        first: Exception | None = None
        for step in self._reconfigure:
            try:
                step()
            except Exception as error:
                first = first or error
        if first is not None:
            raise first


class _NeverSynchronized:
    """時刻の同期を確かめる口が作れない・要らないときの代わり（確かでない側に倒す）。"""

    def is_synchronized(self) -> bool:
        return False


def _has_library(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _default_display(bus: I2cBus, glyphs: Glyphs, address: int) -> DisplayPort:
    return Lcd1602Display(bus, glyphs, address=address)


def _default_buttons(gpio: Gpio, extra_pin: str | None) -> ButtonPort:
    return HatButtons(gpio, extra_pin=extra_pin)


def _default_motors(hat: HatMcu, gpio: Gpio, motor_driver: MotorDriver) -> MotorPort:
    return PiCarXMotors(hat, gpio, motor_driver=motor_driver)


@dataclass(frozen=True)
class PiFactories:
    """部品とデバイスの作り方（テストで差し替える）。"""

    i2c_bus: Callable[[], I2cBus] = lambda: I2cBus(I2C_BUS_NUMBER)
    gpio: Callable[[], Gpio] = Gpio
    hat_mcu: Callable[[I2cBus, Gpio | _NoGpio], HatMcu] = HatMcu
    hat_version: Callable[[], HatVersion] = read_hat_version
    display: Callable[[I2cBus, Glyphs, int], DisplayPort] = _default_display
    buttons: Callable[[Gpio, str | None], ButtonPort] = _default_buttons
    led: Callable[[Gpio], LedPort] = HatLed
    servos: Callable[[HatMcu], ServoPort] = PiCarXServos
    motors: Callable[[HatMcu, Gpio, MotorDriver], MotorPort] = _default_motors
    enable_speaker: Callable[[HatVersion, SpeakerPin], object] = pinctrl_enable_speaker
    synth: Callable[[], SpeechSynthPort] = OpenJTalkSynth
    player: Callable[[], AudioPlayerPort] = PaplayPlayer
    timesync: Callable[[], TimeSyncPort] = TimedatectlTimeSync
    has_library: Callable[[str], bool] = _has_library


def unavailable_reason(error: BaseException) -> str:
    """ログに出してよい短い理由。決まった文のものだけ文を使い、ほかは型の名前にする。"""
    if isinstance(error, MotorMappingUnverified):
        return str(error)  # "v5 motor mapping is unverified" など（値もパスも入らない）
    return type(error).__name__


def _prepare(name: str, make: Callable[[], T]) -> T | Unavailable:
    try:
        return make()
    except Exception as error:
        return Unavailable(device=name, reason=unavailable_reason(error))


def _needs(name: str, part: object, make: Callable[[], T]) -> T | Unavailable:
    """共有の部品 part が使えなければ、その理由でこのデバイスも使えない。"""
    if isinstance(part, Unavailable):
        return Unavailable(device=name, reason=part.reason)
    return _prepare(name, make)


def build_pi_devices(
    core_cfg: CoreConfig, glyphs: Glyphs, *, factories: PiFactories | None = None
) -> Devices:
    """実機のデバイス一式を作る。

    ハードのライブラリ（smbus2 と lgpio）がどちらもなければ実機ではないので ImportError。
    """
    f = _factories_on_pi(factories)
    hw = core_cfg.hardware

    # 共有の部品: I2C のバス 1 と gpiochip を 1 つずつ
    bus = _prepare("i2c", f.i2c_bus)
    gpio = _prepare("gpio", f.gpio)

    # マイコンは GPIO がなくても PWM を書けるように作る（リセットだけできない）
    pins: Gpio | _NoGpio = _NoGpio() if isinstance(gpio, Unavailable) else gpio
    mcu = _needs("hat", bus, lambda: f.hat_mcu(_part(bus), pins))

    display = _needs("display", bus, lambda: f.display(_part(bus), glyphs, hw.lcd_address))
    buttons = _needs("buttons", gpio, lambda: f.buttons(_part(gpio), hw.extra_button_pin))
    led = _needs("led", gpio, lambda: f.led(_part(gpio)))
    servos = _needs("servos", mcu, lambda: f.servos(_part(mcu)))
    motors_part: object = mcu if isinstance(mcu, Unavailable) else gpio
    motors = _needs(
        "motors", motors_part, lambda: f.motors(_part(mcu), _part(gpio), hw.motor_driver)
    )

    hat: _PiHat | Unavailable
    if isinstance(mcu, Unavailable):
        hat = mcu
    elif isinstance(gpio, Unavailable):
        hat = Unavailable(device="hat", reason=gpio.reason)  # リセットに GPIO が要る
    else:
        hat = _PiHat(mcu)
        if not isinstance(servos, Unavailable):
            hat.add_reconfigure(lambda: f.servos(mcu))
        if not isinstance(motors, Unavailable):
            hat.add_reconfigure(lambda: f.motors(mcu, gpio, hw.motor_driver))

    _turn_on_speaker(f, mcu, hw.speaker_enable_pin)

    synth = _prepare("synth", f.synth)
    player = _prepare("player", f.player)
    timesync = _prepare("timesync", f.timesync)
    if isinstance(timesync, Unavailable):
        logger.warning("時刻の同期を確かめられません: %s", timesync.reason)
        timesync = _NeverSynchronized()

    return Devices(
        display=display,
        led=led,
        buttons=buttons,
        servos=servos,
        motors=motors,
        synth=synth,
        player=player,
        timesync=timesync,
        hat=hat,
    )


def build_pi_voice_devices(
    core_cfg: CoreConfig, *, factories: PiFactories | None = None
) -> Devices:
    """話して終わる（`--say`）ための、声のデバイスだけを作る（13.5）。

    I2C・GPIO・マイコン・LCD には触らない。スピーカーのアンプを有効にし、声の合成と再生だけを作る。
    ほかのデバイスは `Unavailable(名前, "not needed")` にする。
    """
    f = _factories_on_pi(factories)
    # 版はデバイスツリーから読む（マイコンを探さない）
    _turn_on_speaker(
        f, Unavailable(device="hat", reason=NOT_NEEDED), core_cfg.hardware.speaker_enable_pin
    )
    return Devices(
        display=Unavailable(device="display", reason=NOT_NEEDED),
        led=Unavailable(device="led", reason=NOT_NEEDED),
        buttons=Unavailable(device="buttons", reason=NOT_NEEDED),
        servos=Unavailable(device="servos", reason=NOT_NEEDED),
        motors=Unavailable(device="motors", reason=NOT_NEEDED),
        synth=_prepare("synth", f.synth),
        player=_prepare("player", f.player),
        timesync=_NeverSynchronized(),
        hat=Unavailable(device="hat", reason=NOT_NEEDED),
    )


def _factories_on_pi(factories: PiFactories | None) -> PiFactories:
    # ハードのライブラリ（smbus2 と lgpio）がどちらもなければ実機ではない
    f = factories or PiFactories()
    if not any(f.has_library(lib) for lib in HARDWARE_LIBS):
        raise ImportError("hardware libraries are not installed")
    return f


def _part(value: T | Unavailable) -> T:
    # needs() で使えないときは呼ばれないので、ここに Unavailable は来ない
    assert not isinstance(value, Unavailable)
    return value


def _turn_on_speaker(f: PiFactories, mcu: HatMcu | Unavailable, configured: SpeakerPin) -> None:
    """アンプを有効にする。失敗しても声のデバイスは残す（前の起動で High のままかもしれない）。"""
    try:
        version = f.hat_version() if isinstance(mcu, Unavailable) else mcu.hat_version()
        f.enable_speaker(version, configured)
    except Exception as error:
        logger.warning("スピーカーを有効にできませんでした: %s", type(error).__name__)
