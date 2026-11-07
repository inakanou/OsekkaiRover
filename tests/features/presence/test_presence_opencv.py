"""OpenCV の映像の処理（adapters/opencv）。cv2 と numpy は偽の module にする。

開発 PC と CI には OpenCV と numpy がないので、呼び出しの形（大きさ・しきい値・品質）と、
位置の直し方・正規化・動きの量の割合・モデルがないときの振る舞いを確かめる。
"""

from __future__ import annotations

import importlib.util
import math
import sys
import types
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from osekkai.features.presence.adapters import opencv
from osekkai.features.presence.adapters.opencv import (
    MSG_UNKNOWN_FACE,
    MSG_VISION_UNAVAILABLE,
    OpenCvVision,
    VisionUnavailable,
    opencv_available,
)
from osekkai.features.presence.faces import EMBEDDING_SIZE
from osekkai.features.presence.ports import CameraUnavailable, VisionPort
from osekkai.features.presence.tracking import FaceBox

# --- 偽の画像と偽の cv2・numpy ---


def _close(a: object, b: object) -> bool:
    """近い数か。pytest.approx は偽の numpy を見てしまうので、自分で比べる。"""
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b, strict=True))
    assert isinstance(a, (int, float)) and isinstance(b, (int, float))
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


class FakeImage:
    """偽の画像。shape は numpy と同じ（高さ, 幅[, 色]）。"""

    def __init__(self, width: int, height: int, tag: str = "", nonzero: int = 0) -> None:
        self.shape = (height, width, 3)
        self.tag = tag
        self.nonzero = nonzero


class FakeFeature:
    def __init__(self, values: Sequence[float]) -> None:
        self.values = list(values)

    def flatten(self) -> list[float]:
        return list(self.values)


class FakeBuffer:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def tobytes(self) -> bytes:
        return self.data


class FakeDetector:
    def __init__(self, rows: list[list[float]] | None) -> None:
        self.rows = rows
        self.input_sizes: list[tuple[int, int]] = []
        self.detected: list[FakeImage] = []

    def setInputSize(self, size: tuple[int, int]) -> None:  # noqa: N802 （OpenCV の名前）
        self.input_sizes.append(size)

    def detect(self, image: FakeImage) -> tuple[int, list[list[float]] | None]:
        self.detected.append(image)
        return 1, self.rows


class FakeRecognizer:
    def __init__(self, values: Sequence[float]) -> None:
        self.values = values
        self.aligned: list[tuple[FakeImage, Any]] = []

    def alignCrop(self, image: FakeImage, row: Any) -> FakeImage:  # noqa: N802
        self.aligned.append((image, row))
        return FakeImage(112, 112, tag="aligned")

    def feature(self, aligned: FakeImage) -> FakeFeature:
        assert aligned.tag == "aligned"
        return FakeFeature(self.values)


