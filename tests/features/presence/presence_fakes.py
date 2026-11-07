"""見つけてあいさつ（presence）のテストで共通に使う、カメラと映像の処理の偽物（9.4）。

`from presence_fakes import ...` で読む（tests/features/presence が import の道にある）。
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from osekkai.features.presence.faces import EMBEDDING_SIZE, Embedding, normalize
from osekkai.features.presence.ports import CameraUnavailable, Frame
from osekkai.features.presence.tracking import FaceBox

MSG_NOT_FAKE_FRAME = "偽のフレームではありません"
MSG_MOTION_RANGE = "動きの量は 0 から 1 の数にしてください"


@dataclass(frozen=True)
class FakeFrame:
    """偽の映像の 1 枚。番号で台本と結びつける。"""

    index: int
    width: int = 640
    height: int = 480


def _index(frame: Frame) -> int:
    if not isinstance(frame, FakeFrame):
        raise TypeError(MSG_NOT_FAKE_FRAME)
    return frame.index


def embedding(seed: int) -> Embedding:
    """種から、決まった 128 個の長さ 1 の特徴を作る。"""
    rng = random.Random(seed)
    return normalize([rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_SIZE)])


@dataclass
class FakeCamera:
    """偽のカメラ。呼ばれた回数を数え、撮るたびに番号の続くフレームを返す。

    `unavailable` を真にすると、始める・撮る・静止画で `CameraUnavailable` を出す。
    """

    unavailable: bool = False
    started: bool = False
    start_count: int = 0
    stop_count: int = 0
    capture_count: int = 0
    still_count: int = 0

    def _check(self) -> None:
        if self.unavailable:
            raise CameraUnavailable()

    def start(self) -> None:
        self._check()
        self.start_count += 1
        self.started = True

    def stop(self) -> None:
        self.stop_count += 1
        self.started = False

    def capture(self) -> FakeFrame:
        self._check()
        frame = FakeFrame(self.capture_count)
        self.capture_count += 1
        return frame

    def capture_still(self) -> bytes:
        self._check()
        data = f"still:{self.still_count}".encode()
        self.still_count += 1
        return data


@dataclass
class FakeVision:
    """偽の映像の処理。フレームの番号ごとの台本で、顔の位置と特徴を返す。

    - faces: 番号 → 顔の位置の並び（なければ顔なし）
    - embeddings: 番号 → 特徴（なければ None。認識のモデルがないときと同じ）
    - motions: 動きの量を呼ばれた順に返し、尽きたら最後の値（空なら 0）
    """

    faces: Mapping[int, Sequence[FaceBox]] = field(default_factory=dict)
    embeddings: Mapping[int, Embedding | None] = field(default_factory=dict)
    motions: Sequence[float] = ()
    detect_calls: list[int] = field(default_factory=list)
    embed_calls: list[tuple[int, FaceBox]] = field(default_factory=list)
    motion_calls: list[tuple[int, int]] = field(default_factory=list)
    jpeg_calls: list[tuple[int, int, int]] = field(default_factory=list)
    _motion_next: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if any(not 0.0 <= value <= 1.0 for value in self.motions):
            raise ValueError(MSG_MOTION_RANGE)

    def detect(self, frame: Frame) -> tuple[FaceBox, ...]:
        index = _index(frame)
        self.detect_calls.append(index)
        return tuple(self.faces.get(index, ()))

    def embed(self, frame: Frame, face: FaceBox) -> Embedding | None:
        index = _index(frame)
        self.embed_calls.append((index, face))
        return self.embeddings.get(index)

    def motion(self, before: Frame, after: Frame) -> float:
        self.motion_calls.append((_index(before), _index(after)))
        if not self.motions:
            return 0.0
        value = self.motions[min(self._motion_next, len(self.motions) - 1)]
        self._motion_next += 1
        return value

    def to_jpeg(self, frame: Frame, max_width: int, quality: int) -> bytes:
        index = _index(frame)
        self.jpeg_calls.append((index, max_width, quality))
        return f"jpeg:{index}:{max_width}:{quality}".encode()
