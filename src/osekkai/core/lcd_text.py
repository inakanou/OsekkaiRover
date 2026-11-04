"""LCD（HD44780 の A00 の ROM）に出す文字の変換と、絵の文字の形（純粋な関数）。"""

import unicodedata
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from osekkai.core.types import Glyph

_UNKNOWN: Final = ord("?")
_DAKUTEN: Final = 0xDE
_HANDAKUTEN: Final = 0xDF
_COMBINING_MARKS: Final = {"゙": _DAKUTEN, "゚": _HANDAKUTEN}


def _build_kana_table() -> dict[str, int]:
    # 半角カタカナ（U+FF61〜U+FF9F）は ROM の 0xA1〜0xDF に並ぶ
    table: dict[str, int] = {}
    for cp in range(0xFF61, 0xFFA0):
        full = unicodedata.normalize("NFKC", chr(cp))
        if len(full) == 1:
            table[full] = cp - 0xFF61 + 0xA1
    # 単独の濁点・半濁点と、半角の形がない小さいかな
    table.update({"゛": _DAKUTEN, "゜": _HANDAKUTEN})
    table.update({"ヵ": table["カ"], "ヶ": table["ケ"], "ヮ": table["ワ"]})
    return table


_KANA: Final = _build_kana_table()


def _char_codes(ch: str) -> list[int]:
    cp = ord(ch)
    if cp < len(Glyph):  # 絵の文字（CGRAM の 0〜7）
        return [cp]
    if 0x20 <= cp <= 0x7D:
        return [cp]
    if 0x3041 <= cp <= 0x3096:  # ひらがな → カタカナ
        ch = chr(cp + 0x60)
    if ch in _KANA:
        return [_KANA[ch]]
    base_mark = unicodedata.normalize("NFD", ch)
    if len(base_mark) == 2 and base_mark[0] in _KANA and base_mark[1] in _COMBINING_MARKS:
        return [_KANA[base_mark[0]], _COMBINING_MARKS[base_mark[1]]]
    return [_UNKNOWN]


def to_lcd_codes(text: str) -> list[int]:
    """文字列を LCD のコードの並びに変える。濁点・半濁点は別のセルにする（7.2）。"""
    codes: list[int] = []
    for raw in text:
        if ord(raw) < len(Glyph):
            codes.append(ord(raw))
            continue
        if raw in _KANA:  # 単独の「゛」「゜」は NFKC で空白＋結合文字に分かれるので先に引く
            codes.append(_KANA[raw])
            continue
        # 全角の英数字や半角カタカナを、まずふつうの形にそろえる
        for ch in unicodedata.normalize("NFKC", raw):
            codes.extend(_char_codes(ch))
    return codes


def center(text: str, width: int = 16) -> str:
    """変換後のセル数で数えて、左に空白を足して中央に置く（7.4）。"""
    cells = len(to_lcd_codes(text))
    if cells >= width:
        return text
    return " " * ((width - cells) // 2) + text


_PATTERNS: dict[Glyph, tuple[int, ...]] = {
    Glyph.EYE: (
        0b00000,
        0b01110,
        0b11111,
        0b11011,
        0b11011,
        0b11111,
        0b01110,
        0b00000,
    ),
    Glyph.EYE_HAPPY: (
        0b00000,
        0b00000,
        0b01110,
        0b10001,
        0b10001,
        0b00000,
        0b00000,
        0b00000,
    ),
    Glyph.EYE_CLOSED: (
        0b00000,
        0b00000,
        0b00000,
        0b10001,
        0b10001,
        0b01110,
        0b00000,
        0b00000,
    ),
    Glyph.SMILE: (
        0b00000,
        0b00000,
        0b00000,
        0b10001,
        0b10001,
        0b01110,
        0b00100,
        0b00000,
    ),
    Glyph.MOUTH_O: (
        0b00000,
        0b00000,
        0b01110,
        0b10001,
        0b10001,
        0b10001,
        0b01110,
        0b00000,
    ),
    Glyph.STAR: (
        0b00100,
        0b00100,
        0b11111,
        0b01110,
        0b01110,
        0b11011,
        0b10001,
        0b00000,
    ),
    Glyph.HEART: (
        0b00000,
        0b01010,
        0b11111,
        0b11111,
        0b11111,
        0b01110,
        0b00100,
        0b00000,
    ),
    Glyph.ZZZ: (
        0b11110,
        0b00100,
        0b01000,
        0b11110,
        0b00000,
        0b00111,
        0b00010,
        0b00111,
    ),
}

GLYPH_PATTERNS: Final[Mapping[Glyph, tuple[int, ...]]] = MappingProxyType(_PATTERNS)
"""絵の文字の 5×8 の形。各行の下位 5 ビットが左から右の点（7.3）。"""
