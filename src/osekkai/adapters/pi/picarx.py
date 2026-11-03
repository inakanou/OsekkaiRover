"""PiCar-X の体（サーボとモーター）と、ExecStopPost 用の安全停止の部品。

値は SunFounder の次のソースで確かめた:
- robot-hat 2.5.x `servo.py`: 50 Hz、周期 4095、プリスケーラ = CLOCK / FREQ / PERIOD
  （`prescaler()` が round して「値 − 1」を書く → 72e6 / 50 / 4095 ≈ 351.6 → 352）
- robot-hat 2.5.x `servo.py`: 角度を −90〜90 に収め、`mapping` で 500〜2500 µs に写し、
  値 = int(パルス幅 / 20000 × 4095)（切り捨て）
- robot-hat 2.5.x `pwm.py`: CLOCK = 72 MHz、タイマーは ch 0〜3 で共有
- picar-x 2.1.x `picarx.py`: `servo_pins=['P0', 'P1', 'P2']`（パン、チルト、ステアリング）。
  パンとチルトは `angle(-1 * (...))` で符号を反転し、ステアリングは反転しない
- picar-x 2.1.0a1 `set_motor_speed`: 速さを −100〜100 に収め、0 以外は
  デューティ int(|s|/2) + 50 %、負なら方向ピンを High。`forward(s)` は右に −s を渡す
- robot-hat 2.5.x `pwm.py` `pulse_width_percent`: 値 = int(% / 100 × 周期)
モーターの割り当ては research.md「実機で確かめた HAT の版とモーターの割り当て」（v4、pwm_dir）。
調整値と向きの補正は MotionService が足す（ここでは足さない）。
"""

from __future__ import annotations

import importlib.util
import logging
import math
from collections.abc import Callable
from typing import Final, Literal, Protocol

from osekkai.adapters.pi.gpio import Gpio
from osekkai.adapters.pi.hat import HatMcu, timer_index
from osekkai.adapters.pi.i2c_bus import I2cBus

logger = logging.getLogger(__name__)

ServoChannel = Literal["pan", "tilt", "steering"]

CLOCK_HZ: Final = 72_000_000
SERVO_FREQ_HZ: Final = 50
SERVO_PERIOD: Final = 4095
MIN_PULSE_US: Final = 500.0
MAX_PULSE_US: Final = 2500.0
FRAME_US: Final = 1_000_000 / SERVO_FREQ_HZ  # 20000 µs
MIN_ANGLE: Final = -90.0
MAX_ANGLE: Final = 90.0

SERVO_PWM: Final[dict[ServoChannel, int]] = {"pan": 0, "tilt": 1, "steering": 2}
INVERTED: Final[frozenset[ServoChannel]] = frozenset({"pan", "tilt"})


def prescaler_for(freq_hz: float, period: int, clock_hz: float = CLOCK_HZ) -> int:
    """robot-hat と同じく CLOCK / freq / period を四捨五入する。"""
    return round(clock_hz / freq_hz / period)


def angle_to_pulse_us(degrees: float) -> float:
    """−90〜90° を 500〜2500 µs に写す（範囲の外は端に収める）。"""
    if not math.isfinite(degrees):
        raise ValueError(f"servo angle must be finite: {degrees}")
    a = min(MAX_ANGLE, max(MIN_ANGLE, degrees))
    return MIN_PULSE_US + (a - MIN_ANGLE) * (MAX_PULSE_US - MIN_PULSE_US) / (MAX_ANGLE - MIN_ANGLE)


def pulse_us_to_value(pulse_us: float, period: int = SERVO_PERIOD) -> int:
    """パルス幅をチャンネルの値にする（robot-hat と同じく切り捨て）。"""
    p = min(MAX_PULSE_US, max(MIN_PULSE_US, pulse_us))
    return int(p / FRAME_US * period)


