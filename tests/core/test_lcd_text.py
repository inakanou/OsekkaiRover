"""LCD の文字の変換・中央寄せ・絵の文字・顔（7.2、7.3、7.4）。"""

import pytest

from osekkai.core.lcd_text import GLYPH_PATTERNS, center, to_lcd_codes
from osekkai.core.types import Face, Glyph

Q = ord("?")


def test_おはようは半角カタカナのコードになる() -> None:
    assert to_lcd_codes("おはよう") == [0xB5, 0xCA, 0xD6, 0xB3]


def test_カタカナと半角カタカナも同じコードになる() -> None:
    assert to_lcd_codes("オハヨウ") == [0xB5, 0xCA, 0xD6, 0xB3]
    assert to_lcd_codes("ｵﾊﾖｳ") == [0xB5, 0xCA, 0xD6, 0xB3]


def test_濁点は別のセルになる() -> None:
    assert to_lcd_codes("が") == [0xB6, 0xDE]
    assert to_lcd_codes("ガ") == [0xB6, 0xDE]
    assert to_lcd_codes("ｶﾞ") == [0xB6, 0xDE]
    assert to_lcd_codes("ヴ") == [0xB3, 0xDE]


def test_半濁点は別のセルになる() -> None:
    assert to_lcd_codes("ぱ") == [0xCA, 0xDF]


def test_小さいかなと長音と記号() -> None:
    assert to_lcd_codes("ゃッ") == [0xAC, 0xAF]
    assert to_lcd_codes("ー") == [0xB0]
    assert to_lcd_codes("。「」、・") == [0xA1, 0xA2, 0xA3, 0xA4, 0xA5]
    assert to_lcd_codes("ヲン") == [0xA6, 0xDD]


def test_ASCIIの表示できる範囲はそのまま() -> None:
    text = "".join(chr(c) for c in range(0x20, 0x7E))
    assert to_lcd_codes(text) == list(range(0x20, 0x7E))


def test_全角の英数字と空白は半角になる() -> None:
    assert to_lcd_codes("Ａ１！　") == [ord("A"), ord("1"), ord("!"), 0x20]


@pytest.mark.parametrize("text", ["漢", "~", "😀", "\n", "\x7f", "é"])
def test_表示できない文字は疑問符になる(text: str) -> None:
    assert to_lcd_codes(text) == [Q]


def test_漢字はひと文字ずつ疑問符になる() -> None:
    assert to_lcd_codes("今日は") == [Q, Q, 0xCA]


def test_絵の文字のコードは通す() -> None:
    text = "".join(chr(g) for g in Glyph)
    assert to_lcd_codes(text) == list(range(8))


def test_中央寄せは濁点も数える() -> None:
    # がっこう は ｶ ﾞ ｯ ｺ ｳ の 5 セル
    assert len(to_lcd_codes("がっこう")) == 5
    assert center("がっこう") == " " * ((16 - 5) // 2) + "がっこう"


def test_中央寄せの幅を変えられる() -> None:
    assert center("AB", width=6) == "  AB"


def test_幅に収まらないときはそのまま() -> None:
    long = "あいうえおかきくけこさしすせそた"
    assert center(long) == long
    assert center("ABCDEFGHIJKLMNOPQ") == "ABCDEFGHIJKLMNOPQ"


def test_絵の文字は8つあり形は5かける8() -> None:
    assert set(GLYPH_PATTERNS) == set(Glyph)
    for rows in GLYPH_PATTERNS.values():
        assert len(rows) == 8
        assert all(0 <= row <= 0b11111 for row in rows)
        assert any(rows)


def test_絵の文字の形はそれぞれ違う() -> None:
    assert len(set(GLYPH_PATTERNS.values())) == len(Glyph)


def _glyphs(face: Face) -> set[Glyph]:
    return {Glyph(ord(c)) for c in face.text() if ord(c) < 8}


def test_顔は4つあり短い文字列() -> None:
    for face in Face:
        cells = to_lcd_codes(face.text())
        assert 1 <= len(cells) <= 8
        assert Q not in cells


def test_顔はそれぞれ合った絵の文字を使う() -> None:
    assert Glyph.EYE in _glyphs(Face.NORMAL)
    assert Glyph.MOUTH_O in _glyphs(Face.CALL)
    assert {Glyph.EYE_HAPPY, Glyph.SMILE} <= _glyphs(Face.HAPPY)
    assert {Glyph.EYE_CLOSED, Glyph.ZZZ} <= _glyphs(Face.SLEEP)
    assert len({face.text() for face in Face}) == 4


def test_単独の濁点と半濁点は1セルになる() -> None:
    assert to_lcd_codes("゛") == [0xDE]
    assert to_lcd_codes("゜") == [0xDF]
