"""USB のカメラ（C270）の実機のアダプタ（要件 8.1、8.3）。

偽の cv2 を sys.modules に入れて確かめる。JPEG は小さな作りもののバイト列を使う。
実機のライブラリ（cv2・numpy）は読まない。
"""

from __future__ import annotations

import importlib.util
import logging
import struct
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from remote_fakes import FakeClock

from osekkai.features.remote.adapters import usb_camera
from osekkai.features.remote.adapters.usb_camera import (
    C270_GLOB,
    DHT_SEGMENT,
    UsbCamera,
    ensure_dht,
    find_usb_camera,
    usb_camera_available,
)
from osekkai.features.remote.ports import UsbCameraPort

# --- 作りものの JPEG ---

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
APP0 = b"\xff\xe0\x00\x06AVI1"
DQT = b"\xff\xdb\x00\x04\x00\x01"
SOF0 = b"\xff\xc0\x00\x0b\x08\x01\xe0\x02\x80\x01\x01\x11\x00"
SOS = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
SCAN = b"\x12\x34\xff\x00\x56"
OWN_DHT = b"\xff\xc4\x00\x05\x00\x00\x00"

NO_DHT = SOI + APP0 + DQT + SOF0 + SOS + SCAN + EOI
WITH_DHT = SOI + APP0 + DQT + OWN_DHT + SOF0 + SOS + SCAN + EOI


# --- 偽の cv2 ---


class FakeFrame:
    """cv2 が返す配列の偽物。ndim・dtype・shape・tobytes だけを持つ。"""

    def __init__(self, data: bytes, *, shape: tuple[int, ...] | None = None) -> None:
        self._data = data
        # 本物の OpenCV の V4L2 は、CONVERT_RGB=0 で (1, N) の配列を返す
        self.shape = shape if shape is not None else (1, len(data))
        self.ndim = len(self.shape)
        self.dtype = "uint8"

    def tobytes(self) -> bytes:
        return self._data


class FakeCapture:
    def __init__(self, cv2: FakeCv2, device: str, api: int) -> None:
        self._cv2 = cv2
        self.device = device
        self.api = api
        self.props: list[tuple[int, float]] = []
        self.released = 0

    def isOpened(self) -> bool:
        return self._cv2.opened

    def set(self, prop: int, value: float) -> bool:
        self.props.append((prop, value))
        return True

    def read(self) -> tuple[bool, Any]:
        if not self._cv2.frames:
            return False, None
        item = self._cv2.frames.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def release(self) -> None:
        self.released += 1


class FakeCv2(ModuleType):
    """cv2 の偽物。sys.modules に入れて、関数の中の import cv2 に読ませる。"""

    CAP_V4L2 = 200
    CAP_PROP_FOURCC = 6
    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4
    CAP_PROP_BUFFERSIZE = 38
    CAP_PROP_CONVERT_RGB = 16
    IMWRITE_JPEG_QUALITY = 1

    def __init__(self) -> None:
        super().__init__("cv2")
        self.opened = True
        self.captures: list[FakeCapture] = []
        self.frames: list[tuple[bool, Any] | Exception] = []
        self.encoded: list[tuple[str, Any, list[int]]] = []
        self.fail_capture: Exception | None = None

    @staticmethod
    def VideoWriter_fourcc(*chars: str) -> int:
        a, b, c, d = (ord(ch) for ch in chars)
        return a | (b << 8) | (c << 16) | (d << 24)

    def VideoCapture(self, device: str, api: int) -> FakeCapture:
        if self.fail_capture is not None:
            raise self.fail_capture
        cap = FakeCapture(self, device, api)
        self.captures.append(cap)
        return cap

    def imencode(self, ext: str, frame: Any, params: list[int]) -> tuple[bool, FakeFrame]:
        self.encoded.append((ext, frame, params))
        return True, FakeFrame(b"encoded")


@pytest.fixture
def cv2(monkeypatch: pytest.MonkeyPatch) -> FakeCv2:
    fake = FakeCv2()
    monkeypatch.setitem(sys.modules, "cv2", fake)
    return fake


def _jpeg_frame(data: bytes) -> tuple[bool, FakeFrame]:
    return True, FakeFrame(data)


def _segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


# --- 標準のハフマン表（DHT） ---


