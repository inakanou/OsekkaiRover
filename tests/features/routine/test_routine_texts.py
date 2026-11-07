"""日課の決まった言葉と LCD の文字、呼び名の差し込み（1.5、1.6、9.2）。"""

import pytest

from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.types import Glyph
from osekkai.features.routine.texts import (
    LATER_SPEECH,
    LCD_LATER,
    LCD_SLEEP,
    STAR,
    fill_name,
    idle_info,
    lcd_done,
)


def test_星はLCDの絵の文字() -> None:
    assert chr(Glyph.STAR) == STAR


def test_決まった言葉と文字() -> None:
    assert LATER_SPEECH == "あとで できたら ボタンを おしてね"
    assert LCD_LATER == "ｱﾄﾃﾞ ﾈ"
    assert LCD_SLEEP == "ｵﾔｽﾐ"


@pytest.mark.parametrize(
    ("stars", "expected"),
    [(0, "0"), (12, "12"), (999, "999"), (1000, "999"), (123456, "999")],
)
def test_星の数は999まで(stars: int, expected: str) -> None:
    assert idle_info(stars) == STAR + expected
    assert lcd_done(stars) == "ﾔｯﾀﾈ! " + STAR + expected


def test_LCDの文字は1行に入る() -> None:
    for text in (LCD_LATER, LCD_SLEEP, lcd_done(10**9), idle_info(10**9)):
        assert len(to_lcd_codes(text)) <= 16


def test_呼び名を差し込む() -> None:
    assert fill_name("{name}、おはよう", "テストくん") == "テストくん、おはよう"
    assert fill_name("{name}と{name}", "テストくん") == "テストくんとテストくん"


@pytest.mark.parametrize("call_name", ["", "   "])
def test_呼び名がなければきみ(call_name: str) -> None:
    assert fill_name("{name}、おはよう", call_name) == "きみ、おはよう"


def test_ほかの波かっこはそのまま() -> None:
    text = "{nam} {0} {} {name!r} { name } {{name}} {"
    assert fill_name(text, "テストくん") == "{nam} {0} {} {name!r} { name } {テストくん} {"


def test_呼び名の中の波かっこは差し込みにしない() -> None:
    assert fill_name("{name}!", "{name}") == "{name}!"
