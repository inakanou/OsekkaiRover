"""PC 用の台本の会話の AI（10.2）。

鍵がない PC で、決まった返事を順に返し、終わったら初めに戻る。外へは何も送らない。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from osekkai.features.conversation.ports import ChatResult, Replied, Turn

# 呼び名の目印（privacy.NAME_MARK と同じ文字。アダプタは privacy を読めない）
_MARK: Final = "{name}"

# 1〜2 文の短いやさしい言葉。記号と絵文字は使わない（3.1）
SCRIPT: Final[tuple[str, ...]] = (
    f"そうなんだ。{_MARK}は すごいね",
    "おしえてくれて ありがとう。もっと きかせてね",
    f"{_MARK}は どう おもうかな",
    "ぼくは ロボットだよ。いっしょに おはなし できて うれしいな",
)


class ScriptedChatModel:
    """決まった返事を順に返す偽の会話の AI。"""

    def __init__(self) -> None:
        self._next = 0

    def reply(self, system: str, turns: Sequence[Turn], timeout_s: float) -> ChatResult:
        """やりとりの中身によらず、台本の次の返事を返す。"""
        text = SCRIPT[self._next]
        self._next = (self._next + 1) % len(SCRIPT)
        return Replied(text)