class FakeCv2:
    """偽の cv2 の中身。module に写して使う。"""

    COLOR_BGR2GRAY = 6
    THRESH_BINARY = 0
    INTER_AREA = 3
    IMWRITE_JPEG_QUALITY = 1

    def __init__(self) -> None:
        self.detector = FakeDetector(rows=None)
        self.recognizer = FakeRecognizer([1.0] * EMBEDDING_SIZE)
        self.detector_create: list[tuple[Any, ...]] = []
        self.recognizer_create: list[tuple[Any, ...]] = []
        self.resized: list[tuple[str, tuple[int, int], dict[str, Any]]] = []
        self.calls: list[tuple[str, Any]] = []
        self.encoded: list[tuple[str, FakeImage, list[int]]] = []
        self.encode_ok = True
        self.fail_detector_create = False
        self.fail_recognizer_create = False

        outer = self

        class FaceDetectorYN:
            @staticmethod
            def create(*args: Any) -> FakeDetector:
                if outer.fail_detector_create:
                    raise RuntimeError("壊れたモデル")
                outer.detector_create.append(args)
                return outer.detector

        class FaceRecognizerSF:
            @staticmethod
            def create(*args: Any) -> FakeRecognizer:
                if outer.fail_recognizer_create:
                    raise RuntimeError("壊れたモデル")
                outer.recognizer_create.append(args)
                return outer.recognizer

        self.FaceDetectorYN = FaceDetectorYN
        self.FaceRecognizerSF = FaceRecognizerSF

    def resize(self, image: FakeImage, size: tuple[int, int], **kwargs: Any) -> FakeImage:
        self.resized.append((image.tag, size, kwargs))
        return FakeImage(size[0], size[1], tag=f"{image.tag}-small", nonzero=image.nonzero)

    def cvtColor(self, image: FakeImage, code: int) -> FakeImage:  # noqa: N802
        self.calls.append(("cvtColor", (image.tag, code)))
        return FakeImage(
            image.shape[1], image.shape[0], tag=f"{image.tag}-gray", nonzero=image.nonzero
        )

    def GaussianBlur(self, image: FakeImage, ksize: tuple[int, int], sigma: float) -> FakeImage:  # noqa: N802
        self.calls.append(("GaussianBlur", (image.tag, ksize, sigma)))
        return FakeImage(
            image.shape[1], image.shape[0], tag=f"{image.tag}-blur", nonzero=image.nonzero
        )

    def absdiff(self, a: FakeImage, b: FakeImage) -> FakeImage:
        self.calls.append(("absdiff", (a.tag, b.tag)))
        # 偽の差: 2 枚の nonzero の差を、変わった点の数とする
        return FakeImage(a.shape[1], a.shape[0], tag="diff", nonzero=abs(a.nonzero - b.nonzero))

    def threshold(
        self, image: FakeImage, thresh: float, maxval: float, kind: int
    ) -> tuple[float, FakeImage]:
        self.calls.append(("threshold", (image.tag, thresh, maxval, kind)))
        return thresh, FakeImage(image.shape[1], image.shape[0], tag="mask", nonzero=image.nonzero)

    def countNonZero(self, image: FakeImage) -> int:  # noqa: N802
        self.calls.append(("countNonZero", image.tag))
        return image.nonzero

    def imencode(self, ext: str, image: FakeImage, params: list[int]) -> tuple[bool, FakeBuffer]:
        self.encoded.append((ext, image, params))
        return self.encode_ok, FakeBuffer(b"\xff\xd8jpeg")


class FakeNumpy:
    float32 = "float32"

    def __init__(self) -> None:
        self.arrays: list[tuple[list[float], Any]] = []

    def asarray(self, values: Sequence[float], dtype: Any = None) -> list[float]:
        copied = list(values)
        self.arrays.append((copied, dtype))
        return copied


