"""OpenCV の映像の処理（顔を見つける・特徴にする・動きの量・JPEG にする）。

顔は YuNet（320×240、score 0.6）で見つけ、SFace で 128 個の特徴にして長さ 1 にそろえる。
cv2 と numpy は、作るときと関数の中だけで読み込む（開発 PC と CI にはない）。
映像の上下の反転は、ここでは当てない。カメラのアダプタが、撮ったフレームと静止画に
1 回だけ当てる（7.4、7.5）。ここで当てると 2 回になって元に戻ってしまう。
"""

from __future__ import annotations

import importlib.util
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from osekkai.features.presence.faces import Embedding, normalize
from osekkai.features.presence.ports import CameraUnavailable, Frame
from osekkai.features.presence.tracking import FaceBox

DETECT_WIDTH: Final = 320  # 顔を見つけるときの大きさ（1.1）
DETECT_HEIGHT: Final = 240
SCORE_THRESHOLD: Final = 0.6  # 顔とみなす確からしさ
NMS_THRESHOLD: Final = 0.3  # 重なった枠をまとめるしきい値
TOP_K: Final = 5000  # まとめる前に残す枠の数
ROW_POINTS: Final = 14  # 1 行のうち位置と目鼻口の数（x と y が交互）。残りは score

MOTION_WIDTH: Final = 160  # 動きの量を見るときの大きさ（7.3）
MOTION_HEIGHT: Final = 120
MOTION_BLUR: Final = (5, 5)  # ぼかしの大きさ
MOTION_DIFF: Final = 25  # 変わったとみなす明るさの差

MSG_VISION_UNAVAILABLE = "映像の処理が使えません"
MSG_UNKNOWN_FACE = "見つけていない顔です"
MSG_JPEG_FAILED = "JPEG にできませんでした"
MSG_JPEG_WIDTH = "JPEG の幅は 1 以上にしてください"
MSG_JPEG_QUALITY = "JPEG の品質は 1 から 100 にしてください"


class VisionUnavailable(CameraUnavailable):
    """映像の処理が使えない（cv2 か numpy がない、検出のモデルがない・読めない）。

    見る流れがカメラと同じく「見ることを止める」と扱えるように、カメラが使えない例外の仲間にする。
    理由は決まった文言だけで、パスや値は持たない。
    """

    def __init__(self) -> None:
        Exception.__init__(self, MSG_VISION_UNAVAILABLE)


def _module_available(name: str) -> bool:
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def opencv_available() -> bool:
    """cv2 と numpy を読み込めるか（機能と self-check が使う）。読み込みはしない。"""
    return _module_available("cv2") and _module_available("numpy")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _size_of(frame: Any) -> tuple[int, int]:
    """映像の（幅, 高さ）。numpy の shape は（高さ, 幅, 色）。"""
    height, width = frame.shape[:2]
    return int(width), int(height)


