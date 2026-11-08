"""だるまさんの決まった言葉・LCD の文字・唱え言葉・首の角度・待つ長さ。

要件 1.1〜1.5、2.1、2.3、2.4、2.7、2.8、3.3、3.4、4.1〜4.3、4.6。
"""

import pytest

from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.types import Face, Glyph
from osekkai.features.daruma import texts

LCD_COLS = 16


# --- 始める語句 ---


def test_始める語句は_だるま_と_転んだ() -> None:
    assert texts.START_PHRASES == ("だるま", "転んだ")  # 1.1


# --- 決まった言葉 ---


def test_決まった言葉は要件の文言と同じ() -> None:
    assert texts.SAY_INTRO == (
        "だるまさんが ころんだ、しよう！ローバーが おにだよ。"
        "ローバーから はなれて、スタートの ところに いってね"
    )  # 1.2
    assert texts.SAY_GO == (
        "{rounds}かいの うちに ローバーに タッチ できたら {name}の かち。いくよー！"
    )  # 1.3
    assert texts.SAY_NO_CAMERA == "ごめんね、カメラが ないと あそべないんだ"  # 1.4
    before = "あ、つぎの よていの じかんが ちかいね。また あとで あそぼうね"
    assert before == texts.SAY_NEXT_TASK_BEFORE  # 1.5
    assert texts.SAY_KORONDA == "ころんだ！"  # 2.4
    assert texts.SAY_MOVED == "あっ、うごいた！スタートに もどってね"  # 2.7
    assert texts.SAY_WIN == "タッチ されちゃった！{name}の かち！やったね！"  # 3.3
    assert texts.SAY_MAX_ROUNDS == "おしまい！こんどは ローバーの かち。また あそぼうね"  # 4.1
    assert texts.SAY_NEXT_TASK == "あ、つぎの よていの じかんが ちかいね。また あそぼうね"  # 4.3
    assert (
        texts.SAY_CAMERA_LOST == "ごめんね、カメラが うまく うごかないみたい。また あそぼうね"
    )  # 4.6


def test_始める前と途中の次の予定の言葉は違う() -> None:
    # 1.5 は「また あとで」、4.3 は「また」
    assert "また あとで あそぼうね" in texts.SAY_NEXT_TASK_BEFORE
    assert "あとで" not in texts.SAY_NEXT_TASK


def test_だれもいないの言葉はない() -> None:
    # 止まるのが上手な子と、だれもいないのは見分けられないので、その決まりはやめた
    assert not hasattr(texts, "SAY_NOBODY")


# --- 回数（1.3、4.2） ---


@pytest.mark.parametrize("max_rounds", [1, 10, 50])
def test_始めの言葉に設定の回数が入る(max_rounds: int) -> None:
    said = texts.say_with_name(texts.say_go(max_rounds), "ゆう")
    assert said == f"{max_rounds}かいの うちに ローバーに タッチ できたら ゆうの かち。いくよー！"


def test_回数を入れても呼び名の場所は残る() -> None:
    assert texts.NAME_SLOT in texts.say_go(10)
    assert texts.ROUNDS_SLOT not in texts.say_go(10)


def test_残りの回数の言葉は最後の_3_回だけ() -> None:
    assert dict(texts.SAY_ROUNDS_LEFT) == {
        3: "あと さんかい",
        2: "あと にかい",
        1: "さいごの いっかい",
    }  # 4.2


def test_残りの回数の言葉は書き換えられない() -> None:
    with pytest.raises(TypeError):
        texts.SAY_ROUNDS_LEFT[4] = "x"  # type: ignore[index]


# --- 呼び名 ---


@pytest.mark.parametrize("template", [texts.SAY_GO, texts.SAY_WIN])
def test_呼び名を入れた言葉(template: str) -> None:
    said = texts.say_with_name(template, "ゆう")
    assert "ゆうの かち" in said
    assert "{name}" not in said


