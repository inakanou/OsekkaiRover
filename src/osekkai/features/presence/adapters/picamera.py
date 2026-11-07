"""首のカメラ（picamera2）のアダプタ（1.1、7.5、7.8）。

picamera2 と libcamera は、関数の中で遅れて読み込む（開発 PC と CI にはない）。
映像は 640×480 の RGB888（並びは BGR として扱う。サンプルと同じ）。
上下の反転は、このアダプタの中だけで当てる。libcamera の変換（左右と上下の反転 = 180 度）を
映像と静止画の設定に入れるので、撮った後に回し直さない（OpenCV のアダプタは反転しない）。
静止画は JPEG にしてメモリの中で返し、ファイルには書かない（7.5）。
記録には例外の型の名前だけを残す。
"""

from __future__ import annotations

import importlib
import io
import logging
from importlib.util import find_spec
from typing import Any

from osekkai.features.presence.ports import CameraUnavailable, Frame

logger = logging.getLogger(__name__)

PICAMERA_MODULE = "picamera2"
LIBCAMERA_MODULE = "libcamera"
VIDEO_SIZE = (640, 480)
VIDEO_FORMAT = "RGB888"
STILL_FORMAT = "jpeg"
MAIN_STREAM = "main"


def picamera_available() -> bool:
    """カメラのライブラリ（picamera2）を読み込めるかを返す。読み込みはしない。"""
    try:
        return find_spec(PICAMERA_MODULE) is not None
    except ValueError:
        # すでに読み込まれていて、出どころの情報がないとき
        return True
    except ImportError:
        # 読み込めないと印が付いているとき
        return False


def camera_count() -> int:
    """つながっているカメラの台数を返す。調べられなければ 0。"""
    try:
        from picamera2 import Picamera2

        return len(Picamera2.global_camera_info())
    except Exception as exc:
        logger.debug("カメラの台数を調べられません: %s", type(exc).__name__)
        return 0


def _transform(flip: bool) -> Any:
    """反転のときは左右と上下を反転する（180 度）。libcamera は型を見ないので読み込みで取る。"""
    libcamera = importlib.import_module(LIBCAMERA_MODULE)
    value = 1 if flip else 0
    return libcamera.Transform(hflip=value, vflip=value)


class PiCamera:
    """首のカメラ。CameraPort を満たす。使えないときは CameraUnavailable を出す。"""

    def __init__(self, *, flip: bool) -> None:
        self._flip = flip
        self._camera: Any = None

    def start(self) -> None:
        """640×480 の映像で撮るのを始める。始めていれば何もしない。"""
        if self._camera is not None:
            return
        try:
            from picamera2 import Picamera2

            camera = Picamera2()
        except Exception as exc:
            logger.warning("カメラを開けません: %s", type(exc).__name__)
            raise CameraUnavailable() from None
        try:
            config = camera.create_video_configuration(
                main={"size": VIDEO_SIZE, "format": VIDEO_FORMAT},
                transform=_transform(self._flip),
            )
            camera.configure(config)
            camera.start()
        except Exception as exc:
            logger.warning("カメラを始められません: %s", type(exc).__name__)
            _close(camera)
            raise CameraUnavailable() from None
        self._camera = camera

    def stop(self) -> None:
        """撮るのを止めて、カメラを閉じる。始めていなければ何もしない。"""
        camera = self._camera
        if camera is None:
            return
        self._camera = None
        try:
            camera.stop()
        except Exception as exc:
            logger.warning("カメラを止められません: %s", type(exc).__name__)
        _close(camera)

    def capture(self) -> Frame:
        """いまの映像の 1 枚（640×480）を返す。"""
        camera = self._require()
        try:
            frame: Frame = camera.capture_array(MAIN_STREAM)
        except Exception as exc:
            logger.warning("映像を撮れません: %s", type(exc).__name__)
            raise CameraUnavailable() from None
        return frame

    def capture_still(self) -> bytes:
        """カメラの最大の大きさの静止画を JPEG で返す。メモリの中に書き、保存はしない。"""
        camera = self._require()
        buffer = io.BytesIO()
        try:
            # 大きさを指定しない静止画の設定は、カメラの最大の大きさになる
            config = camera.create_still_configuration(transform=_transform(self._flip))
            # 撮った後は映像の設定に戻る
            camera.switch_mode_and_capture_file(config, buffer, format=STILL_FORMAT)
        except Exception as exc:
            logger.warning("静止画を撮れません: %s", type(exc).__name__)
            raise CameraUnavailable() from None
        return buffer.getvalue()

    def _require(self) -> Any:
        if self._camera is None:
            raise CameraUnavailable()
        return self._camera


def _close(camera: Any) -> None:
    try:
        camera.close()
    except Exception as exc:
        logger.warning("カメラを閉じられません: %s", type(exc).__name__)