class PiCarXServos:
    """ServoPort の実機版。作るときに P0〜P2 のタイマーを 50 Hz にする（OSError はそのまま）。"""

    def __init__(self, hat: HatMcu) -> None:
        self._hat = hat
        prescaler = prescaler_for(SERVO_FREQ_HZ, SERVO_PERIOD)
        with hat.bus.lock:
            # P0〜P3 は timer 0 を共有するので、タイマーごとに 1 回だけ書く
            first_of_timer = {timer_index(c): c for c in sorted(SERVO_PWM.values(), reverse=True)}
            for ch in sorted(first_of_timer.values()):
                hat.set_period(ch, SERVO_PERIOD)  # robot-hat と同じく周期が先
                hat.set_prescaler(ch, prescaler)

    def set_angle(self, channel: ServoChannel, degrees: float) -> None:
        pwm = SERVO_PWM.get(channel)
        if pwm is None:
            raise ValueError(f"unknown servo channel: {channel}")
        angle = -degrees if channel in INVERTED else degrees
        self._hat.set_channel_value(pwm, pulse_us_to_value(angle_to_pulse_us(angle)))


# ---- モーター ----

MotorDriver = Literal["auto", "pwm_dir", "dual_pwm"]

MOTOR_PERIOD: Final = 4095
MOTOR_PRESCALER: Final = 10
LEFT_PWM: Final = 13  # P13
RIGHT_PWM: Final = 12  # P12
LEFT_DIR_PIN: Final = 23  # D4（BCM）
RIGHT_DIR_PIN: Final = 24  # D5（BCM）
MAX_SPEED: Final = 100
STOP_LOCK_TIMEOUT: Final = 0.05  # 最速の停止がロックを待つ上限（秒）

# 安全停止で 0 にする PWM（P12〜P15 は 1 つのタイマーを共有）と、Low にする方向ピン
SAFE_STOP_PWM: Final[tuple[int, ...]] = (12, 13, 14, 15)
SAFE_STOP_DIR_PINS: Final[tuple[int, ...]] = (LEFT_DIR_PIN, RIGHT_DIR_PIN)


class MotorMappingUnverified(RuntimeError):
    """実機で確かめていないモーターの割り当て（v5、PWM 2 本）。"""


class _OutputPins(Protocol):
    def claim_output(self, pin: int, level: int) -> None: ...
    def write(self, pin: int, level: int) -> None: ...


def speed_to_duty(speed: int) -> tuple[int, bool]:
    """速さ −100〜100 を（デューティの %、方向ピンを High にするか）にする。"""
    s = min(MAX_SPEED, max(-MAX_SPEED, int(speed)))
    if s == 0:
        return 0, False
    return int(abs(s) / 2) + 50, s < 0


def duty_to_value(percent: int, period: int = MOTOR_PERIOD) -> int:
    """デューティの % をチャンネルの値にする（robot-hat と同じく切り捨て）。"""
    return int(percent / 100.0 * period)


def resolve_motor_mode(driver: MotorDriver, hat: HatMcu) -> Literal["pwm_dir"]:
    """方式を決める。確かめていない方式は MotorMappingUnverified で断る。"""
    if driver not in ("auto", "pwm_dir", "dual_pwm"):
        raise ValueError(f"unknown motor driver: {driver!r}")
    if driver == "dual_pwm":
        raise MotorMappingUnverified("dual_pwm motor mapping is unverified")
    if driver == "auto" and hat.hat_version() == "v5":
        raise MotorMappingUnverified("v5 motor mapping is unverified")
    return "pwm_dir"


class PiCarXMotors:
    """MotorPort の実機版（pwm_dir）。左は P13＋D4、右は P12＋D5。右は反転して付いている。"""

    def __init__(self, hat: HatMcu, pins: _OutputPins, *, motor_driver: MotorDriver) -> None:
        self.mode = resolve_motor_mode(motor_driver, hat)
        self._hat = hat
        self._pins = pins
        # (PWM, 方向ピン, 反転するか)
        self._motors = ((LEFT_PWM, LEFT_DIR_PIN, False), (RIGHT_PWM, RIGHT_DIR_PIN, True))
        with hat.bus.lock:
            # P12 と P13 は同じタイマー。robot-hat と同じく周期が先
            hat.set_period(LEFT_PWM, MOTOR_PERIOD)
            hat.set_prescaler(LEFT_PWM, MOTOR_PRESCALER)
            for pwm, pin, _ in self._motors:
                pins.claim_output(pin, 0)
                hat.set_channel_value(pwm, 0)

    @property
    def channels(self) -> tuple[int, ...]:
        """この方式で使う PWM のチャンネル。"""
        return tuple(pwm for pwm, _, _ in self._motors)

    def set_speeds(self, left: int, right: int) -> None:
        """正は両輪とも前進。書き込みの失敗は OSError のまま投げる。"""
        with self._hat.bus.lock:
            for (pwm, pin, reversed_), speed in zip(self._motors, (left, right), strict=True):
                percent, high = speed_to_duty(-speed if reversed_ else speed)
                self._pins.write(pin, 1 if high else 0)  # picar-x と同じく向きが先
                self._hat.set_channel_value(pwm, duty_to_value(percent))

    def stop_now(self) -> None:
        """ロックを 50 ms まで待ち、取れなくても PWM に 0 を書く（シグナルハンドラ用）。"""
        # 書き込みは取れても取れなくても I2C_RDWR の 1 回の転送にする。メインのスレッドの
        # 送る途中の転送に割り込んでも、共有の fd の相手のアドレスを変えないため
        lock = self._hat.bus.lock
        got = lock.acquire(timeout=STOP_LOCK_TIMEOUT)
        try:
            _zero_all(self._hat.set_channel_value_emergency, self.channels)
        finally:
            if got:
                lock.release()