def _tables(segment: bytes) -> list[tuple[int, list[int], bytes]]:
    """DHT の節を (クラスと番号, 長さごとの数, 値) に分ける。"""
    assert segment[:2] == b"\xff\xc4"
    (length,) = struct.unpack(">H", segment[2:4])
    assert length == len(segment) - 2
    body = segment[4:]
    tables = []
    while body:
        tc_th = body[0]
        counts = list(body[1:17])
        values = body[17 : 17 + sum(counts)]
        tables.append((tc_th, counts, values))
        body = body[17 + sum(counts) :]
    return tables


def test_標準の表は4つで_長さは418() -> None:
    assert DHT_SEGMENT[:4] == b"\xff\xc4\x01\xa2"
    assert len(DHT_SEGMENT) == 420
    assert [t[0] for t in _tables(DHT_SEGMENT)] == [0x00, 0x10, 0x01, 0x11]


def test_標準の表の値は規格の附属書Kのとおり() -> None:
    dc_l, ac_l, dc_c, ac_c = _tables(DHT_SEGMENT)
    assert dc_l[1] == [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
    assert dc_c[1] == [0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
    assert ac_l[1] == [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D]
    assert ac_c[1] == [0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77]
    assert dc_l[2] == bytes(range(12))
    assert dc_c[2] == bytes(range(12))
    # AC の値は、0x00（EOB）と 0xF0（ZRL）と、0 の数（上の 4 ビット）と大きさ 1〜10 の組
    expected = {0x00, 0xF0} | {(run << 4) | size for run in range(16) for size in range(1, 11)}
    for table in (ac_l, ac_c):
        assert len(table[2]) == 162
        assert set(table[2]) == expected
    assert ac_l[2][:12] == bytes([1, 2, 3, 0, 4, 0x11, 5, 0x12, 0x21, 0x31, 0x41, 6])
    assert ac_c[2][:12] == bytes([0, 1, 2, 3, 0x11, 4, 5, 0x21, 0x31, 6, 0x12, 0x41])
    assert ac_l[2][-2:] == b"\xf9\xfa"
    assert ac_c[2][-2:] == b"\xf9\xfa"


def test_標準の表の符号は長さの中に収まる() -> None:
    for _, counts, _ in _tables(DHT_SEGMENT):
        code = 0
        for length, count in enumerate(counts, start=1):
            code += count
            assert code <= (1 << length)  # 符号が長さのビットからあふれない
            code <<= 1


# --- DHT の差し込み ---


def test_DHT_がなければ_SOF0_の直前に差し込む() -> None:
    assert ensure_dht(NO_DHT) == SOI + APP0 + DQT + DHT_SEGMENT + SOF0 + SOS + SCAN + EOI


def test_DHT_があれば変えない() -> None:
    assert ensure_dht(WITH_DHT) == WITH_DHT


def test_節の中身の_FFC4_のような並びは数えない() -> None:
    # 表の中身に ff c4 の並びがあっても、DHT の節ではない
    dqt = _segment(0xDB, b"\x00\xff\xc4\x01")
    jpeg = SOI + dqt + SOF0 + SOS + SCAN + EOI
    assert ensure_dht(jpeg) == SOI + dqt + DHT_SEGMENT + SOF0 + SOS + SCAN + EOI


def test_SOF0_がなければ_SOS_の直前に差し込む() -> None:
    sof2 = b"\xff\xc2" + SOF0[2:]
    jpeg = SOI + DQT + sof2 + SOS + SCAN + EOI
    assert ensure_dht(jpeg) == SOI + DQT + sof2 + DHT_SEGMENT + SOS + SCAN + EOI


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not a jpeg",
        SOI + b"\xff\xdb\x00",  # 長さが切れている
        SOI + b"\xff\xdb\x00\x40\x00",  # 長さが残りより長い
        SOI + b"\x00\x00" + SOF0 + SOS + EOI,  # 印の前に ff がない
        SOI + APP0 + DQT,  # SOS も SOF もない
    ],
)
def test_JPEG_の形でなければ変えない(data: bytes) -> None:
    assert ensure_dht(data) == data


# --- C270 の見つけ方 ---

C270_NAME = "usb-046d_0825_ABCD1234-video-index0"


def test_C270_の道の形() -> None:
    assert C270_GLOB == "/dev/v4l/by-id/usb-046d_0825_*-video-index0"


