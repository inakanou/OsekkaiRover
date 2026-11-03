"""LCD1602（HD44780 と、I2C の PCF8574 の裏板）の表示のアダプタ。

PCF8574 のビット: P0=RS、P1=RW、P2=EN、P3=バックライト、P4〜P7=D4〜D7。
4 ビットモードなので、1 バイトを上位・下位の 2 つの半分に分け、EN を Low→High→Low にして送る。
絵の文字の形（core.lcd_text の GLYPH_PATTERNS）は呼び出し側から受け取る
（アダプタは core.ports と core.types だけを読み込むため）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from osekkai.adapters.pi.i2c_bus import I2cBus
from osekkai.core.types import Glyph

DEFAULT_ADDRESS: Final = 0x27
COLUMNS: Final = 16
ROW_ADDRESSES: Final[tuple[int, ...]] = (0x00, 0x40)
GLYPH_SLOTS: Final = 8
GLYPH_ROWS: Final = 8

RS: Final = 0x01
EN: Final = 0x04
BACKLIGHT: Final = 0x08

CMD_CLEAR: Final = 0x01
CMD_ENTRY_MODE: Final = 0x06  # 書くたびに右へ進む。画面はずらさない
CMD_DISPLAY_OFF: Final = 0x08
CMD_DISPLAY_ON: Final = 0x0C  # カーソルと点滅はなし
CMD_FUNCTION_SET: Final = 0x28  # 4 ビット、2 行、5×8 の点
CMD_SET_CGRAM: Final = 0x40
CMD_SET_DDRAM: Final = 0x80

POWER_ON_WAIT: Final = 0.05  # 40 ms より長く
CLEAR_WAIT: Final = 0.002  # 1.52 ms より長く


class Lcd1602Display:
    """DisplayPort の実機の版。作るときに初期化して、絵の文字を CGRAM の 0〜7 に登録する。

    書き込みは 1 バイトごとに I2C のバスのロック（HAT と共有）の中で行う。
    書き込みの失敗は OSError のまま投げる。
    """

    def __init__(
        self,
        bus: I2cBus,
        glyphs: Mapping[Glyph, Sequence[int]],
        *,
        address: int = DEFAULT_ADDRESS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._glyphs = _validate_glyphs(glyphs)
        self._bus = bus
        self._address = address
        self._sleep = sleep
        self._backlight = BACKLIGHT
        self._initialize()

    # ---- DisplayPort ----

    def write_line(self, row: int, codes: Sequence[int]) -> None:
        if row not in (0, 1):
            raise ValueError("row must be 0 or 1")
        if len(codes) != COLUMNS:
            raise ValueError(f"codes must have {COLUMNS} items")
        if any(not 0 <= c <= 0xFF for c in codes):
            raise ValueError("codes must be 0..255")
        with self._bus.lock:  # 1 行をまとめて書く
            self._command(CMD_SET_DDRAM | ROW_ADDRESSES[row])
            for code in codes:
                self._data(code)

    def set_backlight(self, on: bool) -> None:
        self._backlight = BACKLIGHT if on else 0
        self._bus.write_byte(self._address, self._backlight)

    def clear(self) -> None:
        self._command(CMD_CLEAR)
        self._sleep(CLEAR_WAIT)

    # ---- 中身 ----

    def _initialize(self) -> None:
        self._sleep(POWER_ON_WAIT)
        # 8 ビットのつもりで 3 回送ってから、4 ビットに切り替える（HD44780 の手順）
        for wait in (0.0045, 0.0045, 0.00015):
            self._nibble(0x3, 0)
            self._sleep(wait)
        self._nibble(0x2, 0)
        self._command(CMD_FUNCTION_SET)
        self._command(CMD_DISPLAY_OFF)
        self.clear()
        self._command(CMD_ENTRY_MODE)
        self._register_glyphs()
        self._command(CMD_DISPLAY_ON)

    def _register_glyphs(self) -> None:
        for code, rows in sorted(self._glyphs.items()):
            with self._bus.lock:
                self._command(CMD_SET_CGRAM | (code << 3))
                for bits in rows:
                    self._data(bits)

    def _command(self, value: int) -> None:
        self._byte(value, 0)

    def _data(self, value: int) -> None:
        self._byte(value, RS)

    def _byte(self, value: int, mode: int) -> None:
        with self._bus.lock:
            self._nibble(value >> 4, mode)
            self._nibble(value & 0x0F, mode)

    def _nibble(self, nibble: int, mode: int) -> None:
        out = ((nibble & 0x0F) << 4) | mode | self._backlight
        with self._bus.lock:
            self._bus.write_byte(self._address, out)  # RS を先に決めてから EN を上げる
            self._bus.write_byte(self._address, out | EN)
            self._bus.write_byte(self._address, out)


def _validate_glyphs(glyphs: Mapping[Glyph, Sequence[int]]) -> dict[int, tuple[int, ...]]:
    checked: dict[int, tuple[int, ...]] = {}
    for glyph, rows in glyphs.items():
        code = int(glyph)
        if not 0 <= code < GLYPH_SLOTS:
            raise ValueError("glyph code must be 0..7")
        if len(rows) != GLYPH_ROWS or any(not 0 <= r <= 0x1F for r in rows):
            raise ValueError("glyph must be 8 rows of 5 bits")
        checked[code] = tuple(rows)
    return checked
