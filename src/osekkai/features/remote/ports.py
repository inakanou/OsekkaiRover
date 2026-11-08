"""ラジコンのセンサーとカメラの口の型（5.1、5.4、8.1、8.3）。

判定とサービスは、この型だけを通してセンサーとカメラを使う。
実機では HAT のセンサーと USB のカメラのアダプタが、開発 PC では偽物（adapters/fake）が、
テストでは偽物がこの型を満たす。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SensorReading:
    """1 回の読み。

    `far=True` は、物がない（echo が待ちの中で下がらない）。距離は無限として扱い、
    危険にしない。echo が上がらないのは読み取りの失敗で、`distance_cm=None` になる。
    """

    distance_cm: float | None  # None は読めない
    floor: tuple[int | None, int | None, int | None]  # A0〜A2。None は読めない
    far: bool = False  # 物がない


@runtime_checkable
class SensorPort(Protocol):
    """前の距離と床の明るさを読む口。"""

    def read(self) -> SensorReading:
        """1 回読む。最長 0.1 秒。"""
        ...

    def close(self) -> None: ...


@runtime_checkable
class UsbCameraPort(Protocol):
    """USB のカメラの口。"""

    def open(self) -> bool:
        """開く。開けなければ偽。"""
        ...

    def read_jpeg(self) -> bytes | None:
        """1 枚の JPEG。失敗は None。"""
        ...

    def close(self) -> None: ...


@runtime_checkable
class FrameSource(Protocol):
    """映像を 1 枚ずつ読む元（首のカメラと USB のカメラ）。FrameHub が使う。"""

    def open(self) -> bool:
        """読み始める。読めなければ偽。"""
        ...

    def read(self) -> bytes | None:
        """映像の 1 枚（JPEG）。失敗と開いていないときは None。"""
        ...

    def still(self) -> bytes | None:
        """写真の 1 枚（JPEG）。失敗と開いていないときは None。"""
        ...

    def close(self) -> None: ...
