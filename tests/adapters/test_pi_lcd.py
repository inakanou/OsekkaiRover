"""LCD1602 のアダプタのテスト。smbus2 は読み込まず、偽のバスで送ったバイトを確かめる。"""

from __future__ import annotations

import threading
from typing import NamedTuple

import pytest

from osekkai.adapters.pi.i2c_bus import I2cBus
from osekkai.adapters.pi.lcd1602 import Lcd1602Display
from osekkai.core.lcd_text import GLYPH_PATTERNS
from osekkai.core.types import Glyph

ADDR = 0x27
GLYPHS: dict[Glyph, tuple[int, ...]] = {
    g: tuple((int(g) + r) & 0x1F for r in range(8)) for g in Glyph
}


class FakeSMBus:
    """write_byte を記録する。書くたびに、バスのロックが持たれているかも記録する。"""

    def __init__(self) -> None:
        self.writes: list[tuple[int, int]] = []
        self.unlocked_writes = 0
        self.fail = False
        self.bus: I2cBus | None = None

    def write_quick(self, addr: int) -> None:
        pass

    def write_byte(self, addr: int, value: int) -> None:
        if self.fail:
            raise OSError(121, "Remote I/O error")
        if self.bus is not None and _free_from_other_thread(self.bus.lock):
            self.unlocked_writes += 1
        self.writes.append((addr, value))

    def write_i2c_block_data(self, addr: int, reg: int, data: list[int]) -> None:
        raise AssertionError("LCD はブロック書き込みを使わない")

    def close(self) -> None:
        pass


def _free_from_other_thread(lock: threading.RLock) -> bool:
    result: list[bool] = []

    def probe() -> None:
        got = lock.acquire(blocking=False)
        if got:
            lock.release()
        result.append(got)

    t = threading.Thread(target=probe)
    t.start()
    t.join()
    return result[0]


class Nibble(NamedTuple):
    value: int
    rs: int
    backlight: bool


def _nibbles(writes: list[tuple[int, int]]) -> list[Nibble]:
    """EN を Low→High→Low にする 3 回の書き込みを、1 つの半バイトに戻す。"""
    assert len(writes) % 3 == 0
    out: list[Nibble] = []
    for (a0, setup), (a1, high), (a2, low) in zip(
        writes[::3], writes[1::3], writes[2::3], strict=True
    ):
        assert a0 == a1 == a2 == ADDR
        assert high & 0x04, "EN が High になっていない"
        assert setup == high & ~0x04, "EN を上げる前に RS とデータを決めていない"
        assert low == high & ~0x04, "EN を Low に戻していない"
        assert not high & 0x02, "RW は常に 0（書き込み）"
        out.append(Nibble(high >> 4, high & 0x01, bool(high & 0x08)))
    return out


def _bytes(nibbles: list[Nibble]) -> list[tuple[int, int]]:
    """半バイトの組を (rs, 値) に戻す。"""
    assert len(nibbles) % 2 == 0
    result: list[tuple[int, int]] = []
    for hi, lo in zip(nibbles[::2], nibbles[1::2], strict=True):
        assert hi.rs == lo.rs
        result.append((hi.rs, (hi.value << 4) | lo.value))
    return result


def _make(
    glyphs: dict[Glyph, tuple[int, ...]] | None = None,
) -> tuple[Lcd1602Display, FakeSMBus, list[float]]:
    fake = FakeSMBus()
    bus = I2cBus(opener=lambda _n: fake)
    fake.bus = bus
    sleeps: list[float] = []
    lcd = Lcd1602Display(bus, GLYPHS if glyphs is None else glyphs, sleep=sleeps.append)
    return lcd, fake, sleeps


# ---- 初期化 ----


def test_初期化は_4_ビットモードに切り替えてから設定を送る() -> None:
    _lcd, fake, sleeps = _make(glyphs={})
    nibbles = _nibbles(fake.writes)
    assert [n.value for n in nibbles[:4]] == [0x3, 0x3, 0x3, 0x2]
    assert all(n.rs == 0 for n in nibbles[:4])
    assert _bytes(nibbles[4:]) == [(0, 0x28), (0, 0x08), (0, 0x01), (0, 0x06), (0, 0x0C)]
    assert sleeps[0] >= 0.04
    assert sleeps[1] >= 0.0041 and sleeps[2] >= 0.0041 and sleeps[3] >= 0.0001
    assert any(s >= 0.00152 for s in sleeps[4:]), "消去の後に待つ"


def test_初期化で絵の文字を_CGRAM_の_0_から_7_に登録する() -> None:
    _lcd, fake, _ = _make()
    sent = _bytes(_nibbles(fake.writes)[4:])
    start = sent.index((0, 0x06)) + 1
    glyph_part = sent[start : start + 8 * 9]
    for code in range(8):
        chunk = glyph_part[code * 9 : code * 9 + 9]
        assert chunk[0] == (0, 0x40 | (code << 3))
        assert chunk[1:] == [(1, bits) for bits in GLYPHS[Glyph(code)]]
    data = [v for rs, v in glyph_part if rs == 1]
    assert len(data) == 64
    assert sent[start + 72] == (0, 0x0C), "登録の後で表示を点ける"


