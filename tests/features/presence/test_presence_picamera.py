"""カメラ（picamera2）のアダプタ（1.1、7.5、7.8）。

picamera2 と libcamera は開発 PC と CI にないので、偽の module を sys.modules に入れて
呼び出しの形（設定の大きさ・色の並び・反転・始めと止め・静止画のメモリへの書き出し）を確かめる。
"""

from __future__ import annotations

import builtins
import io
import logging
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from osekkai.features.presence.adapters import picamera
from osekkai.features.presence.adapters.picamera import (
    STILL_FORMAT,
    VIDEO_FORMAT,
    VIDEO_SIZE,
    PiCamera,
    camera_count,
    picamera_available,
)
from osekkai.features.presence.ports import CameraPort, CameraUnavailable

STILL_JPEG = b"\xff\xd8fake-still\xff\xd9"


class FakeTransform:
    def __init__(self, hflip: int = 0, vflip: int = 0) -> None:
        self.hflip = hflip
        self.vflip = vflip


class FakePicamera2:
    """picamera2.Picamera2 の偽物。呼ばれた順を calls に残す。"""

    instances: list[FakePicamera2] = []
    cameras: list[dict[str, Any]] = [{"Model": "fake"}]
    fail_init: BaseException | None = None
    fail_start: BaseException | None = None
    fail_capture: BaseException | None = None
    fail_info: BaseException | None = None

    def __init__(self) -> None:
        if FakePicamera2.fail_init is not None:
            raise FakePicamera2.fail_init
        self.calls: list[str] = []
        self.configured: dict[str, Any] | None = None
        self.video_kwargs: dict[str, Any] = {}
        self.still_kwargs: dict[str, Any] = {}
        self.still_config: dict[str, Any] | None = None
        self.still_output: object = None
        self.still_format: str | None = None
        FakePicamera2.instances.append(self)

    @staticmethod
    def global_camera_info() -> list[dict[str, Any]]:
        if FakePicamera2.fail_info is not None:
            raise FakePicamera2.fail_info
        return FakePicamera2.cameras

    def create_video_configuration(self, **kwargs: Any) -> dict[str, Any]:
        self.video_kwargs = kwargs
        return {"kind": "video", **kwargs}

    def create_still_configuration(self, **kwargs: Any) -> dict[str, Any]:
        self.still_kwargs = kwargs
        return {"kind": "still", **kwargs}

    def configure(self, config: dict[str, Any]) -> None:
        self.calls.append("configure")
        self.configured = config

    def start(self) -> None:
        if FakePicamera2.fail_start is not None:
            raise FakePicamera2.fail_start
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def close(self) -> None:
        self.calls.append("close")

    def capture_array(self, name: str = "main") -> object:
        if FakePicamera2.fail_capture is not None:
            raise FakePicamera2.fail_capture
        self.calls.append(f"capture_array:{name}")
        return ("frame", name)

    def switch_mode_and_capture_file(
        self,
        config: dict[str, Any],
        file_output: object,
        name: str = "main",
        format: str | None = None,
    ) -> None:
        self.calls.append("switch_mode_and_capture_file")
        self.still_config = config
        self.still_output = file_output
        self.still_format = format
        if isinstance(file_output, (str, Path)):
            raise AssertionError("ファイルに書こうとした")
        assert isinstance(file_output, io.BytesIO)
        file_output.write(STILL_JPEG)


@pytest.fixture
def fake_libs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    FakePicamera2.instances = []
    FakePicamera2.cameras = [{"Model": "fake"}]
    FakePicamera2.fail_init = None
    FakePicamera2.fail_start = None
    FakePicamera2.fail_capture = None
    FakePicamera2.fail_info = None
    picamera2_mod = types.ModuleType("picamera2")
    picamera2_mod.Picamera2 = FakePicamera2  # type: ignore[attr-defined]
    libcamera_mod = types.ModuleType("libcamera")
    libcamera_mod.Transform = FakeTransform  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "picamera2", picamera2_mod)
    monkeypatch.setitem(sys.modules, "libcamera", libcamera_mod)
    yield


@pytest.fixture
def no_libs(monkeypatch: pytest.MonkeyPatch) -> None:
    # None を入れると import が ImportError になる
    monkeypatch.setitem(sys.modules, "picamera2", None)
    monkeypatch.setitem(sys.modules, "libcamera", None)


def _only() -> FakePicamera2:
    assert len(FakePicamera2.instances) == 1
    return FakePicamera2.instances[0]


# --- 補助（機能と self-check が使う） ---


def test_ライブラリを読み込めればあると答える(fake_libs: None) -> None:
    assert picamera_available() is True


def test_ライブラリを読み込めなければないと答える(no_libs: None) -> None:
    assert picamera_available() is False


def test_ライブラリがどこにもなければないと答える(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "picamera2", raising=False)
    monkeypatch.setattr(picamera, "find_spec", lambda name: None)
    assert picamera_available() is False


def test_カメラの台数を返す(fake_libs: None) -> None:
    FakePicamera2.cameras = [{"Model": "a"}, {"Model": "b"}]
    assert camera_count() == 2


def test_カメラがなければ0台(fake_libs: None) -> None:
    FakePicamera2.cameras = []
    assert camera_count() == 0


def test_台数を調べられなければ0台(fake_libs: None) -> None:
    FakePicamera2.fail_info = RuntimeError("hidden detail")
    assert camera_count() == 0


def test_ライブラリがなければ0台(no_libs: None) -> None:
    assert camera_count() == 0