def _module(name: str, source: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for attr in dir(source):
        if not attr.startswith("__"):
            setattr(module, attr, getattr(source, attr))
    return module


@pytest.fixture
def cv2(monkeypatch: pytest.MonkeyPatch) -> FakeCv2:
    fake = FakeCv2()
    monkeypatch.setitem(sys.modules, "cv2", _module("cv2", fake))
    return fake


@pytest.fixture
def np(monkeypatch: pytest.MonkeyPatch) -> FakeNumpy:
    fake = FakeNumpy()
    monkeypatch.setitem(sys.modules, "numpy", _module("numpy", fake))
    return fake


@pytest.fixture
def models(tmp_path: Path) -> tuple[Path, Path]:
    detection = tmp_path / "yunet.onnx"
    recognition = tmp_path / "sface.onnx"
    detection.write_bytes(b"d")
    recognition.write_bytes(b"r")
    return detection, recognition


def _row(x: float, y: float, w: float, h: float, score: float = 0.9) -> list[float]:
    # 目鼻口の 10 個は、それぞれ x と y の並び
    landmarks = [x + 1.0, y + 2.0] * 5
    return [x, y, w, h, *landmarks, score]


# --- OpenCV を読み込めるか ---


def test_cv2とnumpyがあれば読み込めるとする(cv2: FakeCv2, np: FakeNumpy) -> None:
    assert opencv_available() is True


def test_cv2がなければ読み込めないとする(monkeypatch: pytest.MonkeyPatch, np: FakeNumpy) -> None:
    monkeypatch.delitem(sys.modules, "cv2", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert opencv_available() is False


def test_numpyがなければ読み込めないとする(monkeypatch: pytest.MonkeyPatch, cv2: FakeCv2) -> None:
    monkeypatch.delitem(sys.modules, "numpy", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert opencv_available() is False


def test_探す途中の誤りは読み込めないとする(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "cv2", raising=False)
    monkeypatch.delitem(sys.modules, "numpy", raising=False)

    def broken(name: str) -> None:
        raise ImportError(name)

    monkeypatch.setattr(importlib.util, "find_spec", broken)
    assert opencv_available() is False


# --- 作るとき（モデル） ---


def test_検出の器を320x240としきい値で作る(
    cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]
) -> None:
    detection, recognition = models
    vision: VisionPort = OpenCvVision(detection, recognition)
    assert isinstance(vision, OpenCvVision)
    assert len(cv2.detector_create) == 1
    args = cv2.detector_create[0]
    assert args[0] == str(detection)
    assert args[1] == ""
    assert tuple(args[2]) == (320, 240)
    assert _close(args[3], 0.6)
    assert cv2.detector.input_sizes == [(320, 240)]
    assert cv2.recognizer_create == [(str(recognition), "")]


def test_しきい値を変えられる(cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]) -> None:
    OpenCvVision(*models, score_threshold=0.8)
    assert _close(cv2.detector_create[0][3], 0.8)


def test_検出のモデルがなければ使えないとする(cv2: FakeCv2, np: FakeNumpy, tmp_path: Path) -> None:
    with pytest.raises(VisionUnavailable) as info:
        OpenCvVision(tmp_path / "ない.onnx", tmp_path / "ない2.onnx")
    assert str(info.value) == MSG_VISION_UNAVAILABLE
    assert "ない" not in str(info.value)
    # 見る流れがカメラの例外として扱えるように、カメラが使えない例外の仲間にする
    assert isinstance(info.value, CameraUnavailable)
    assert cv2.detector_create == []


def test_検出のモデルを読めなければ使えないとする(
    cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]
) -> None:
    cv2.fail_detector_create = True
    with pytest.raises(VisionUnavailable) as info:
        OpenCvVision(*models)
    assert str(info.value) == MSG_VISION_UNAVAILABLE


def test_cv2を読み込めなければ使えないとする(
    monkeypatch: pytest.MonkeyPatch, np: FakeNumpy, models: tuple[Path, Path]
) -> None:
    monkeypatch.setitem(sys.modules, "cv2", None)  # import すると ImportError になる
    with pytest.raises(VisionUnavailable):
        OpenCvVision(*models)


def test_numpyを読み込めなければ使えないとする(
    monkeypatch: pytest.MonkeyPatch, cv2: FakeCv2, models: tuple[Path, Path]
) -> None:
    monkeypatch.setitem(sys.modules, "numpy", None)
    with pytest.raises(VisionUnavailable):
        OpenCvVision(*models)


def test_読み込みは作るときまで遅らせる() -> None:
    # module を読み込んだだけでは cv2 と numpy を読まない（開発 PC でも import できる）
    assert "cv2" not in vars(opencv)
    assert "numpy" not in vars(opencv)
    assert "np" not in vars(opencv)


# --- 顔を見つける ---


def test_320x240に縮めて見つけ位置と大きさを返す(
    cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]
) -> None:
    # 真ん中の顔（中心 160, 120）と右下の顔（中心 240, 180）
    cv2.detector.rows = [_row(140, 100, 40, 40), _row(220, 150, 40, 60)]
    vision = OpenCvVision(*models)
    frame = FakeImage(640, 480, tag="frame")
    faces = vision.detect(frame)
    assert cv2.resized[0][:2] == ("frame", (320, 240))
    assert cv2.detector.detected[0].tag == "frame-small"
    assert faces[0] == FaceBox(0.0, 0.0, 40 / 320)
    assert _close(faces[1].x, 0.5)
    assert _close(faces[1].y, 0.5)
    assert _close(faces[1].size, 40 / 320)


