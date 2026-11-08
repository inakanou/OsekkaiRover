"""呼び名の置き換えと返事の整え（1.4、3.3、4.1〜4.3）。"""

import pytest

from osekkai.features.conversation import texts
from osekkai.features.conversation.privacy import (
    DEFAULT_CALL_NAME,
    MAX_CHARS,
    NAME_MARK,
    NameGuard,
    shape_reply,
)

# --- 目印 ---


def test_目印と既定の呼び名() -> None:
    assert NAME_MARK == "{name}"
    assert DEFAULT_CALL_NAME == "きみ"


def test_決まりの文に目印が入っている() -> None:
    # 4.4: 子どもを呼ぶときは目印を書くよう求める
    assert NAME_MARK in texts.SYSTEM_PROMPT


# --- mask（4.1、4.2） ---


def test_ひらがなの呼び名を目印にする() -> None:
    guard = NameGuard("たろう", [])
    assert guard.mask("たろうだよ") == "{name}だよ"


def test_カタカナで入っても目印にする() -> None:
    guard = NameGuard("たろう", [])
    assert guard.mask("ぼく タロウ") == "ぼく {name}"


def test_呼び名がカタカナでもひらがなを目印にする() -> None:
    guard = NameGuard("タロウ", [])
    assert guard.mask("たろうと タロウ") == "{name}と {name}"


def test_漢字の別の形を目印にする() -> None:
    guard = NameGuard("たろう", ["太郎"])
    assert guard.mask("太郎は 5 さい") == "{name}は 5 さい"


def test_かなの混じった別の形もかなの違いを同じとみなす() -> None:
    guard = NameGuard("たろう", ["太ろう"])
    assert guard.mask("太ロウ くん") == "{name} くん"


def test_置き換えは元の文字列に行う() -> None:
    # 呼び名でないカタカナは、ひらがなに変わらない
    guard = NameGuard("たろう", [])
    assert guard.mask("ロボットの タロウ") == "ロボットの {name}"


def test_長い形から先に置き換える() -> None:
    guard = NameGuard("たろ", ["たろうくん"])
    assert guard.mask("たろうくん と たろ") == "{name} と {name}"


def test_何回でてもすべて置き換える() -> None:
    guard = NameGuard("たろう", ["太郎"])
    assert guard.mask("たろう 太郎 タロウ") == "{name} {name} {name}"


def test_呼び名がないときはそのまま() -> None:
    guard = NameGuard("たろう", ["太郎"])
    assert guard.mask("きょうは はれ") == "きょうは はれ"


@pytest.mark.parametrize("call_name", ["", "  ", "　"])
def test_呼び名が空なら置き換えない(call_name: str) -> None:
    guard = NameGuard(call_name, ["太郎"])
    assert guard.mask("きみと 太郎") == "きみと 太郎"


def test_呼び名がきみなら置き換えない() -> None:
    guard = NameGuard("きみ", [])
    assert guard.mask("きみは だれ") == "きみは だれ"


def test_空や空白だけの別の形は飛ばす() -> None:
    guard = NameGuard("たろう", ["", " ", "　"])
    assert guard.mask("たろう と はなこ") == "{name} と はなこ"


def test_別の形の前後の空白は除いて照合する() -> None:
    guard = NameGuard("たろう", [" 太郎 "])
    assert guard.mask("太郎だよ") == "{name}だよ"


def test_記号の入った呼び名でも文字どおりに照合する() -> None:
    guard = NameGuard("た.ろ", [])
    assert guard.mask("たろ た.ろ") == "たろ {name}"


# --- fill（4.1、4.3） ---


def test_目印を呼び名に戻す() -> None:
    guard = NameGuard("たろう", ["太郎"])
    assert guard.fill("{name}、すごいね") == "たろう、すごいね"


def test_全角の目印も呼び名に戻す() -> None:
    guard = NameGuard("たろう", [])
    assert guard.fill("｛name｝と {name}") == "たろうと たろう"


@pytest.mark.parametrize("call_name", ["", "  "])
def test_呼び名が空なら目印はきみになる(call_name: str) -> None:
    guard = NameGuard(call_name, [])
    assert guard.fill("{name}、えらいね") == "きみ、えらいね"


def test_目印のない返事はそのまま() -> None:
    guard = NameGuard("たろう", [])
    assert guard.fill("すごいね") == "すごいね"


def test_置き換えて戻すと元の呼び名になる() -> None:
    guard = NameGuard("たろう", [])
    assert guard.fill(guard.mask("たろうだよ")) == "たろうだよ"


# --- shape_reply（1.4、3.3） ---


def test_短い返事はそのまま() -> None:
    assert shape_reply("こんにちは！ げんき？") == "こんにちは！ げんき？"


def test_3文の返事は2文になる() -> None:
    reply = "いいね。すごいね！ また あそぼう？"
    assert shape_reply(reply) == "いいね。すごいね！"


def test_半角の区切りでも2文にする() -> None:
    assert shape_reply("いいね! すごいね? またね.") == "いいね! すごいね?"


def test_続く区切りは同じ文にまとめる() -> None:
    assert shape_reply("すごい！！ ほんと？！ やったね。") == "すごい！！ ほんと？！"


def test_区切りのない終わりも1文とみなす() -> None:
    assert shape_reply("いいね。そうだね") == "いいね。そうだね"


def test_130文字の返事は120文字になる() -> None:
    reply = "あ" * 130
    shaped = shape_reply(reply)
    assert shaped == "あ" * MAX_CHARS
    assert MAX_CHARS == 120


def test_2文にした後も120文字を超えれば切る() -> None:
    reply = "あ" * 70 + "。" + "い" * 70 + "。" + "う。"
    assert shape_reply(reply) == ("あ" * 70 + "。" + "い" * 70)[:120]


def test_目印の途中では切らない() -> None:
    reply = "あ" * 117 + "{name}だよ"
    assert shape_reply(reply) == "あ" * 117


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("すごいね😀", "すごいね"),
        ("❤️ だいすき", "だいすき"),
        ("👍🏽 いいね", "いいね"),
        ("おはよう☀️！", "おはよう！"),
        ("♪ うたおう ★", "うたおう"),
        ("👨‍👩‍👧 かぞく", "かぞく"),
    ],
)
def test_絵文字を除く(reply: str, expected: str) -> None:
    assert shape_reply(reply) == expected


def test_箇条書きの頭を除く() -> None:
    reply = "- りんご\n- みかん\n• ぶどう"
    assert shape_reply(reply) == "りんご みかん ぶどう"


def test_字下げした箇条書きの頭も除く() -> None:
    assert shape_reply("  - りんご。\n  - みかん。") == "りんご。 みかん。"


def test_記号を除く() -> None:
    assert shape_reply("**すごい**ね。# がんばろう") == "すごいね。 がんばろう"


def test_行の途中のハイフンは残す() -> None:
    assert shape_reply("ハイ-タッチ！") == "ハイ-タッチ！"


def test_目印は残す() -> None:
    assert shape_reply("{name}、すごいね。") == "{name}、すごいね。"


@pytest.mark.parametrize("reply", ["", "   ", "\n　\n", "😀", "- *", "#  •"])
def test_空白だけなら空にする(reply: str) -> None:
    assert shape_reply(reply) == ""