# --- 始めと止め ---


def test_型を満たす(fake_libs: None) -> None:
    camera: CameraPort = PiCamera(flip=False)
    assert camera is not None


def test_作っただけではカメラを開かない(fake_libs: None) -> None:
    PiCamera(flip=False)
    assert FakePicamera2.instances == []


def test_始めると640x480のRGB888の映像で始める(fake_libs: None) -> None:
    camera = PiCamera(flip=False)
    camera.start()
    cam = _only()
    assert VIDEO_SIZE == (640, 480)
    assert VIDEO_FORMAT == "RGB888"
    assert cam.video_kwargs["main"] == {"size": (640, 480), "format": "RGB888"}
    assert cam.configured is not None and cam.configured["kind"] == "video"
    assert cam.calls == ["configure", "start"]


def test_反転しないときは変換を当てない(fake_libs: None) -> None:
    camera = PiCamera(flip=False)
    camera.start()
    transform = _only().video_kwargs["transform"]
    assert (transform.hflip, transform.vflip) == (0, 0)


def test_反転するときは映像を180度回す(fake_libs: None) -> None:
    camera = PiCamera(flip=True)
    camera.start()
    transform = _only().video_kwargs["transform"]
    assert (transform.hflip, transform.vflip) == (1, 1)


def test_2回始めても開くのは1回(fake_libs: None) -> None:
    camera = PiCamera(flip=False)
    camera.start()
    camera.start()
    assert _only().calls == ["configure", "start"]


def test_止めるとカメラを止めて閉じる(fake_libs: None) -> None:
    camera = PiCamera(flip=False)
    camera.start()
    camera.stop()
    assert _only().calls == ["configure", "start", "stop", "close"]


def test_始める前に止めても何もしない(fake_libs: None) -> None:
    PiCamera(flip=False).stop()
    assert FakePicamera2.instances == []


def test_止めた後にまた始められる(fake_libs: None) -> None:
    camera = PiCamera(flip=False)
    camera.start()
    camera.stop()
    camera.start()
    assert len(FakePicamera2.instances) == 2
    assert FakePicamera2.instances[1].calls == ["configure", "start"]


# --- 撮る ---


def test_撮るとmainの1枚を返す(fake_libs: None) -> None:
    camera = PiCamera(flip=True)
    camera.start()
    assert camera.capture() == ("frame", "main")


def test_始める前に撮るとカメラが使えない(fake_libs: None) -> None:
    with pytest.raises(CameraUnavailable):
        PiCamera(flip=False).capture()


def test_撮れなければカメラが使えない(fake_libs: None) -> None:
    camera = PiCamera(flip=False)
    camera.start()
    FakePicamera2.fail_capture = RuntimeError("hidden detail")
    with pytest.raises(CameraUnavailable):
        camera.capture()


# --- 静止画 ---


def test_静止画は最大の大きさのJPEGをメモリの中で返す(fake_libs: None) -> None:
    camera = PiCamera(flip=False)
    camera.start()
    assert camera.capture_still() == STILL_JPEG
    cam = _only()
    assert isinstance(cam.still_output, io.BytesIO)
    assert cam.still_format == STILL_FORMAT == "jpeg"
    assert cam.still_config is not None and cam.still_config["kind"] == "still"
    # 大きさを指定しない（既定はカメラの最大の大きさ）
    assert "main" not in cam.still_kwargs


def test_反転するときは静止画も180度回す(fake_libs: None) -> None:
    camera = PiCamera(flip=True)
    camera.start()
    camera.capture_still()
    transform = _only().still_kwargs["transform"]
    assert (transform.hflip, transform.vflip) == (1, 1)


def test_反転しないときは静止画に変換を当てない(fake_libs: None) -> None:
    camera = PiCamera(flip=False)
    camera.start()
    camera.capture_still()
    transform = _only().still_kwargs["transform"]
    assert (transform.hflip, transform.vflip) == (0, 0)


def test_静止画はファイルを書かない(
    fake_libs: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    opened: list[object] = []
    real_open = builtins.open

    def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    camera = PiCamera(flip=False)
    camera.start()
    camera.capture_still()
    assert opened == []
    assert list(tmp_path.iterdir()) == []


def test_始める前の静止画はカメラが使えない(fake_libs: None) -> None:
    with pytest.raises(CameraUnavailable):
        PiCamera(flip=False).capture_still()


# --- カメラが使えない（7.8） ---


def test_ライブラリがなければ始めるとカメラが使えない(no_libs: None) -> None:
    with pytest.raises(CameraUnavailable):
        PiCamera(flip=False).start()


def test_カメラがなければ始めるとカメラが使えない(
    fake_libs: None, caplog: pytest.LogCaptureFixture
) -> None:
    FakePicamera2.fail_init = IndexError("hidden detail")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(CameraUnavailable):
        PiCamera(flip=False).start()
    assert "IndexError" in caplog.text
    assert "hidden detail" not in caplog.text


def test_始められなければ閉じてカメラが使えない(
    fake_libs: None, caplog: pytest.LogCaptureFixture
) -> None:
    FakePicamera2.fail_start = RuntimeError("hidden detail")
    caplog.set_level(logging.DEBUG)
    camera = PiCamera(flip=False)
    with pytest.raises(CameraUnavailable):
        camera.start()
    assert _only().calls == ["configure", "close"]
    assert "RuntimeError" in caplog.text
    assert "hidden detail" not in caplog.text
    # 失敗の後は始めていない扱い
    with pytest.raises(CameraUnavailable):
        camera.capture()