def test_顔がなければ空を返す(cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]) -> None:
    cv2.detector.rows = None
    vision = OpenCvVision(*models)
    assert list(vision.detect(FakeImage(640, 480))) == []


def test_はみ出した顔は範囲に収める(cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]) -> None:
    cv2.detector.rows = [_row(-50, -60, 30, 30), _row(300, 230, 400, 80)]
    vision = OpenCvVision(*models)
    faces = vision.detect(FakeImage(640, 480))
    assert faces[0].x == -1.0
    assert faces[0].y == -1.0
    assert faces[1].x == 1.0
    assert faces[1].y == 1.0
    assert faces[1].size == 1.0


def test_使えない数の行は捨てる(cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]) -> None:
    cv2.detector.rows = [_row(math.nan, 100, 40, 40), _row(140, 100, 40, 40)]
    vision = OpenCvVision(*models)
    assert vision.detect(FakeImage(640, 480)) == (FaceBox(0.0, 0.0, 40 / 320),)


def test_元の映像の大きさに合わせて直す(
    cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]
) -> None:
    # 元の大きさが違っても、縮めた後の位置から同じ割合になる
    cv2.detector.rows = [_row(140, 100, 40, 40)]
    vision = OpenCvVision(*models)
    assert vision.detect(FakeImage(1280, 960)) == (FaceBox(0.0, 0.0, 40 / 320),)


# --- 特徴にする ---


def test_元の大きさに戻した行で切り出し正規化した特徴を返す(
    cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]
) -> None:
    row = _row(140, 100, 40, 40)
    cv2.detector.rows = [row]
    cv2.recognizer.values = [2.0] * EMBEDDING_SIZE
    vision = OpenCvVision(*models)
    frame = FakeImage(640, 480, tag="frame")
    (face,) = vision.detect(frame)
    emb = vision.embed(frame, face)
    assert emb is not None
    assert len(emb) == EMBEDDING_SIZE
    assert _close(math.fsum(v * v for v in emb), 1.0)
    assert all(isinstance(v, float) for v in emb)
    image, passed = cv2.recognizer.aligned[0]
    assert image is frame  # 元の映像から切り出す
    # 位置と目鼻口の 14 個は 2 倍（640/320, 480/240）、score はそのまま
    expected = [v * 2.0 for v in row[:14]] + [row[14]]
    assert _close(passed, expected)
    assert np.arrays[-1][1] == "float32"


def test_縦と横の倍率を分けて戻す(cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]) -> None:
    row = _row(140, 100, 40, 40)
    cv2.detector.rows = [row]
    vision = OpenCvVision(*models)
    frame = FakeImage(960, 480)
    (face,) = vision.detect(frame)
    vision.embed(frame, face)
    _, passed = cv2.recognizer.aligned[0]
    assert _close(passed[0], row[0] * 3.0)  # x は 960/320
    assert _close(passed[1], row[1] * 2.0)  # y は 480/240
    assert _close(passed[2], row[2] * 3.0)
    assert _close(passed[3], row[3] * 2.0)
    assert _close(passed[4], row[4] * 3.0)
    assert _close(passed[5], row[5] * 2.0)


def test_認識のモデルがなければ特徴はNone(
    cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]
) -> None:
    detection, recognition = models
    recognition.unlink()
    cv2.detector.rows = [_row(140, 100, 40, 40)]
    vision = OpenCvVision(detection, recognition)
    assert cv2.recognizer_create == []
    frame = FakeImage(640, 480)
    (face,) = vision.detect(frame)
    assert vision.embed(frame, face) is None


def test_認識のモデルを読めなければ特徴はNone(
    cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]
) -> None:
    cv2.fail_recognizer_create = True
    cv2.detector.rows = [_row(140, 100, 40, 40)]
    vision = OpenCvVision(*models)
    frame = FakeImage(640, 480)
    (face,) = vision.detect(frame)
    assert vision.embed(frame, face) is None