def test_本物の_GLYPH_PATTERNS_をそのまま渡して_CGRAM_に登録できる() -> None:
    fake = FakeSMBus()
    Lcd1602Display(I2cBus(opener=lambda _n: fake), GLYPH_PATTERNS, sleep=lambda _s: None)
    sent = _bytes(_nibbles(fake.writes)[4:])
    for glyph in Glyph:
        at = sent.index((0, 0x40 | (int(glyph) << 3)))
        assert sent[at + 1 : at + 9] == [(1, bits) for bits in GLYPH_PATTERNS[glyph]]
    assert len(GLYPH_PATTERNS) == 8


def test_初期化はバックライトを点けたまま送る() -> None:
    _lcd, fake, _ = _make()
    assert all(n.backlight for n in _nibbles(fake.writes))


@pytest.mark.parametrize(
    "glyphs",
    [{Glyph.EYE: (0,) * 7}, {Glyph.EYE: (0x20,) + (0,) * 7}],
    ids=["行が足りない", "5ビットを超える"],
)
def test_絵の文字の形がおかしければ_ValueError(glyphs: dict[Glyph, tuple[int, ...]]) -> None:
    with pytest.raises(ValueError):
        _make(glyphs=glyphs)


# ---- 行の書き込み ----


def test_1_行目は_DDRAM_の_0x00_から_16_コードを書く() -> None:
    lcd, fake, _ = _make()
    fake.writes.clear()
    codes = [0xB1 + i for i in range(15)] + [0]
    lcd.write_line(0, codes)
    assert _bytes(_nibbles(fake.writes)) == [(0, 0x80)] + [(1, c) for c in codes]


def test_2_行目は_DDRAM_の_0x40_から書く() -> None:
    lcd, fake, _ = _make()
    fake.writes.clear()
    lcd.write_line(1, [0x20] * 16)
    sent = _bytes(_nibbles(fake.writes))
    assert sent[0] == (0, 0xC0)
    assert sent[1:] == [(1, 0x20)] * 16


@pytest.mark.parametrize("n", [0, 15, 17])
def test_16_コードでなければ_ValueError_で何も送らない(n: int) -> None:
    lcd, fake, _ = _make()
    fake.writes.clear()
    with pytest.raises(ValueError):
        lcd.write_line(0, [0x20] * n)
    assert fake.writes == []


def test_行の番号とコードの範囲を確かめる() -> None:
    lcd, fake, _ = _make()
    fake.writes.clear()
    with pytest.raises(ValueError):
        lcd.write_line(2, [0x20] * 16)
    with pytest.raises(ValueError):
        lcd.write_line(0, [0x100] + [0x20] * 15)
    assert fake.writes == []


# ---- バックライトと消去 ----


def test_バックライトを消すとビットを落とし_その後の書き込みも消えたまま() -> None:
    lcd, fake, _ = _make()
    fake.writes.clear()
    lcd.set_backlight(False)
    assert fake.writes == [(ADDR, 0x00)]
    fake.writes.clear()
    lcd.write_line(0, [0x20] * 16)
    assert not any(n.backlight for n in _nibbles(fake.writes))


def test_バックライトを点けるとビットが立つ() -> None:
    lcd, fake, _ = _make()
    lcd.set_backlight(False)
    fake.writes.clear()
    lcd.set_backlight(True)
    assert fake.writes == [(ADDR, 0x08)]
    fake.writes.clear()
    lcd.clear()
    assert all(n.backlight for n in _nibbles(fake.writes))


def test_消去は_0x01_を送って待つ() -> None:
    lcd, fake, sleeps = _make()
    fake.writes.clear()
    sleeps.clear()
    lcd.clear()
    assert _bytes(_nibbles(fake.writes)) == [(0, 0x01)]
    assert sleeps and sleeps[-1] >= 0.00152


# ---- ロックと失敗 ----


def test_すべての書き込みはバスのロックの中で行う() -> None:
    lcd, fake, _ = _make()
    lcd.write_line(0, [0x20] * 16)
    lcd.set_backlight(False)
    lcd.clear()
    assert fake.writes
    assert fake.unlocked_writes == 0


def test_書き込みの失敗は_OSError_のまま投げる() -> None:
    lcd, fake, _ = _make()
    fake.fail = True
    with pytest.raises(OSError):
        lcd.write_line(0, [0x20] * 16)
    with pytest.raises(OSError):
        lcd.set_backlight(True)
    with pytest.raises(OSError):
        lcd.clear()


def test_初期化での失敗も_OSError_のまま投げる() -> None:
    fake = FakeSMBus()
    fake.fail = True
    with pytest.raises(OSError):
        Lcd1602Display(I2cBus(opener=lambda _n: fake), GLYPHS, sleep=lambda _s: None)


def test_アドレスを変えられる() -> None:
    fake = FakeSMBus()
    Lcd1602Display(I2cBus(opener=lambda _n: fake), {}, address=0x3F, sleep=lambda _s: None)
    assert fake.writes and {a for a, _ in fake.writes} == {0x3F}