class OpenCvVision:
    """OpenCV で映像を処理する（VisionPort）。

    検出のモデルがない・読めない、cv2 か numpy を読み込めないときは、
    作るときに `VisionUnavailable`。
    認識のモデルだけがない・読めないときは作れて、特徴は None になる。
    フレームは BGR の numpy の配列（カメラのアダプタが上下の反転を当てた後のもの）。
    """

    def __init__(
        self,
        detection_model: Path,
        recognition_model: Path,
        *,
        score_threshold: float = SCORE_THRESHOLD,
    ) -> None:
        try:
            import cv2
            import numpy
        except ImportError:
            raise VisionUnavailable() from None
        self._cv2: Any = cv2
        self._np: Any = numpy
        if not detection_model.is_file():
            raise VisionUnavailable()
        size = (DETECT_WIDTH, DETECT_HEIGHT)
        try:
            detector = cv2.FaceDetectorYN.create(
                str(detection_model), "", size, score_threshold, NMS_THRESHOLD, TOP_K
            )
            detector.setInputSize(size)
        except Exception:
            raise VisionUnavailable() from None
        self._detector: Any = detector
        self._recognizer: Any = None
        if recognition_model.is_file():
            try:
                self._recognizer = cv2.FaceRecognizerSF.create(str(recognition_model), "")
            except Exception:
                self._recognizer = None
        # 最後に見つけた顔ごとの、元の映像の大きさに戻した行（特徴にするときに使う）
        self._rows: dict[FaceBox, list[float]] = {}

    def detect(self, frame: Frame) -> Sequence[FaceBox]:
        """320×240 に縮めて顔を見つけ、真ん中から −1〜1 の位置と幅の割合にする（1.1）。"""
        cv2 = self._cv2
        width, height = _size_of(frame)
        small = cv2.resize(frame, (DETECT_WIDTH, DETECT_HEIGHT))
        _, rows = self._detector.detect(small)
        scale_x = width / DETECT_WIDTH
        scale_y = height / DETECT_HEIGHT
        faces: list[FaceBox] = []
        found: dict[FaceBox, list[float]] = {}
        for raw in [] if rows is None else rows:
            row = [float(v) for v in raw]
            if len(row) <= ROW_POINTS or not all(math.isfinite(v) for v in row):
                continue
            fx, fy, fw, fh = row[:4]
            face = FaceBox(
                x=_clamp((fx + fw / 2) / DETECT_WIDTH * 2 - 1, -1.0, 1.0),
                y=_clamp((fy + fh / 2) / DETECT_HEIGHT * 2 - 1, -1.0, 1.0),
                size=_clamp(fw / DETECT_WIDTH, 0.0, 1.0),
            )
            scaled = [
                v * (scale_x if i % 2 == 0 else scale_y) if i < ROW_POINTS else v
                for i, v in enumerate(row)
            ]
            faces.append(face)
            found[face] = scaled
        self._rows = found
        return tuple(faces)

    def embed(self, frame: Frame, face: FaceBox) -> Embedding | None:
        """最後の detect で見つけた顔を元の映像から切り出し、長さ 1 の特徴にする（2.1）。

        認識のモデルがなければ None。最後の detect で見つけていない顔は `ValueError`。
        """
        if self._recognizer is None:
            return None
        row = self._rows.get(face)
        if row is None:
            raise ValueError(MSG_UNKNOWN_FACE)
        box = self._np.asarray(row, dtype=self._np.float32)
        aligned = self._recognizer.alignCrop(frame, box)
        feature = self._recognizer.feature(aligned)
        return normalize([float(v) for v in feature.flatten()])

    def motion(self, before: Frame, after: Frame) -> float:
        """160×120 の灰色にしてぼかし、明るさの差が 25 を超えた点の割合を返す（7.3）。"""
        cv2 = self._cv2
        diff = cv2.absdiff(self._motion_image(before), self._motion_image(after))
        _, mask = cv2.threshold(diff, MOTION_DIFF, 255, cv2.THRESH_BINARY)
        changed = int(cv2.countNonZero(mask))
        return _clamp(changed / (MOTION_WIDTH * MOTION_HEIGHT), 0.0, 1.0)

    def _motion_image(self, frame: Frame) -> Any:
        cv2 = self._cv2
        small = cv2.resize(frame, (MOTION_WIDTH, MOTION_HEIGHT))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(gray, MOTION_BLUR, 0)

    def to_jpeg(self, frame: Frame, max_width: int, quality: int) -> bytes:
        """幅が上限より広ければ縦横の比を保って縮め、品質を決めて JPEG にする（7.4）。"""
        if max_width < 1:
            raise ValueError(MSG_JPEG_WIDTH)
        if not 1 <= quality <= 100:
            raise ValueError(MSG_JPEG_QUALITY)
        cv2 = self._cv2
        image: Any = frame
        width, height = _size_of(frame)
        if width > max_width:
            new_height = max(1, round(height * max_width / width))
            image = cv2.resize(frame, (max_width, new_height), interpolation=cv2.INTER_AREA)
        ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError(MSG_JPEG_FAILED)
        return bytes(buffer.tobytes())
