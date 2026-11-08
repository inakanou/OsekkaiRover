"""開発 PC で使う偽物。センサーのライブラリがない PC と CI で使う（15.1）。"""

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


class NoUsbCamera:
    """ない USB のカメラ（UsbCameraPort）。開けない。"""

    def open(self) -> bool:
        return False

    def read_jpeg(self) -> bytes | None:
        return None

    def close(self) -> None:
        pass