def test_設定の道があればそれを使う(tmp_path: Path) -> None:
    device = tmp_path / "video9"
    device.touch()
    (tmp_path / C270_NAME).touch()
    assert find_usb_camera(str(device), by_id_dir=tmp_path) == str(device)


def test_設定の道がなければ見つからない(tmp_path: Path) -> None:
    (tmp_path / C270_NAME).touch()
    assert find_usb_camera(str(tmp_path / "nothing"), by_id_dir=tmp_path) is None


def test_設定が空なら_C270_の最初の道を使う(tmp_path: Path) -> None:
    (tmp_path / "usb-046d_0825_ZZZZ-video-index0").touch()
    (tmp_path / C270_NAME).touch()
    (tmp_path / "usb-046d_0825_AAAA-video-index1").touch()  # 2 つ目の口は映像ではない
    (tmp_path / "usb-1234_5678_AAAA-video-index0").touch()  # ほかのカメラ
    assert find_usb_camera("", by_id_dir=tmp_path) == str(tmp_path / C270_NAME)


def test_C270_がなければ見つからない(tmp_path: Path) -> None:
    (tmp_path / "usb-1234_5678_AAAA-video-index0").touch()
    assert find_usb_camera("", by_id_dir=tmp_path) is None
    assert find_usb_camera("", by_id_dir=tmp_path / "missing") is None


# --- ライブラリがあるか ---


def test_cv2_があるかを_find_spec_で見る(monkeypatch: pytest.MonkeyPatch) -> None:
    found: list[str] = []

    def fake_find_spec(name: str) -> object | None:
        found.append(name)
        return object()

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.delitem(sys.modules, "cv2", raising=False)
    assert usb_camera_available() is True
    assert found == ["cv2"]


def test_cv2_がなければ使えない(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    monkeypatch.delitem(sys.modules, "cv2", raising=False)
    assert usb_camera_available() is False


# --- 開け閉めと読み取り ---

DEVICE = "/dev/v4l/by-id/" + C270_NAME


def test_口の型を満たす() -> None:
    assert isinstance(UsbCamera(DEVICE), UsbCameraPort)


def test_V4L2_で開き_MJPG_にしてから大きさとバッファを決める(cv2: FakeCv2) -> None:
    camera = UsbCamera(DEVICE)
    assert camera.open() is True
    (cap,) = cv2.captures
    assert (cap.device, cap.api) == (DEVICE, FakeCv2.CAP_V4L2)
    assert cap.props == [
        (FakeCv2.CAP_PROP_FOURCC, FakeCv2.VideoWriter_fourcc(*"MJPG")),
        (FakeCv2.CAP_PROP_FRAME_WIDTH, 640),
        (FakeCv2.CAP_PROP_FRAME_HEIGHT, 480),
        (FakeCv2.CAP_PROP_BUFFERSIZE, 1),
        (FakeCv2.CAP_PROP_CONVERT_RGB, 0),
    ]


def test_開けなければ偽で_後始末をする(cv2: FakeCv2) -> None:
    cv2.opened = False
    camera = UsbCamera(DEVICE)
    assert camera.open() is False
    assert cv2.captures[0].released == 1
    assert camera.read_jpeg() is None


def test_cv2_を読めなければ開けない(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "cv2", None)
    assert UsbCamera(DEVICE).open() is False


def test_開くときの例外は外に出さない(cv2: FakeCv2, caplog: pytest.LogCaptureFixture) -> None:
    cv2.fail_capture = OSError("hidden")
    with caplog.at_level(logging.DEBUG, logger=usb_camera.__name__):
        assert UsbCamera(DEVICE).open() is False
    assert "OSError" in caplog.text
    assert "hidden" not in caplog.text
    assert C270_NAME not in caplog.text


def test_開く前は読めない(cv2: FakeCv2) -> None:
    cv2.frames.append(_jpeg_frame(WITH_DHT))
    assert UsbCamera(DEVICE).read_jpeg() is None


def test_カメラの_JPEG_に表を差し込み_終わりの後を切る(cv2: FakeCv2) -> None:
    camera = UsbCamera(DEVICE)
    assert camera.open()
    cv2.frames.append(_jpeg_frame(NO_DHT + b"\x00\x00\x00"))
    assert camera.read_jpeg() == ensure_dht(NO_DHT)


@pytest.mark.parametrize("shape", [(1, len(NO_DHT)), (len(NO_DHT), 1), (len(NO_DHT),)])
def test_1_行や_1_列の配列も生の_JPEG_として読む(cv2: FakeCv2, shape: tuple[int, ...]) -> None:
    camera = UsbCamera(DEVICE)
    assert camera.open()
    cv2.frames.append((True, FakeFrame(NO_DHT, shape=shape)))
    assert camera.read_jpeg() == ensure_dht(NO_DHT)


def test_表のある_JPEG_はそのまま返す(cv2: FakeCv2) -> None:
    camera = UsbCamera(DEVICE)
    assert camera.open()
    cv2.frames.append(_jpeg_frame(WITH_DHT))
    assert camera.read_jpeg() == WITH_DHT
    assert cv2.encoded == []


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00\x01" + NO_DHT,  # FFD8 で始まらない
        NO_DHT[:-2],  # FFD9 がない
    ],
)
def test_JPEG_でない_バッファは失敗(cv2: FakeCv2, data: bytes) -> None:
    camera = UsbCamera(DEVICE)
    assert camera.open()
    cv2.frames.append(_jpeg_frame(data))
    assert camera.read_jpeg() is None


