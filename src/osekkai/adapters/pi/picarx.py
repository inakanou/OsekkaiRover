"""PiCar-X の体（サーボ。モーターは 7.6 で別のクラスとして足す）。

値は SunFounder の次のソースで確かめた:
- robot-hat 2.5.x `servo.py`: 50 Hz、周期 4095、プリスケーラ = CLOCK / FREQ / PERIOD
  （`prescaler()` が round して「値 − 1」を書く → 72e6 / 50 / 4095 ≈ 351.6 → 352）
- robot-hat 2.5.x `servo.py`: 角度を −90〜90 に収め、`mapping` で 500〜2500 µs に写し、
  値 = int(パルス幅 / 20000 × 4095)（切り捨て）
- robot-hat 2.5.x `pwm.py`: CLOCK = 72 MHz、タイマーは ch 0〜3 で共有
- picar-x 2.1.x `picarx.py`: `servo_pins=['P0', 'P1', 'P2']`（パン、チルト、ステアリング）。
  パンとチルトは `angle(-1 * (...))` で符号を反転し、ステアリングは反転しない
調整値は MotionService が足す（ここでは足さない）。
"""

from __future__ import annotations

import math
from typing import Final, Literal

from osekkai.adapters.pi.hat import HatMcu, timer_index

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
