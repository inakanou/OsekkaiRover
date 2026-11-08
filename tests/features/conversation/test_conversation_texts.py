"""おしゃべりの決まりの文・決まった言葉・LCD の文字。

要件 1.1〜1.3、1.5、3.1、3.2、4.4、5.1〜5.3、6.1〜6.3、9.2。
"""

import pytest

from osekkai.core.lcd_text import to_lcd_codes
from osekkai.features.conversation import texts

LCD_COLS = 16


# --- 子ども向けの決まりの文 ---


@pytest.mark.parametrize(
    "rule",
    [
        # 1.3: 1〜2 文、やさしい言葉、絵文字・記号・箇条書きなし
        "1〜2 文",
        "4 さい",
        "絵文字",
        "記号",
        "箇条書き",
        # 3.1: 性格
        "明るく",
        "やさしく",
        "おせっかい",
        "ほめ",
        # 3.1: 分からないこと
        "分からない",
        "うそ",
        # 3.1: 個人のこと
        "住所",
        "幼稚園",
        "電話番号",
        "パスワード",
        "覚えない",
        # 3.1: 怖い話
        "怖い話",
        "暴力",
        "大人向け",
        "別の話",
        # 3.1: けが・病気・危ないこと
        "けが",
        "病気",
        "火",
        "薬",
        "電気",
        "高い所",
        "ママか パパに すぐ いってね",
        # 3.1: 悲しい・痛い・いやなこと
        "悲しい",
        "痛い",
        "いやなこと",
        "気持ち",
        "家の大人",
        # 3.1: 秘密
        "秘密",
        # 3.1: ロボット
        "ロボット",
        "人間のふり",
        # 3.1: いい習慣
        "歯みがき",
        "お片づけ",
        "早寝",
    ],
)
def test_決まりの文に要件の決まりが入る(rule: str) -> None:
    assert rule in texts.SYSTEM_PROMPT


def test_決まりの文は呼び名の目印で呼ぶよう求める() -> None:
    # 4.4: 子どもを呼ぶときは目印を書く。呼び名そのものは入れない
    assert "{name}" in texts.SYSTEM_PROMPT
    assert "きみ" not in texts.SYSTEM_PROMPT


def test_決まりの文は秘密にしようと言わないよう求める() -> None:
    assert "家の人に秘密にしようとは決して言わない" in texts.SYSTEM_PROMPT


# --- 決まった言葉 ---


def test_決まった言葉は要件の文言と同じ() -> None:
    assert texts.SAY_EMPTY == "うーん、なんて いおうかな"  # 1.5
    assert texts.SAY_REFUSED == "そのはなしは ちょっと むずかしいな。べつの おはなし しよう"  # 3.2
    assert texts.SAY_SLEEP_TIME == "いまは ねる じかんだよ。また あした おはなし しようね"  # 5.1
    assert texts.SAY_UNAVAILABLE == "いまは おはなし できないんだ。また あとで ね"  # 5.2、9.2
    assert texts.SAY_LIMIT == "きょうは いっぱい おはなし したね。また あした ね"  # 5.3
    assert texts.SAY_CONNECTION == "ごめんね、いま インターネットに つながらないみたい"  # 6.1
    assert texts.SAY_BUSY == "ちょっと つかれちゃった。すこし まってから また はなそうね"  # 6.2
    assert texts.SAY_OTHER == "ごめんね、いま うまく かんがえられないみたい"  # 6.3、6.4


def test_門の理由から言葉を引ける() -> None:
    # 理由は gate の GateReason の値（"not_set_up" など）で引く
    assert dict(texts.GATE_REPLIES) == {
        "not_set_up": texts.SAY_UNAVAILABLE,  # 9.2
        "clock_unsynced": texts.SAY_UNAVAILABLE,  # 5.2
        "out_of_hours": texts.SAY_SLEEP_TIME,  # 5.1
        "limit": texts.SAY_LIMIT,  # 5.3、5.6
    }


def test_門の表は書き換えられない() -> None:
    with pytest.raises(TypeError):
        texts.GATE_REPLIES["limit"] = "x"  # type: ignore[index]


# --- LCD の文字 ---


def test_LCD_の文字は要件の文言と同じ() -> None:
    assert texts.LCD_THINKING == "ｶﾝｶﾞｴﾁｭｳ"  # 1.1
    assert texts.LCD_ROBOT == "ﾛｰﾊﾞｰ"  # 1.2


@pytest.mark.parametrize("text", [texts.LCD_THINKING, texts.LCD_ROBOT])
def test_LCD_の文字は_16_セルに収まり_ROM_にない文字を含まない(text: str) -> None:
    codes = to_lcd_codes(text)
    assert len(codes) <= LCD_COLS
    assert ord("?") not in codes
