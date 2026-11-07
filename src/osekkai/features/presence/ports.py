"""カメラと映像の処理の口の型（9.4）。

見る流れ（watcher）は、この型だけを通してカメラと映像の処理を使う。
実機では picamera2 と OpenCV のアダプタが、テストでは偽物がこの型を満たす。
顔の位置の型（FaceBox）は追従の部品に、特徴の型（Embedding）は顔の帳面にあり、ここでは読み込んで使う。
上下の反転は、アダプタの中で当てる（フレームと静止画とも。7.4、7.5）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from osekkai.features.presence.faces import Embedding
from osekkai.features.presence.tracking import FaceBox

MSG_CAMERA_UNAVAILABLE = "カメラが使えません"


class Frame(Protocol):
    """アダプタの映像の 1 枚。中身は見ない。映像の処理の口にそのまま渡す。"""


class CameraPort(Protocol):
    """首のカメラの口。使えないときは `CameraUnavailable` を出す。"""

    def start(self) -> None:
        """撮るのを始める。"""
        ...

    def stop(self) -> None:
        """撮るのを止める。止めた後に start でまた始められる。"""
        ...

    def capture(self) -> Frame:
        """いまの映像の 1 枚（640×480）を返す。"""
        ...

    def capture_still(self) -> bytes:
        """カメラの最大の大きさの静止画を JPEG で返す。メモリの中で返し、保存はしない（7.5）。"""
        ...


class VisionPort(Protocol):
    """映像の処理の口（顔を見つける・特徴にする・動きの量・JPEG にする）。"""

    def detect(self, frame: Frame) -> Sequence[FaceBox]:
        """映像の中の顔の位置を返す。顔がなければ空。"""
        ...

    def embed(self, frame: Frame, face: FaceBox) -> Embedding | None:
        """顔の特徴（長さ 1 にそろえた 128 個）を返す。認識のモデルがなければ None。"""
        ...

    def motion(self, before: Frame, after: Frame) -> float:
        """2 枚の映像の変化の大きさを 0〜1 で返す（7.3）。"""
        ...

    def to_jpeg(self, frame: Frame, max_width: int, quality: int) -> bytes:
        """映像を幅の上限と品質で JPEG にして、メモリの中で返す（7.4）。"""
        ...


class CameraUnavailable(Exception):
    """カメラが使えない。理由は決まった文言だけで、値や機器の名前は持たない。"""

    def __init__(self) -> None:
        super().__init__(MSG_CAMERA_UNAVAILABLE)
