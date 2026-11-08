"""PC 用の台本の会話の AI（ScriptedChatModel、10.2）。"""

import ast
from pathlib import Path

from osekkai.features.conversation import texts
from osekkai.features.conversation.adapters import scripted
from osekkai.features.conversation.adapters.scripted import SCRIPT, ScriptedChatModel
from osekkai.features.conversation.ports import ChatModelPort, Replied, Turn
from osekkai.features.conversation.privacy import NAME_MARK, shape_reply

ASK = (Turn("user", "なあに"),)


def _texts(model: ScriptedChatModel, count: int) -> list[str]:
    results = [model.reply(texts.SYSTEM_PROMPT, ASK, 1.0) for _ in range(count)]
    assert all(isinstance(r, Replied) for r in results)
    return [r.text for r in results if isinstance(r, Replied)]


def test_口の型を満たす() -> None:
    # mypy が型を確かめる
    model: ChatModelPort = ScriptedChatModel()
    assert isinstance(model.reply(texts.SYSTEM_PROMPT, ASK, 1.0), Replied)


def test_台本は2つ以上ある() -> None:
    assert len(SCRIPT) >= 2


def test_台本の順に返す() -> None:
    assert _texts(ScriptedChatModel(), len(SCRIPT)) == list(SCRIPT)


def test_台本の終わりの次は初めに戻る() -> None:
    got = _texts(ScriptedChatModel(), len(SCRIPT) * 2 + 1)
    assert got == [*SCRIPT, *SCRIPT, SCRIPT[0]]


def test_やりとりの中身によらず順に返す() -> None:
    model = ScriptedChatModel()
    first = model.reply("", (Turn("user", "ちがう"),), 0.1)
    second = model.reply(texts.SYSTEM_PROMPT, (*ASK, Turn("assistant", "うん"), *ASK), 5.0)
    assert first == Replied(SCRIPT[0])
    assert second == Replied(SCRIPT[1])


def test_目印は呼び名の目印と同じ文字() -> None:
    assert scripted._MARK == NAME_MARK


def test_目印を含む返事がある() -> None:
    assert any(NAME_MARK in line for line in SCRIPT)


def test_返事は整えても変わらない短い文() -> None:
    # 1〜2 文・記号や絵文字なし（3.1）なので、整えても同じ
    for line in SCRIPT:
        assert line.strip()
        assert shape_reply(line) == line


def test_外へ送るライブラリを読み込まない() -> None:
    tree = ast.parse(Path(scripted.__file__).read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    allowed = ("__future__", "collections", "typing", "osekkai.features.conversation.ports")
    assert all(n.startswith(allowed) for n in names), names
