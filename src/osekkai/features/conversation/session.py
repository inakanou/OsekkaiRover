"""会話のやりとり（Session）。要件 2.1、2.2、2.4、2.5、6.6。

やりとりは記憶の中だけに置き、ファイルにも外部にも出さない（2.5）。
捨てるときは参照を外すだけ。呼び名は目印のまま持つ。
"""

from __future__ import annotations

from collections import deque
from typing import Final

from osekkai.features.conversation.ports import Turn

# 最後に使ってから、この秒数たったら会話を忘れる（2.2）
FORGET_AFTER_S: Final = 130.0


class Session:
    """1 つの会話のやりとり。時刻は単調な秒（time.monotonic など）で受け取る。"""

    def __init__(self, max_turns: int, now: float) -> None:
        # 上限を超えたら古い往復から捨てる（2.4）
        self._pairs: deque[tuple[str, str]] = deque(maxlen=max_turns)
        self._last_used = now

    def turns_with(self, user_text: str) -> list[Turn]:
        """今までの往復に新しい問いを足した列。user で始まり交互で user で終わる。"""
        turns: list[Turn] = []
        for asked, replied in self._pairs:
            turns.append(Turn("user", asked))
            turns.append(Turn("assistant", replied))
        turns.append(Turn("user", user_text))
        return turns

    def add(self, user_text: str, reply_text: str, now: float) -> None:
        """返事を話せた 1 往復を足す（拒否・空・失敗の問いは足さない。6.6）。"""
        self._pairs.append((user_text, reply_text))
        self._last_used = now

    def touch(self, now: float) -> None:
        """往復は足さずに、最後に使った時刻だけを進める（拒否・空・失敗を話し終えたとき）。"""
        self._last_used = now

    def expired(self, now: float) -> bool:
        """最後に使ってから FORGET_AFTER_S たったか。"""
        return now - self._last_used >= FORGET_AFTER_S