def test_見つけていない顔は特徴にできない(
    cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]
) -> None:
    vision = OpenCvVision(*models)
    with pytest.raises(ValueError) as info:
        vision.embed(FakeImage(640, 480), FaceBox(0.0, 0.0, 0.1))
    assert str(info.value) == MSG_UNKNOWN_FACE


def test_特徴の形が誤っていればValueError(
    cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]
) -> None:
    cv2.detector.rows = [_row(140, 100, 40, 40)]
    cv2.recognizer.values = [0.0] * EMBEDDING_SIZE
    vision = OpenCvVision(*models)
    frame = FakeImage(640, 480)
    (face,) = vision.detect(frame)
    with pytest.raises(ValueError):
        vision.embed(frame, face)


# --- 動きの量 ---


def test_動きの量は160x120の灰色をぼかし差25の割合(
    cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]
) -> None:
    vision = OpenCvVision(*models)
    before = FakeImage(640, 480, tag="a", nonzero=0)
    after = FakeImage(640, 480, tag="b", nonzero=160 * 120 // 4)
    assert _close(vision.motion(before, after), 0.25)
    assert [r[:2] for r in cv2.resized] == [("a", (160, 120)), ("b", (160, 120))]
    assert ("cvtColor", ("a-small", FakeCv2.COLOR_BGR2GRAY)) in cv2.calls
    assert ("GaussianBlur", ("a-small-gray", (5, 5), 0)) in cv2.calls
    assert ("GaussianBlur", ("b-small-gray", (5, 5), 0)) in cv2.calls
    assert ("absdiff", ("a-small-gray-blur", "b-small-gray-blur")) in cv2.calls
    assert ("threshold", ("diff", 25, 255, FakeCv2.THRESH_BINARY)) in cv2.calls
    assert ("countNonZero", "mask") in cv2.calls


def test_動きがなければ0(cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]) -> None:
    vision = OpenCvVision(*models)
    assert vision.motion(FakeImage(640, 480), FakeImage(640, 480)) == 0.0


# --- JPEG にする ---


def test_幅の上限より広ければ縦横の比を保って縮め品質でJPEGにする(
    cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]
) -> None:
    vision = OpenCvVision(*models)
    data = vision.to_jpeg(FakeImage(640, 480, tag="frame"), max_width=320, quality=70)
    assert data == b"\xff\xd8jpeg"
    tag, size, kwargs = cv2.resized[-1]
    assert (tag, size) == ("frame", (320, 240))
    assert kwargs == {"interpolation": FakeCv2.INTER_AREA}
    ext, image, params = cv2.encoded[0]
    assert ext == ".jpg"
    assert image.shape[:2] == (240, 320)
    assert params == [FakeCv2.IMWRITE_JPEG_QUALITY, 70]


def test_幅の上限以下なら縮めない(cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]) -> None:
    vision = OpenCvVision(*models)
    frame = FakeImage(640, 480, tag="frame")
    vision.to_jpeg(frame, max_width=640, quality=80)
    assert cv2.resized == []
    assert cv2.encoded[0][1] is frame


def test_JPEGにできなければ決まった理由の誤り(
    cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path]
) -> None:
    cv2.encode_ok = False
    vision = OpenCvVision(*models)
    with pytest.raises(RuntimeError) as info:
        vision.to_jpeg(FakeImage(640, 480), max_width=640, quality=80)
    assert str(info.value) == opencv.MSG_JPEG_FAILED


@pytest.mark.parametrize(("max_width", "quality"), [(0, 80), (640, 0), (640, 101)])
def test_幅と品質の範囲の外はValueError(
    cv2: FakeCv2, np: FakeNumpy, models: tuple[Path, Path], max_width: int, quality: int
) -> None:
    vision = OpenCvVision(*models)
    with pytest.raises(ValueError):
        vision.to_jpeg(FakeImage(640, 480), max_width=max_width, quality=quality)
    assert cv2.encoded == []
