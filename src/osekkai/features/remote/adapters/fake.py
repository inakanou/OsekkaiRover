"""開発 PC で使う偽物。センサーのライブラリがない PC と CI で使う（15.1）。

UnreadableSensors は、Raspberry Pi なのにセンサーのライブラリがないときに使う。
いつも読めない（危険）ので、前には進まない。
"""

from __future__ import annotations

from typing import Final

from ..ports import SensorReading

CLEAR_DISTANCE_CM: Final = 100.0
CLEAR_FLOOR: Final = 1000


class ClearSensors:
    """いつも安全な読みを返すセンサー（SensorPort）。前に物がなく、床が明るい。"""

    def read(self) -> SensorReading:
        return SensorReading(
            distance_cm=CLEAR_DISTANCE_CM, floor=(CLEAR_FLOOR, CLEAR_FLOOR, CLEAR_FLOOR)
        )

    def close(self) -> None:
        pass


class UnreadableSensors:
    """いつも読めないセンサー（SensorPort）。距離も床も読めない（危険）。"""

    def read(self) -> SensorReading:
        return SensorReading(distance_cm=None, floor=(None, None, None))

    def close(self) -> None:
        pass


class NoUsbCamera:
    """ない USB のカメラ（UsbCameraPort）。開けない。"""

    def open(self) -> bool:
        return False

    def read_jpeg(self) -> bytes | None:
        return None

    def close(self) -> None:
        pass
