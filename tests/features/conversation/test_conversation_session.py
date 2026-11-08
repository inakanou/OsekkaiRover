"""会話のやりとり（2.1、2.2、2.4、2.5、6.6）。"""

from osekkai.features.conversation.ports import Turn
from osekkai.features.conversation.session import FORGET_AFTER_S, Session


def _pairs(session: Session, count: int) -> None:
    for i in range(count):
        session.add(f"問い{i}", f"返事{i}", now=float(i))


# --- 列の形 ---


def test_はじめは新しい問いだけ() -> None:
    session = Session(max_turns=10, now=0.0)
    assert session.turns_with("こんにちは") == [Turn("user", "こんにちは")]


def test_今までの往復に新しい問いを足す() -> None:
    session = Session(max_turns=10, now=0.0)
    session.add("なまえは", "ロボだよ", now=1.0)
    session.add("すきなものは", "でんきだよ", now=2.0)

    assert session.turns_with("ほんと") == [
        Turn("user", "なまえは"),
        Turn("assistant", "ロボだよ"),
        Turn("user", "すきなものは"),
        Turn("assistant", "でんきだよ"),
        Turn("user", "ほんと"),
    ]


def test_user_で始まり交互で_user_で終わる() -> None:
    session = Session(max_turns=10, now=0.0)
    _pairs(session, 4)

    turns = session.turns_with("つぎ")

    assert len(turns) == 9
    assert [t.role for t in turns] == ["user", "assistant"] * 4 + ["user"]


def test_turns_with_は問いを覚えない() -> None:
    # 失敗した問いは残さない（6.6）。足すのは add だけ
    session = Session(max_turns=10, now=0.0)
    session.turns_with("きえるといい")

    assert session.turns_with("つぎ") == [Turn("user", "つぎ")]


def test_目印のまま持つ() -> None:
    session = Session(max_turns=10, now=0.0)
    session.add("{name}だよ", "{name}、こんにちは", now=1.0)

    assert session.turns_with("うん")[:2] == [
        Turn("user", "{name}だよ"),
        Turn("assistant", "{name}、こんにちは"),
    ]


# --- 往復の上限（2.4） ---


def test_10_往復までは全部覚える() -> None:
    session = Session(max_turns=10, now=0.0)
    _pairs(session, 10)

    turns = session.turns_with("つぎ")

    assert len(turns) == 21
    assert turns[0] == Turn("user", "問い0")


def test_11_往復目で最初の往復が消える() -> None:
    session = Session(max_turns=10, now=0.0)
    _pairs(session, 11)

    turns = session.turns_with("つぎ")

    assert len(turns) == 21
    assert turns[0] == Turn("user", "問い1")
    assert turns[1] == Turn("assistant", "返事1")
    assert turns[-2] == Turn("assistant", "返事10")
    assert Turn("user", "問い0") not in turns
    assert Turn("assistant", "返事0") not in turns


def test_上限の数は設定に従う() -> None:
    session = Session(max_turns=2, now=0.0)
    _pairs(session, 5)

    assert session.turns_with("つぎ") == [
        Turn("user", "問い3"),
        Turn("assistant", "返事3"),
        Turn("user", "問い4"),
        Turn("assistant", "返事4"),
        Turn("user", "つぎ"),
    ]


# --- 切れる時刻（2.2） ---


def test_忘れるまでは_130_秒() -> None:
    assert FORGET_AFTER_S == 130


def test_作ってから_130_秒で切れる() -> None:
    session = Session(max_turns=10, now=100.0)

    assert not session.expired(now=100.0)
    assert not session.expired(now=229.9)
    assert session.expired(now=230.0)
    assert session.expired(now=500.0)


def test_最後に足してから数える() -> None:
    session = Session(max_turns=10, now=0.0)
    session.add("なに", "ロボだよ", now=100.0)

    assert not session.expired(now=229.9)
    assert session.expired(now=230.0)


def test_足さずに話し終えた時刻でも数え直す() -> None:
    # 拒否や失敗を話し終えた後も、続けて聞く間は会話を覚えている（2.1、6.6）
    session = Session(max_turns=10, now=0.0)
    session.add("なに", "ロボだよ", now=0.0)
    session.touch(now=125.0)

    assert not session.expired(now=250.0)
    assert session.expired(now=255.0)
    assert len(session.turns_with("それで")) == 3


def test_時刻を進めても往復は増えない() -> None:
    session = Session(max_turns=10, now=0.0)
    session.touch(now=10.0)

    assert session.turns_with("なに") == [Turn("user", "なに")]
