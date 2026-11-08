"""USB のカメラ（Logicool C270）の実機のアダプタ（要件 8.1、8.3）。

- `/dev/v4l/by-id/usb-046d_0825_*-video-index0` を cv2 の V4L2 で開き、MJPG 640×480 にする
- `CAP_PROP_CONVERT_RGB=0` で、カメラの JPEG をそのまま受け取る
- C270 の JPEG はハフマン表（DHT）を持たない。ほかのソフトでも開けるように、
  JPEG の規格の附属書 K の標準の表を SOF0 の前に差し込む（ffmpeg の mjpeg2jpeg と同じ節）
- 3 チャンネルの画像で返る版の cv2 では、`cv2.imencode` で JPEG にする
- 開けない・読めないときは例外を外に出さず、偽や None で返す
- cv2 と numpy は関数の中だけで読む。表の差し込み（`ensure_dht`）は cv2 なしで使える
- ログに出すのは例外の型の名前だけ（道の名前も出さない）。同じ型の失敗は間をあけて出す
"""

from __future__ import annotations

import importlib.util
import logging
import struct
import sys
import threading
from pathlib import Path
from typing import Any, Final

from osekkai.core.clock import Clock

logger = logging.getLogger(__name__)

BY_ID_DIR: Final = Path("/dev/v4l/by-id")
C270_PATTERN: Final = "usb-046d_0825_*-video-index0"
C270_GLOB: Final = f"{BY_ID_DIR}/{C270_PATTERN}"

WIDTH: Final = 640
HEIGHT: Final = 480
JPEG_QUALITY: Final = 70
LOG_INTERVAL_S: Final = 60.0  # 同じ型の読みの失敗をログに出す間

SOI: Final = b"\xff\xd8"
EOI: Final = b"\xff\xd9"
_SOF0: Final = 0xC0
_DHT: Final = 0xC4
_SOS: Final = 0xDA
# 長さを持たない印（RST0〜7、TEM）
_STANDALONE: Final = frozenset({0x01, *range(0xD0, 0xD8)})

# JPEG の規格（ITU-T T.81）の附属書 K.3 の標準のハフマン表
_DC_LUMINANCE_BITS: Final = (0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0)
_DC_CHROMINANCE_BITS: Final = (0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0)
_DC_VALUES: Final = bytes(range(12))
_AC_LUMINANCE_BITS: Final = (0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D)
_AC_LUMINANCE_VALUES: Final = bytes.fromhex(
    "01020300041105122131410613516107"
    "227114328191a1082342b1c11552d1f0"
    "2433627282090a161718191a25262728"
    "292a3435363738393a43444546474849"
    "4a535455565758595a63646566676869"
    "6a737475767778797a83848586878889"
    "8a92939495969798999aa2a3a4a5a6a7"
    "a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5"
    "c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2"
    "e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8"
    "f9fa"
)
_AC_CHROMINANCE_BITS: Final = (0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77)
_AC_CHROMINANCE_VALUES: Final = bytes.fromhex(
    "00010203110405213106124151076171"
    "1322328108144291a1b1c109233352f0"
    "156272d10a162434e125f11718191a26"
    "2728292a35363738393a434445464748"
    "494a535455565758595a636465666768"
    "696a737475767778797a828384858687"
    "88898a92939495969798999aa2a3a4a5"
    "a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3"
    "c4c5c6c7c8c9cad2d3d4d5d6d7d8d9da"
    "e2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8"
    "f9fa"
)


def _dht_segment() -> bytes:
    tables = (
        (0x00, _DC_LUMINANCE_BITS, _DC_VALUES),
        (0x10, _AC_LUMINANCE_BITS, _AC_LUMINANCE_VALUES),
        (0x01, _DC_CHROMINANCE_BITS, _DC_VALUES),
        (0x11, _AC_CHROMINANCE_BITS, _AC_CHROMINANCE_VALUES),
    )
    body = b"".join(bytes([tc_th, *bits]) + values for tc_th, bits, values in tables)
    return b"\xff\xc4" + struct.pack(">H", len(body) + 2) + body


DHT_SEGMENT: Final = _dht_segment()


def _insert_point(jpeg: bytes) -> int | None:
    """表を差し込む位置。表がすでにある・JPEG の形でないときは None。

    SOI の後の節を SOS まで順に見る。SOF0 があればその前、なければ SOS の前に差し込む。
    """
    if not jpeg.startswith(SOI):
        return None
    pos = len(SOI)
    sof0: int | None = None
    while pos + 1 < len(jpeg):
        if jpeg[pos] != 0xFF:
            return None
        marker = jpeg[pos + 1]
        if marker == 0xFF:  # 詰め物の ff
            pos += 1
            continue
        if marker in _STANDALONE:
            pos += 2
            continue
        if marker == _DHT:
            return None
        if marker == _SOS:
            return sof0 if sof0 is not None else pos
        if marker == _SOF0 and sof0 is None:
            sof0 = pos
        if pos + 4 > len(jpeg):
            return None
        (length,) = struct.unpack(">H", jpeg[pos + 2 : pos + 4])
        if length < 2 or pos + 2 + length > len(jpeg):
            return None
        pos += 2 + length
    return None


