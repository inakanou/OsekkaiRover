"""カメラと映像の処理の口の型（ports）と、テスト用の偽物（9.4）。"""

import math

import pytest
from presence_fakes import FakeCamera, FakeFrame, FakeVision, embedding

from osekkai.features.presence.faces import EMBEDDING_SIZE
from osekkai.features.presence.ports import (
    MSG_CAMERA_UNAVAILABLE,
    CameraPort,
    CameraUnavailable,
    Frame,
    VisionPort,
)
from osekkai.features.presence.tracking import FaceBox

FACE_A = FaceBox(0.1, -0.2, 0.3)
FACE_B = FaceBox(-0.5, 0.4, 0.1)


# --- 型を満たす（mypy が確かめる） ---


def test_偽物は口の型を満たす() -> None:
    camera: CameraPort = FakeCamera()
    vision: VisionPort = FakeVision()
    frame: Frame = camera.capture()
    assert isinstance(frame, FakeFrame)
    assert vision.detect(frame) == ()


# --- カメラが使えない例外 ---


def test_カメラが使えない例外は決まった理由だけを持つ() -> None:
    error = CameraUnavailable()
    assert str(error) == MSG_CAMERA_UNAVAILABLE
    assert error.args == (MSG_CAMERA_UNAVAILABLE,)


# --- 偽のカメラ ---


def test_偽のカメラは始める止める撮るの回数を数える() -> None:
    camera = FakeCamera()
    before = camera.started
    camera.start()
    assert (before, camera.started) == (False, True)
    camera.capture()
    camera.capture()
    camera.capture_still()
    camera.stop()
    assert not camera.started
    assert (camera.start_count, camera.stop_count) == (1, 1)
    assert (camera.capture_count, camera.still_count) == (2, 1)


def test_偽のカメラは順に番号のついたフレームを返す() -> None:
    camera = FakeCamera()
    camera.start()
    frames = [camera.capture() for _ in range(3)]
    assert [f.index for f in frames] == [0, 1, 2]
    assert all((f.width, f.height) == (640, 480) for f in frames)


def test_偽のカメラの静止画はバイト列で毎回違う() -> None:
    camera = FakeCamera()
    first = camera.capture_still()
    second = camera.capture_still()
    assert isinstance(first, bytes)
    assert first != second


def test_使えない偽のカメラは例外を出す() -> None:
    camera = FakeCamera(unavailable=True)
    with pytest.raises(CameraUnavailable):
        camera.start()
    with pytest.raises(CameraUnavailable):
        camera.capture()
    with pytest.raises(CameraUnavailable):
        camera.capture_still()
    assert not camera.started
    assert camera.capture_count == 0


def test_途中から使えなくできる() -> None:
    camera = FakeCamera()
    camera.start()
    assert camera.capture().index == 0
    camera.unavailable = True
    with pytest.raises(CameraUnavailable):
        camera.capture()
    camera.unavailable = False
    assert camera.capture().index == 1


# --- 偽の映像の処理 ---


def test_台本どおりの顔の位置を返す() -> None:
    vision = FakeVision(faces={0: [FACE_A], 2: [FACE_A, FACE_B]})
    assert vision.detect(FakeFrame(0)) == (FACE_A,)
    assert vision.detect(FakeFrame(1)) == ()
    assert vision.detect(FakeFrame(2)) == (FACE_A, FACE_B)
    assert vision.detect_calls == [0, 1, 2]


def test_台本どおりの特徴を返し台本になければNone() -> None:
    emb = embedding(1)
    vision = FakeVision(embeddings={0: emb})
    assert vision.embed(FakeFrame(0), FACE_A) == emb
    assert vision.embed(FakeFrame(1), FACE_A) is None
    assert vision.embed_calls == [(0, FACE_A), (1, FACE_A)]


def test_動きの量は台本の順に返し尽きたら最後の値() -> None:
    vision = FakeVision(motions=[0.1, 0.5])
    a, b = FakeFrame(0), FakeFrame(1)
    assert [vision.motion(a, b) for _ in range(3)] == [0.1, 0.5, 0.5]
    assert vision.motion_calls == [(0, 1)] * 3


def test_動きの量の台本がなければ0() -> None:
    assert FakeVision().motion(FakeFrame(0), FakeFrame(1)) == 0.0


def test_動きの量の台本は0から1でなければ作れない() -> None:
    with pytest.raises(ValueError):
        FakeVision(motions=[1.5])


def test_JPEGは決まったバイト列で呼び出しを記録する() -> None:
    vision = FakeVision()
    first = vision.to_jpeg(FakeFrame(3), 320, 50)
    assert first == vision.to_jpeg(FakeFrame(3), 320, 50)
    assert first != vision.to_jpeg(FakeFrame(4), 320, 50)
    assert first != vision.to_jpeg(FakeFrame(3), 640, 50)
    assert vision.jpeg_calls == [(3, 320, 50), (3, 320, 50), (4, 320, 50), (3, 640, 50)]


def test_偽のフレームでないものは断る() -> None:
    vision = FakeVision()
    with pytest.raises(TypeError):
        vision.detect(object())


# --- 特徴を作る補助 ---


def test_種から長さ1の128個の特徴を作る() -> None:
    emb = embedding(7)
    assert len(emb) == EMBEDDING_SIZE
    assert math.isclose(math.fsum(v * v for v in emb), 1.0)
    assert embedding(7) == emb
    assert embedding(8) != emb
