"""おしゃべり（conversation）のテストで共通に使う偽物（10.1）。

`from conversation_fakes import ...` で読む（tests/features/conversation が import の道にある）。
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from osekkai.features.conversation.ports import ChatResult, Turn

MSG_SCRIPT_EMPTY = "偽の会話の AI の台本が尽きました"


@dataclass(frozen=True)
class ChatCall:
    """偽の会話の AI が受け取った 1 回分。"""

    system: str
    turns: tuple[Turn, ...]
    timeout_s: float


class FakeChatModel:
    """偽の会話の AI。台本の結果を順に返し、受け取ったものを覚える。

    `delay_s` を決めると、返す前にその長さだけ止まる（締め切りのテスト用）。
    `release()` で止まっているのを早めに放す。`called` は呼ばれると立つ。
    """

    def __init__(self, script: Iterable[ChatResult], *, delay_s: float = 0.0) -> None:
        self._script = list(script)
        self.delay_s = delay_s
        self.calls: list[ChatCall] = []
        self.called = threading.Event()
        self._released = threading.Event()
        self._lock = threading.Lock()

    def reply(self, system: str, turns: Sequence[Turn], timeout_s: float) -> ChatResult:
        with self._lock:
            self.calls.append(ChatCall(system, tuple(turns), timeout_s))
            if not self._script:
                raise AssertionError(MSG_SCRIPT_EMPTY)
            result = self._script.pop(0)
        self.called.set()
        if self.delay_s > 0:
            self._released.wait(self.delay_s)
        return result

    def release(self) -> None:
        """止まっている呼び出し（とこの後の呼び出し）をすぐ返させる。"""
        self._released.set()
