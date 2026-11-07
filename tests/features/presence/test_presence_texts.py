"""見つけてあいさつの決まった言葉と LCD の文字（3.3〜3.6、3.8、5.1〜5.5）。"""

import pytest

from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.types import Glyph
from osekkai.features.presence import texts

LCD_WIDTH = 16


def test_覚えた人のあいさつは決まった言葉() -> None:
    assert texts.fill(texts.MORNING_KNOWN, "ゆう") == "おはよう、ゆう！きょうも よろしくね"
    assert texts.fill(texts.OKAERI_KNOWN, "ゆう") == "ゆう、おかえり！まってたよ"
    assert tuple(texts.fill(t, "ゆう") for t in texts.HELLO_KNOWN) == (
        "あ、ゆうだ！",
        "ゆう、やっほー！",
        "ゆう、あそびに きてくれたの？うれしいな",
    )


def test_知らない人のあいさつは呼び名を入れない() -> None:
    assert texts.MORNING_UNKNOWN == "おはよう！きょうも よろしくね"
    assert texts.OKAERI_UNKNOWN == "おかえり！まってたよ"
    assert texts.HELLO_UNKNOWN == (
        "あ、みつけた！",
        "やっほー！",
        "あそびに きてくれたの？うれしいな",
    )
    for text in (texts.MORNING_UNKNOWN, texts.OKAERI_UNKNOWN, *texts.HELLO_UNKNOWN):
        assert texts.NAME not in text


def test_覚えた人の言葉にはどれも呼び名の場所がある() -> None:
    for text in (texts.MORNING_KNOWN, texts.OKAERI_KNOWN, *texts.HELLO_KNOWN):
        assert text.count(texts.NAME) == 1


def test_覚えるときの言葉は決まった言葉() -> None:
    assert (
        texts.fill(texts.LEARN_START, "ゆう")
        == "ゆうの おかおを おぼえるよ。ローバーの ほうを みてね"
    )
    assert texts.LEARN_TURN == "こんどは すこし よこを むいてみて"
    assert texts.fill(texts.LEARN_DONE, "ゆう") == "おぼえたよ！これからは ゆうって よぶね"
    assert texts.LEARN_FAILED == (
        "うまく おぼえられなかった。あかるい ところで もういちど やってみてね"
    )


@pytest.mark.parametrize("name", ["{name}", "{0}", "{}", "%s", "{name}{name}", "a{b"])
def test_呼び名の差し込みは書式の記号をそのまま入れる(name: str) -> None:
    assert texts.fill(texts.OKAERI_KNOWN, name) == f"{name}、おかえり！まってたよ"


def test_LCD_の文字は決まった形() -> None:
    heart = chr(Glyph.HEART)
    found = texts.LCD_FOUND
    assert found == f"{heart} ﾐﾂｹﾀ! {heart}"
    assert texts.lcd_learning(0) == "ｵﾎﾞｴﾃﾙﾖ 0/40"
    assert texts.lcd_learning(12) == "ｵﾎﾞｴﾃﾙﾖ 12/40"
    assert texts.lcd_learning(40) == "ｵﾎﾞｴﾃﾙﾖ 40/40"
    assert texts.LCD_LEARNED == "ｵﾎﾞｴﾀﾖ!"


@pytest.mark.parametrize("count", [0, 9, 10, 39, 40])
def test_LCD_の文字は_16_セルに収まり変換できない文字がない(count: int) -> None:
    for text in (texts.LCD_FOUND, texts.lcd_learning(count), texts.LCD_LEARNED):
        codes = to_lcd_codes(text)
        assert len(codes) <= LCD_WIDTH
        assert ord("?") not in codes
    assert to_lcd_codes(texts.LCD_FOUND)[0] == Glyph.HEART


def test_集める数は_40() -> None:
    assert texts.LEARN_SAMPLES == 40