@pytest.mark.parametrize("call_name", ["", "  "])
def test_呼び名が空なら_きみ(call_name: str) -> None:
    assert texts.say_with_name(texts.say_go(10), call_name) == (
        "10かいの うちに ローバーに タッチ できたら きみの かち。いくよー！"
    )  # 3.4
    assert texts.say_with_name(texts.SAY_WIN, call_name) == (
        "タッチ されちゃった！きみの かち！やったね！"
    )


def test_呼び名の波かっこはそのまま入る() -> None:
    # str.format を使わないので、呼び名の中の波かっこで壊れない
    assert "{x}の かち" in texts.say_with_name(texts.SAY_WIN, "{x}")
    # 回数を先に入れるので、呼び名の中の回数の目印はそのまま
    assert "{rounds}の かち" in texts.say_with_name(texts.say_go(10), "{rounds}")


def test_呼び名のない言葉はそのまま() -> None:
    assert texts.say_with_name(texts.SAY_MOVED, "ゆう") == texts.SAY_MOVED


# --- 唱え言葉 ---


def test_唱え言葉は_3_つで話す言葉と_LCD_の文字の組() -> None:
    expected = (
        texts.Chant("だーるーまーさーんーがー", "ﾀﾞｰﾙｰﾏｰｻｰﾝｶﾞｰ"),
        texts.Chant("だるまさんが", "ﾀﾞﾙﾏｻﾝｶﾞ"),
        texts.Chant("だるまさんがー", "ﾀﾞﾙﾏｻﾝｶﾞｰ"),
    )
    assert expected == texts.CHANTS  # 2.1


def test_唱え言葉は書き換えられない() -> None:
    chant = texts.CHANTS[0]
    with pytest.raises(AttributeError):
        chant.speech = "x"  # type: ignore[misc]


# --- LCD の文字 ---


def test_LCD_の文字は要件の文言と同じ() -> None:
    heart = chr(Glyph.HEART)
    assert texts.LCD_INTRO == ("ﾀﾞﾙﾏｻﾝｶﾞ", "ｺﾛﾝﾀﾞ!")  # 1.2
    assert texts.LCD_WATCH == "ﾐﾃﾙﾖ!"  # 2.4
    assert texts.LCD_MOVED == "ｳｺﾞｲﾀ!"  # 2.7
    # 3.3: LCD の ROM に「♥」はないので、絵の文字のハートを使う
    win = f"{heart} ﾀｯﾁ! ｶﾁ! {heart}"
    assert win == texts.LCD_WIN


def test_LCD_の顔() -> None:
    assert texts.FACE_CHANT is Face.NORMAL  # 唱える間は普通の顔
    assert texts.FACE_WATCH is Face.CALL  # 2.4
    assert texts.FACE_MOVED is Face.CALL  # 2.7
    assert texts.FACE_WIN is Face.HAPPY  # 3.3


_LCD_TEXTS = [
    *texts.LCD_INTRO,
    texts.LCD_WATCH,
    texts.LCD_MOVED,
    texts.LCD_WIN,
    *(chant.lcd for chant in texts.CHANTS),
    *(face.text() for face in (texts.FACE_CHANT, texts.FACE_WATCH, texts.FACE_WIN)),
]


@pytest.mark.parametrize("text", _LCD_TEXTS)
def test_LCD_の文字は_16_セルに収まり_ROM_にない文字を含まない(text: str) -> None:
    codes = to_lcd_codes(text)
    assert 0 < len(codes) <= LCD_COLS
    assert ord("?") not in codes


def test_勝ちの_LCD_の端はハートの絵の文字() -> None:
    codes = to_lcd_codes(texts.LCD_WIN)
    assert codes[0] == Glyph.HEART
    assert codes[-1] == Glyph.HEART


# --- 首の角度と待つ長さ ---


def test_首の角度() -> None:
    assert texts.POSE_AWAY == (70.0, -30.0)  # 2.2: 横にそらす（pan、tilt）
    assert texts.POSE_FACING == (0.0, 10.0)  # 2.4: 正面


def test_待つ長さ() -> None:
    assert texts.INTRO_WAIT_S == 5.0  # 1.3
    assert texts.MOVED_WAIT_S == 5.0  # 2.8
    assert texts.PAUSE_MAX_S == 1.2  # 2.3
