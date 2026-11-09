"""ラジコンの話す言葉・LCD の文字と顔・端末の知らせの文。

要件 2.3、2.4、3.2、4.7、5.5、6.4、7.3、7.4、8.2、8.3、9.1〜9.3、10.2〜10.4、11.1〜11.3、12.2。
"""

from pathlib import Path

import pytest

from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.types import Face
from osekkai.features.remote import texts

LCD_COLS = 16
REQUIREMENTS = (
    Path(__file__).resolve().parents[3] / ".kiro" / "specs" / "remote-control" / "requirements.md"
)


def _requirements() -> str:
    return REQUIREMENTS.read_text(encoding="utf-8")


# --- 話す言葉 ---


def test_ブッブーの言葉は_4_つ() -> None:
    assert texts.SAY_HORNS == (
        "ブッブー！",
        "プップー！",
        "どいて どいてー！",
        "しゅっぱつ しんこう！",
    )  # 7.3


def test_話す言葉は要件の文言と同じ() -> None:
    assert texts.SAY_DANGER_OBSTACLE == "おっと、なにか あるよ"  # 5.5
    assert texts.SAY_DANGER_CLIFF == "あぶない！とまるよ"  # 5.5
    assert texts.SAY_PHOTO_CHEESE == "はい、チーズ！"  # 9.1
    assert texts.SAY_PHOTO_DONE == "とれたよ！"  # 9.2
    assert texts.SAY_PHOTO_FAILED == "ごめんね、うまく とれなかった"  # 9.3


# --- 端末の知らせ ---


def test_端末の知らせは要件の文言と同じ() -> None:
    assert texts.MSG_WRONG_PIN == "ばんごうが ちがうよ"  # 2.3
    assert texts.MSG_LOCKED == "しばらく まってね"  # 2.4
    assert texts.MSG_OTHER_OPERATOR == "いまは ほかの ひとが そうさ ちゅう"  # 3.2
    assert texts.MSG_OUT_OF_HOURS == "いまは ラジコンの じかんじゃないよ"  # 11.1
    assert texts.MSG_NO_CLOCK == "いまは つかえないよ"  # 11.3
    assert texts.MSG_BUSY == "ローバーは いま ほかの ことを しているよ。ちょっと まってね"  # 12.2
    assert texts.MSG_STREAM_FULL == "いまは みられないよ"  # 8.2
    assert texts.MSG_CAMERA_UNAVAILABLE == "カメラが つかえないよ"  # 8.3
    assert texts.MSG_TEXT_EMPTY == "もじを いれてね"  # 10.3
    assert texts.MSG_TALKING == "いま おはなし ちゅう。ちょっと まってね"  # 10.4
    assert texts.MSG_DANGER_OBSTACLE == "まえに なにか あるよ"  # 5.5
    assert texts.MSG_DANGER_CLIFF == "だんさが あるよ！"  # 5.5
    assert texts.MSG_BACK_GUIDE == "うしろは みえないよ。ゆっくり さがるよ"  # 6.4


def test_撮れなかった知らせは短くやさしい文() -> None:
    # 9.3 は端末の文言を決めていない。要件のかぎかっこの突き合わせ（_QUOTED）には入れない
    assert texts.MSG_PHOTO_FAILED == "しゃしんが とれなかったよ"


def test_おやすみの知らせは時間帯の外と同じ() -> None:
    assert texts.MSG_QUIET == texts.MSG_OUT_OF_HOURS  # 11.2


# --- LCD の文字と顔 ---


def test_LCD_の文字は要件の文言と同じ() -> None:
    assert texts.LCD_IDLE == "ﾗｼﾞｺﾝ"  # 4.7
    assert texts.LCD_FORWARD == "ﾌﾞｰﾝ!"  # 4.7
    assert texts.LCD_BACK == "ﾊﾞｯｸ"  # 4.7
    assert texts.LCD_STOP == "ｽﾄｯﾌﾟ!"  # 5.5
    assert texts.LCD_DANCE == "ﾀﾞﾝｽ!"  # 7.4
    assert texts.LCD_PHOTO == "ﾊｲ ﾁｰｽﾞ!"  # 9.1
    assert texts.LCD_TALK == "ｵﾊﾅｼ ﾁｭｳ"  # 10.2


def test_LCD_の顔() -> None:
    assert texts.FACE_DRIVE is Face.HAPPY  # 4.7: 笑顔
    assert texts.FACE_STOP is Face.CALL  # 5.5: 呼びかけの顔
    assert texts.FACE_DANCE is Face.HAPPY  # 7.4: 笑顔
    assert texts.FACE_TALK is Face.CALL  # 10.2: 呼びかけの顔


_LCD_TEXTS = [
    texts.LCD_IDLE,
    texts.LCD_FORWARD,
    texts.LCD_BACK,
    texts.LCD_STOP,
    texts.LCD_DANCE,
    texts.LCD_PHOTO,
    texts.LCD_TALK,
    *(face.text() for face in (texts.FACE_DRIVE, texts.FACE_STOP)),
]


@pytest.mark.parametrize("text", _LCD_TEXTS)
def test_LCD_の文字は_16_セルに収まり_ROM_にない文字を含まない(text: str) -> None:
    codes = to_lcd_codes(text)
    assert 0 < len(codes) <= LCD_COLS
    assert ord("?") not in codes


# --- 要件の文との突き合わせ ---

_QUOTED = [
    *texts.SAY_HORNS,
    texts.SAY_DANGER_OBSTACLE,
    texts.SAY_DANGER_CLIFF,
    texts.SAY_PHOTO_CHEESE,
    texts.SAY_PHOTO_DONE,
    texts.SAY_PHOTO_FAILED,
    texts.MSG_WRONG_PIN,
    texts.MSG_LOCKED,
    texts.MSG_OTHER_OPERATOR,
    texts.MSG_OUT_OF_HOURS,
    texts.MSG_QUIET,
    texts.MSG_NO_CLOCK,
    texts.MSG_BUSY,
    texts.MSG_STREAM_FULL,
    texts.MSG_CAMERA_UNAVAILABLE,
    texts.MSG_TEXT_EMPTY,
    texts.MSG_TALKING,
    texts.MSG_DANGER_OBSTACLE,
    texts.MSG_DANGER_CLIFF,
    texts.MSG_BACK_GUIDE,
    *_LCD_TEXTS[:7],
]


@pytest.mark.parametrize("text", _QUOTED)
def test_文は要件の中にかぎかっこで書かれている(text: str) -> None:
    assert f"「{text}」" in _requirements()
