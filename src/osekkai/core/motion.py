"""首と走行の操作（8.1〜8.5、2.1）。

角度は「範囲に収めてから、調整値（正面のずれ）を足す」。速さは範囲に収めてから、
モーターの向き（1 か −1）を掛ける。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal

from osekkai.core.ports import MotorPort, ServoPort, Unavailable
from osekkai.core.types import DeviceUnavailable, MotionAborted

if TYPE_CHECKING:
    from osekkai.core.clock import Clock
    from osekkai.core.config import MotionSettings

logger = logging.getLogger(__name__)

Channel = Literal["pan", "tilt", "steering"]

STEERING_LIMIT = 30.0
SPEED_LIMIT = 100
NOD_STEP = 0.25
LOOK_AROUND_STEP = 0.35
DANCE_STEP = 0.25
DANCE_BEATS = 6
DANCE_SPEED = 20
EMERGENCY_RETRIES = 2


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class MotionService:
    """首と走行の操作。

    ふつうの指令はロックで 1 つずつにする。段階のある動きは段階ごとにロックを取り、
    待つ間はロックを放す（ほかの持ち主の停止や走行を待たせない）。
    """

    def __init__(
        self,
        servos: ServoPort | Unavailable,
        motors: MotorPort | Unavailable,
        settings: MotionSettings,
        clock: Clock,
    ) -> None:
        self._servos = servos
        self._motors = motors
        self._settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        # 最後に送った速さが 0 でないか。ロックを取らずに読む（状態のファイルから読むため）
        self._driving = False

    @property
    def driving(self) -> bool:
        """最後に送った速さ（範囲に収めた後）が 0 でないか。"""
        return self._driving

    # --- 首 ---

    def look(self, pan: float, tilt: float) -> None:
        with self._lock:
            self._look(pan, tilt)

    def home(self) -> None:
        with self._lock:
            self._home()

    def home_neck(self) -> None:
        """首だけを正面に戻す。ステアリング（走行の側）には触らない（4.8）。"""
        with self._lock:
            self._home_neck()

    def nod(self, times: int, should_continue: Callable[[], bool]) -> None:
        for _ in range(times):
            for tilt in (-20.0, 10.0):
                with self._lock:
                    self._check(should_continue)
                    self._set("tilt", tilt)
                self._clock.sleep(NOD_STEP)
        with self._lock:
            self._home_neck()

    def look_around(self, should_continue: Callable[[], bool]) -> None:
        for pan in (-30.0, 30.0, 0.0):
            with self._lock:
                self._check(should_continue)
                self._set("pan", pan)
            self._clock.sleep(LOOK_AROUND_STEP)

    def sleepy(self) -> None:
        with self._lock:
            self._look(0.0, -30.0)

    def dance(self, *, drive: bool, should_continue: Callable[[], bool]) -> None:
        """drive は、呼び出し元が走行（DRIVE）も借りているか。

        走行を借りていなければ首だけを動かし、ステアリングには触らない（4.1、4.9）。
        タイヤは、走行を借りていて、設定の許可もあるときだけ回す（8.3）。
        """
        use_wheels = drive and self._settings.allow_drive_in_dance
        for beat in range(DANCE_BEATS):
            with self._lock:
                self._check(should_continue)
                sign = 1.0 if beat % 2 == 0 else -1.0
                self._set("pan", 30.0 * sign)
                if drive:
                    self._set("steering", -25.0 * sign)
                self._set("tilt", 15.0)
                if use_wheels:
                    self._speeds(int(DANCE_SPEED * sign))
            self._clock.sleep(DANCE_STEP)
        with self._lock:
            # タイヤを回したときだけ止める（ほかの持ち主の走行を止めない）
            if use_wheels:
                self._speeds(0)
            if drive:
                self._home()
            else:
                self._home_neck()

    # --- 走行 ---

    def drive(self, steering: float, speed: int) -> None:
        with self._lock:
            self._set("steering", steering)
            self._speeds(speed)

    def stop(self) -> None:
        with self._lock:
            self._speeds(0)

    def emergency_stop(self) -> None:
        """ロックを待たずにモーターを止める。何度呼んでもよく、例外を外に出さない。"""
        motors = self._motors
        if isinstance(motors, Unavailable):
            logger.debug("モーターが使えないので、非常停止を省きました")
            return
        for attempt in range(1 + EMERGENCY_RETRIES):
            try:
                motors.stop_now()
                self._driving = False
                return
            except Exception as error:
                logger.warning(
                    "モーターを止められませんでした（%d 回目）: %s",
                    attempt + 1,
                    type(error).__name__,
                )

    # --- 内側（ロックを持って呼ぶ） ---

    def _check(self, should_continue: Callable[[], bool]) -> None:
        # ロックを持って呼ぶ。should_continue（とサーボの書き込み）から Arbiter を呼ばないこと
        if not should_continue():
            self.emergency_stop()
            raise MotionAborted()

    def _look(self, pan: float, tilt: float) -> None:
        self._set("pan", pan)
        self._set("tilt", tilt)

    def _home(self) -> None:
        self._home_neck()
        self._set("steering", 0.0)

    def _home_neck(self) -> None:
        self._set("pan", 0.0)
        self._set("tilt", 0.0)

    def _set(self, channel: Channel, degrees: float) -> None:
        servos = self._servos
        if isinstance(servos, Unavailable):
            raise DeviceUnavailable(servos.device)
        calibration = self._settings.calibration
        if channel == "pan":
            low, high = self._settings.pan_range
            offset = calibration.pan
        elif channel == "tilt":
            low, high = self._settings.tilt_range
            offset = calibration.tilt
        else:
            low, high = -STEERING_LIMIT, STEERING_LIMIT
            offset = calibration.steering
        value = _clamp(degrees, low, high) + offset
        with self._device_errors("servos"):
            servos.set_angle(channel, value)

    def _speeds(self, speed: int) -> None:
        motors = self._motors
        if isinstance(motors, Unavailable):
            raise DeviceUnavailable(motors.device)
        value = int(_clamp(speed, -SPEED_LIMIT, SPEED_LIMIT))
        left_dir, right_dir = self._settings.calibration.motor_direction
        if value != 0:
            # 書き込みに失敗しても、回っているかもしれないので走行中とみなす
            self._driving = True
        with self._device_errors("motors"):
            motors.set_speeds(value * left_dir, value * right_dir)
        # 非常停止はロックを取らずに走行中でなくするので、書いた後にもう一度決める
        # （途中で割り込まれても、回っているなら走行中のままにする）
        self._driving = value != 0

    @contextmanager
    def _device_errors(self, device: str) -> Iterator[None]:
        try:
            yield
        except DeviceUnavailable:
            raise
        except Exception as error:
            logger.warning("%s に書き込めませんでした: %s", device, type(error).__name__)
            raise DeviceUnavailable(device) from error