def test_3_チャンネルの画像なら品質70で_JPEG_にする(cv2: FakeCv2) -> None:
    camera = UsbCamera(DEVICE)
    assert camera.open()
    image = FakeFrame(b"\x00" * 12, shape=(2, 2, 3))
    cv2.frames.append((True, image))
    assert camera.read_jpeg() == b"encoded"
    assert cv2.encoded == [(".jpg", image, [FakeCv2.IMWRITE_JPEG_QUALITY, 70])]


def test_ほかの形の画像は失敗(cv2: FakeCv2) -> None:
    camera = UsbCamera(DEVICE)
    assert camera.open()
    cv2.frames.append((True, FakeFrame(b"\x00" * 4, shape=(2, 2))))
    assert camera.read_jpeg() is None
    assert cv2.encoded == []


def test_読めなければ失敗(cv2: FakeCv2) -> None:
    camera = UsbCamera(DEVICE)
    assert camera.open()
    cv2.frames.append((False, None))
    assert camera.read_jpeg() is None
    assert camera.read_jpeg() is None  # 読む物がない


def test_読み取りの例外は外に出さず_型の名前を間をあけてログに出す(
    cv2: FakeCv2, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock()
    camera = UsbCamera(DEVICE, clock=clock)
    assert camera.open()
    cv2.frames.extend([RuntimeError("hidden"), RuntimeError("hidden")])
    with caplog.at_level(logging.WARNING, logger=usb_camera.__name__):
        assert camera.read_jpeg() is None
        assert camera.read_jpeg() is None
    assert caplog.text.count("RuntimeError") == 1
    assert "hidden" not in caplog.text
    assert C270_NAME not in caplog.text
    clock.advance(61.0)
    cv2.frames.append(RuntimeError("hidden"))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=usb_camera.__name__):
        assert camera.read_jpeg() is None
    assert caplog.text.count("RuntimeError") == 1


def test_時計がなければ同じ型の失敗は一度だけログに出す(
    cv2: FakeCv2, caplog: pytest.LogCaptureFixture
) -> None:
    camera = UsbCamera(DEVICE)
    assert camera.open()
    cv2.frames.extend([RuntimeError("hidden"), RuntimeError("hidden"), ValueError("hidden")])
    with caplog.at_level(logging.WARNING, logger=usb_camera.__name__):
        for _ in range(3):
            assert camera.read_jpeg() is None
    assert caplog.text.count("RuntimeError") == 1
    assert caplog.text.count("ValueError") == 1


def test_閉じるのは何度でもよい(cv2: FakeCv2) -> None:
    camera = UsbCamera(DEVICE)
    camera.close()  # 開く前
    assert camera.open()
    camera.close()
    camera.close()
    assert cv2.captures[0].released == 1
    cv2.frames.append(_jpeg_frame(WITH_DHT))
    assert camera.read_jpeg() is None


def test_開き直すと前の口を閉じる(cv2: FakeCv2) -> None:
    camera = UsbCamera(DEVICE)
    assert camera.open()
    assert camera.open()
    assert [c.released for c in cv2.captures] == [1, 0]
    camera.close()
    assert [c.released for c in cv2.captures] == [1, 1]
