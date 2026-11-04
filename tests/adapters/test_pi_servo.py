"""サーボのアダプタのテスト。本物の HatMcu に偽の smbus を渡し、書いたレジスタを確かめる。"""

from __future__ import annotations

from pathlib import Path

import pytest

from osekkai.adapters.pi.hat import HatMcu
from osekkai.adapters.pi.i2c_bus import I2cBus
from osekkai.adapters.pi.picarx import (
    PiCarXServos,
    angle_to_pulse_us,
    prescaler_for,
    pulse_us_to_value,
)
from osekkai.core.ports import ServoPort


class FakeSMBus:
    def __init__(self) -> None:
        self.writes: list[tuple[int, int, list[int]]] = []
        self.fail_write = False

    def write_quick(self, addr: int) -> None:
        if addr != 0x14:
            raise OSError(121, "Remote I/O error")

    def write_byte(self, addr: int, value: int) -> None:
        self.writes.append((addr, value, []))

    def write_i2c_block_data(self, addr: int, reg: int, data: list[int]) -> None:
        if self.fail_write:
            raise OSError(5, "Input/output error")
        self.writes.append((addr, reg, list(data)))

    def close(self) -> None:
        pass


class NoGpio:
    def claim_output(self, pin: int, level: int) -> None:
        pass

    def write(self, pin: int, level: int) -> None:
        pass


def _make(tmp_path: Path) -> tuple[PiCarXServos, FakeSMBus]:
    fake = FakeSMBus()
    bus = I2cBus(opener=lambda n: fake)
    hat = HatMcu(bus, NoGpio(), sleep=lambda s: None, device_tree=tmp_path)
    servos = PiCarXServos(hat)
    return servos, fake


def _word(value: int) -> list[int]:
    return [value >> 8, value & 0xFF]


# ---- 純粋な変換 ----


def test_50ヘルツと周期4095のプリスケーラは352() -> None:
    # robot-hat: 72e6 / 50 / 4095 = 351.6 → round → 352
    assert prescaler_for(50, 4095) == 352


def test_角度はマイナス90から90を500から2500マイクロ秒に写す() -> None:
    assert angle_to_pulse_us(-90) == 500
    assert angle_to_pulse_us(0) == 1500
    assert angle_to_pulse_us(90) == 2500
    assert angle_to_pulse_us(45) == 2000


def test_範囲の外の角度は端に収める() -> None:
    assert angle_to_pulse_us(-200) == 500
    assert angle_to_pulse_us(120) == 2500


def test_有限でない角度は断る() -> None:
    with pytest.raises(ValueError):
        angle_to_pulse_us(float("nan"))
    with pytest.raises(ValueError):
        angle_to_pulse_us(float("inf"))


def test_パルス幅は20ミリ秒に対する割合で切り捨てて値にする() -> None:
    assert pulse_us_to_value(500) == 102  # 0.025 × 4095 = 102.375
    assert pulse_us_to_value(1500) == 307  # 0.075 × 4095 = 307.125
    assert pulse_us_to_value(2500) == 511  # 0.125 × 4095 = 511.875
    assert pulse_us_to_value(3000) == 511


# ---- 実機版のアダプタ ----


def test_作るとタイマー0を周期4095とプリスケーラ352にする(tmp_path: Path) -> None:
    _, fake = _make(tmp_path)
    # P0〜P2 は timer 0 を共有するので 1 回ずつ。プリスケーラは「値 − 1」を書く
    assert fake.writes == [(0x14, 0x44, _word(4095)), (0x14, 0x40, _word(351))]


def test_ServoPortを満たす(tmp_path: Path) -> None:
    servos, _ = _make(tmp_path)
    port: ServoPort = servos
    assert port is servos


def test_ステアリングはP2に反転せずに書く(tmp_path: Path) -> None:
    servos, fake = _make(tmp_path)
    fake.writes.clear()
    servos.set_angle("steering", 30)
    # 30° → 1833.3 µs → int(0.091666 × 4095) = 375
    assert fake.writes == [(0x14, 0x22, _word(375))]


def test_パンはP0に符号を反転して書く(tmp_path: Path) -> None:
    servos, fake = _make(tmp_path)
    fake.writes.clear()
    servos.set_angle("pan", 30)
    # −30° → 1166.7 µs → 238
    assert fake.writes == [(0x14, 0x20, _word(238))]


def test_チルトはP1に符号を反転して書く(tmp_path: Path) -> None:
    servos, fake = _make(tmp_path)
    fake.writes.clear()
    servos.set_angle("tilt", -90)
    assert fake.writes == [(0x14, 0x21, _word(511))]


def test_正面は1500マイクロ秒(tmp_path: Path) -> None:
    servos, fake = _make(tmp_path)
    fake.writes.clear()
    for ch in ("pan", "tilt", "steering"):
        servos.set_angle(ch, 0)
    assert [w[2] for w in fake.writes] == [_word(307)] * 3


def test_範囲の外は反転の後に端に収める(tmp_path: Path) -> None:
    servos, fake = _make(tmp_path)
    fake.writes.clear()
    servos.set_angle("pan", 500)
    servos.set_angle("steering", -500)
    assert fake.writes == [(0x14, 0x20, _word(102)), (0x14, 0x22, _word(102))]


def test_知らないチャンネルは断る(tmp_path: Path) -> None:
    servos, fake = _make(tmp_path)
    fake.writes.clear()
    with pytest.raises(ValueError):
        servos.set_angle("wheel", 0)  # type: ignore[arg-type]
    assert fake.writes == []


def test_書き込みの失敗はOSErrorのまま投げる(tmp_path: Path) -> None:
    servos, fake = _make(tmp_path)
    fake.fail_write = True
    with pytest.raises(OSError):
        servos.set_angle("pan", 10)


def test_作るときの書き込みの失敗もOSErrorのまま投げる(tmp_path: Path) -> None:
    fake = FakeSMBus()
    fake.fail_write = True
    bus = I2cBus(opener=lambda n: fake)
    hat = HatMcu(bus, NoGpio(), sleep=lambda s: None, device_tree=tmp_path)
    with pytest.raises(OSError):
        PiCarXServos(hat)