def _zero_all(write: Callable[[int, int], None], channels: tuple[int, ...]) -> None:
    """すべてのチャンネルに 0 を書く。途中で失敗しても残りを書き、最初の失敗を投げる。"""
    first: Exception | None = None
    for ch in channels:
        try:
            write(ch, 0)
        except Exception as error:
            first = first or error
    if first is not None:
        raise first


# ---- ExecStopPost 用の安全停止 ----


class _Pins(_OutputPins, Protocol):
    def close(self) -> None: ...


class _NoPins:
    """GPIO が開けなかったときの代わり。I2C だけは使えるようにする。"""

    def claim_output(self, pin: int, level: int) -> None:
        raise OSError("GPIO is not available")

    def write(self, pin: int, level: int) -> None:
        raise OSError("GPIO is not available")


class PiSafeStop:
    """設定を読まずにモーターを止める部品。GPIO とマイコンは使うときに開く。"""

    def __init__(
        self,
        open_gpio: Callable[[], _Pins],
        open_hat: Callable[[_OutputPins], HatMcu],
    ) -> None:
        self._open_gpio = open_gpio
        self._open_hat = open_hat
        self._gpio: _Pins | None = None
        self._gpio_error: Exception | None = None
        self._hat: HatMcu | None = None

    def _get_gpio(self) -> _Pins:
        if self._gpio is None:
            if self._gpio_error is not None:
                raise self._gpio_error
            try:
                self._gpio = self._open_gpio()
            except Exception as error:
                self._gpio_error = error
                raise
        return self._gpio

    def _get_hat(self) -> HatMcu:
        if self._hat is None:
            pins: _OutputPins
            try:
                pins = self._get_gpio()
            except Exception:
                pins = _NoPins()  # リセットはできないが、PWM は書ける
            self._hat = self._open_hat(pins)
        return self._hat

    def zero_motor_pwm(self) -> None:
        hat = self._get_hat()
        with hat.bus.lock:
            _zero_all(hat.set_channel_value, SAFE_STOP_PWM)

    def direction_pins_low(self) -> None:
        gpio = self._get_gpio()
        first: Exception | None = None
        for pin in SAFE_STOP_DIR_PINS:
            try:
                gpio.claim_output(pin, 0)
                gpio.write(pin, 0)
            except Exception as error:
                first = first or error
        if first is not None:
            raise first

    def reset_mcu(self) -> None:
        self._get_hat().reset()

    def close(self) -> None:
        if self._hat is not None:
            try:
                self._hat.bus.close()
            except Exception as error:
                logger.warning("I2C bus close failed: %s", type(error).__name__)
        if self._gpio is not None:
            try:
                self._gpio.close()
            except Exception as error:
                logger.warning("gpiochip close failed: %s", type(error).__name__)


def open_safe_stop_hardware() -> PiSafeStop:
    """実機の安全停止の部品。lgpio か smbus2 がなければ ImportError（PC）。"""
    for lib in ("lgpio", "smbus2"):
        if importlib.util.find_spec(lib) is None:
            raise ImportError(f"{lib} is not installed")
    bus = I2cBus()
    return PiSafeStop(Gpio, lambda pins: HatMcu(bus, pins))
