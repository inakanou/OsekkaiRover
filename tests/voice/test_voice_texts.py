"""音声の入口の決まった言葉と LCD の文字（1.1、1.4、3.4、4.2、5.2、6.1、6.3、6.4）。"""

import pytest

from osekkai.core.lcd_text import to_lcd_codes
from osekkai.voice import texts

LCD_COLS = 16


def test_話す言葉は要件の文言と同じ() -> None:
    assert texts.SAY_CALL == "なあに？"
    assert texts.SAY_FOLLOW_UP == "うん、なあに？"
    assert texts.SAY_NOT_HEARD == "ごめんね、きこえなかった。もういちど ボタンを おして はなしてね"
    assert texts.SAY_UNKNOWN == "ごめんね、まだ わからないんだ"
    assert texts.SAY_SLEEP_TIME == "いまは ねる じかんだよ。また あした おはなし しようね"
    assert texts.SAY_PREPARING == "ちょっと まってね。じゅんび しているよ"
    assert texts.SAY_UNAVAILABLE == "いまは おはなし できないんだ"


def test_LCD_の文字は要件の文言と同じ() -> None:
    assert texts.LCD_LISTENING == "ｷｲﾃﾙﾖ"
    assert texts.LCD_THINKING == "ｶﾝｶﾞｴﾁｭｳ"
    assert texts.LCD_SLEEP_TIME == "ｵﾔｽﾐ ｼﾞｶﾝ"


@pytest.mark.parametrize("text", [texts.LCD_LISTENING, texts.LCD_THINKING, texts.LCD_SLEEP_TIME])
def test_LCD_の文字は_16_セルに収まり_ROM_にない文字を含まない(text: str) -> None:
    codes = to_lcd_codes(text)
    assert len(codes) <= LCD_COLS
    assert ord("?") not in codes


def test_既定のヒントはサンプルと同じ() -> None:
    assert (
        texts.DEFAULT_HINT
        == "ローバー。おはよう。おやすみ。できたよ。なんで？あそぼう。すき。たのしい。"
    )
