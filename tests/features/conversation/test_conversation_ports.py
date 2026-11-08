"""会話の AI の口の型（ports）と、テスト用の偽の会話の AI（8.5、10.1）。"""

import threading
import time

import pytest
from conversation_fakes import MSG_SCRIPT_EMPTY, FakeChatModel

from osekkai.features.conversation.ports import (
    ChatModelPort,
    ChatResult,
    Failed,
    FailureKind,
    Refused,
    Replied,
    Turn,
)

ASK = Turn("user", "なあに")
ANSWER = Turn("assistant", "そらだよ")
AGAIN = Turn("user", "なんで")


# --- 型 ---


def test_失敗の種類は4つ() -> None:
    assert [k.name for k in FailureKind] == ["CONNECTION", "BUSY", "TIMEOUT", "OTHER"]
    assert [k.value for k in FailureKind] == ["connection", "busy", "timeout", "other"]


def test_結果は値で比べられて変えられない() -> None:
    assert Replied("はい") == Replied("はい")
    assert Refused() == Refused()
    assert Failed(FailureKind.BUSY) == Failed(FailureKind.BUSY)
    assert Failed(FailureKind.BUSY) != Failed(FailureKind.OTHER)
    with pytest.raises(AttributeError):
        Replied("はい").text = "いいえ"  # type: ignore[misc]


def test_やりとりの1つ分は役と文を持つ() -> None:
    assert ASK.role == "user"
    assert ASK.text == "なあに"
    assert Turn("user", "なあに") == ASK
    with pytest.raises(AttributeError):
        ASK.text = "べつ"  # type: ignore[misc]


# --- 偽の会話の AI ---


def test_偽物は口の型を満たす() -> None:
    model: ChatModelPort = FakeChatModel([Replied("はい")])
    result: ChatResult = model.reply("きまり", [ASK], 20.0)
    assert result == Replied("はい")


def test_偽物は台本の順に返す() -> None:
    model = FakeChatModel([Replied("はい"), Refused(), Failed(FailureKind.TIMEOUT)])
    got = [model.reply("きまり", [ASK], 20.0) for _ in range(3)]
    assert got == [Replied("はい"), Refused(), Failed(FailureKind.TIMEOUT)]


def test_台本が尽きたら決まった理由で止まる() -> None:
    model = FakeChatModel([Refused()])
    model.reply("きまり", [ASK], 20.0)
    with pytest.raises(AssertionError, match=MSG_SCRIPT_EMPTY):
        model.reply("きまり", [ASK], 20.0)


def test_偽物は受け取った決まりの文とやりとりと締め切りを覚える() -> None:
    model = FakeChatModel([Replied("はい"), Replied("うん")])
    first = [ASK]
    model.reply("きまり1", first, 20.0)
    model.reply("きまり2", (ASK, ANSWER, AGAIN), 5.0)
    first.append(ANSWER)  # 渡した後に呼び手が変えても、覚えたものは変わらない
    assert [c.system for c in model.calls] == ["きまり1", "きまり2"]
    assert [c.turns for c in model.calls] == [(ASK,), (ASK, ANSWER, AGAIN)]
    assert [c.timeout_s for c in model.calls] == [20.0, 5.0]


def test_偽物は決めた長さだけ止まる() -> None:
    model = FakeChatModel([Replied("はい")], delay_s=0.2)
    start = time.monotonic()
    assert model.reply("きまり", [ASK], 20.0) == Replied("はい")
    assert time.monotonic() - start >= 0.2


def test_止まっている間も呼ばれたことが分かり_放すと返す() -> None:
    model = FakeChatModel([Replied("はい")], delay_s=30.0)
    got: list[ChatResult] = []
    worker = threading.Thread(target=lambda: got.append(model.reply("きまり", [ASK], 20.0)))
    worker.start()
    assert model.called.wait(2.0)
    assert len(model.calls) == 1
    assert got == []
    model.release()
    worker.join(2.0)
    assert not worker.is_alive()
    assert got == [Replied("はい")]