def ensure_dht(jpeg: bytes) -> bytes:
    """DHT のない JPEG に標準の表を差し込む。表がある・JPEG の形でないときはそのまま返す。"""
    at = _insert_point(jpeg)
    if at is None:
        return jpeg
    return jpeg[:at] + DHT_SEGMENT + jpeg[at:]


def usb_camera_available() -> bool:
    """cv2 を読み込めるか。開発 PC と CI では偽。"""
    if "cv2" in sys.modules:
        return sys.modules["cv2"] is not None
    try:
        return importlib.util.find_spec("cv2") is not None
    except (ImportError, ValueError):
        return False


def find_usb_camera(configured: str, *, by_id_dir: Path = BY_ID_DIR) -> str | None:
    """USB のカメラの道。見つからなければ None。

    設定に道があれば、それがあるときだけ使う（ほかの道に倒さない）。
    空なら、by-id の C270 の映像の口（video-index0）を名前の順で最初に見つかったものにする。
    """
    if configured:
        return configured if Path(configured).exists() else None
    try:
        found = sorted(by_id_dir.glob(C270_PATTERN))
    except OSError:
        return None
    return str(found[0]) if found else None


def _import_cv2() -> Any:
    import cv2  # 実機だけ

    return cv2


class UsbCamera:
    """C270 を cv2 で読む（UsbCameraPort）。

    `read_jpeg` は 1 本の読み取りのスレッドから呼ぶ。開け閉めと読み取りはロックで分ける。
    """

    def __init__(self, device: str, *, clock: Clock | None = None) -> None:
        self._device = device
        self._clock = clock
        self._lock = threading.Lock()
        self._cv2: Any = None
        self._cap: Any = None
        self._logged_at: dict[str, float] = {}

    def open(self) -> bool:
        with self._lock:
            self._release()
            try:
                cv2 = _import_cv2()
                cap = cv2.VideoCapture(self._device, cv2.CAP_V4L2)
            except Exception as e:  # ImportError・cv2.error など
                self._warn("open", e)
                return False
            try:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
                opened = bool(cap.isOpened())
            except Exception as e:
                self._warn("open", e)
                opened = False
            if not opened:
                self._quietly_release(cap)
                return False
            self._cv2 = cv2
            self._cap = cap
            return True

    def read_jpeg(self) -> bytes | None:
        with self._lock:
            if self._cap is None:
                return None
            try:
                ok, frame = self._cap.read()
                if not ok or frame is None:
                    return None
                return self._to_jpeg(frame)
            except Exception as e:  # cv2.error など
                self._warn("read", e)
                return None

    def close(self) -> None:
        with self._lock:
            self._release()

    def _to_jpeg(self, frame: Any) -> bytes | None:
        ndim = getattr(frame, "ndim", None)
        # 生の JPEG は 1 チャンネルのバイトの並び。V4L2 は (1, N) の 2 次元で返す
        flat = ndim == 1 or (ndim == 2 and 1 in tuple(frame.shape))
        if flat and str(frame.dtype) == "uint8":
            data = bytes(frame.tobytes())
            end = data.rfind(EOI)
            if not data.startswith(SOI) or end < 0:
                return None
            return ensure_dht(data[: end + len(EOI)])
        if ndim == 3 and frame.shape[2] == 3:
            cv2 = self._cv2
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            return bytes(encoded.tobytes()) if ok else None
        return None

    def _release(self) -> None:
        cap, self._cap, self._cv2 = self._cap, None, None
        if cap is not None:
            self._quietly_release(cap)

    @staticmethod
    def _quietly_release(cap: Any) -> None:
        try:
            cap.release()
        except Exception as e:
            logger.debug("USB のカメラを閉じられませんでした: %s", type(e).__name__)

    def _warn(self, kind: str, error: BaseException) -> None:
        """同じ種類と型の失敗は、時計があれば間をあけて、なければ一度だけログに出す。"""
        key = f"{kind}:{type(error).__name__}"
        last = self._logged_at.get(key)
        now = self._clock.monotonic() if self._clock is not None else 0.0
        if last is not None and (self._clock is None or now - last < LOG_INTERVAL_S):
            return
        self._logged_at[key] = now
        logger.warning("USB のカメラを使えませんでした（%s）: %s", kind, type(error).__name__)
