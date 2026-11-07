"""ほかの機能が使う、言葉の登録の口（2.1、2.6、3.1、3.2、4.1）。

機能はこのモジュールだけを読めばよい。軽く読めるように、登録表と標準ライブラリだけを使う。

使い方（機能の作り手へ）:

- `run(stop)` の始めに登録し、`alive=lambda: not stop.is_set()` を渡す。
  `finally` で `remove()` する。例外で止まっても外れ、止める旗が立っても外れる（2.6）
- 言葉が来たときに呼ぶ口（handler）は、すぐに戻る。重い仕事は自分のスレッドに渡す
- `alive` は登録表のロックの中で呼ぶ。軽い確かめだけにし、登録の口を呼ばない
- 漢字はかなにしない。聞き取りは漢字で書くことがあるので、語句はかなと、
  出そうな漢字の形の両方を登録する（例: 「だるまさんがころんだ」「だるまさんが転んだ」）
- 言葉を受け取った後に、続けて聞いてほしいときは `listen_again(owner)` を呼ぶ（4.1）。
  返事を話し終えた後に呼ぶ
- 言葉を受け取ってから自分の振る舞いが終わるまでは、ボタンも借りておく。
  借りていないと、押下は入口に届き、入口（優先度 55）が資源を取り上げて聞き取りを始める

例::

    def run(self, stop: threading.Event) -> None:
        registration = register_words(
            "daruma", 0, ("だるまさんがころんだ", "だるまさんが転んだ"),
            self._on_heard, alive=lambda: not stop.is_set(),
        )
        try:
            ...
        finally:
            registration.remove()
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from osekkai.voice.intents import (
    FallbackRegistration,
    FallbackTaken,
    Handler,
    Heard,
    IntentRegistry,
    WordsRegistration,
)

__all__ = [
    "FallbackTaken",
    "Handler",
    "Heard",
    "Registration",
    "listen_again",
    "register_fallback",
    "register_words",
]

# プロセスの中で 1 つの登録表
_REGISTRY = IntentRegistry()


class Registration(Protocol):
    """登録。`remove()` で外す。"""

    def remove(self) -> None:
        """登録を外す。もう外れていれば何もしない。"""
        ...


class _Handle:
    """登録した表を覚えておき、その表から外す。"""

    def __init__(
        self, table: IntentRegistry, registration: WordsRegistration | FallbackRegistration
    ) -> None:
        self._table = table
        self._registration = registration

    def remove(self) -> None:
        self._table.remove(self._registration)


def register_words(
    owner: str,
    priority: int,
    phrases: Sequence[str],
    handler: Handler,
    *,
    alive: Callable[[], bool],
) -> Registration:
    """受け付ける語句を登録する（2.1）。

    優先度の高いものから先に判定する。語句なし・空の語句は ValueError。
    """
    table = _REGISTRY
    return _Handle(table, table.add_words(owner, priority, phrases, handler, alive=alive))


def register_fallback(owner: str, handler: Handler, *, alive: Callable[[], bool]) -> Registration:
    """当てはまらない言葉の受け手を登録する（3.1）。

    受け手は 1 つだけ。生きている受け手があれば FallbackTaken（3.2）。
    """
    table = _REGISTRY
    return _Handle(table, table.set_fallback(owner, handler, alive=alive))


def listen_again(owner: str) -> None:
    """続けて聞くを求める（4.1）。owner の生きている登録がなければ何もしない。"""
    _REGISTRY.request_follow_up(owner)


def registry() -> IntentRegistry:
    """プロセスの中の登録表を返す。

    音声の入口（voice）だけが使う。ほかの機能は上の登録の口を使う。
    """
    return _REGISTRY


def _reset_for_tests() -> None:
    """登録表を空にする（テストだけで使う）。"""
    global _REGISTRY
    _REGISTRY = IntentRegistry()
