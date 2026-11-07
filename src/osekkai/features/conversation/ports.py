"""会話の AI の口の型（8.5、10.1）。

流れ（flow）は、この型だけを通して会話の AI を使う。
実機では Claude のアダプタが、PC では台本のアダプタが、テストでは偽物がこの型を満たす。
送るのは決まりの文とやりとりだけ（8.5）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol


class FailureKind(Enum):
    """返事がもらえなかった種類。"""

    CONNECTION = "connection"  # つながらない
    BUSY = "busy"  # 混み合っている
    TIMEOUT = "timeout"  # 時間切れ
    OTHER = "other"  # そのほか


@dataclass(frozen=True)
class Turn:
    """やりとりの 1 つ分。"""

    role: Literal["user", "assistant"]
    text: str  # 呼び名は目印のまま（呼び名を記憶にも持たない）


@dataclass(frozen=True)
class Replied:
    """返事をもらえた。"""

    text: str


@dataclass(frozen=True)
class Refused:
    """会話の AI が答えるのを断った。"""


@dataclass(frozen=True)
class Failed:
    """返事をもらえなかった。"""

    kind: FailureKind


ChatResult = Replied | Refused | Failed


class ChatModelPort(Protocol):
    """会話の AI の口。"""

    def reply(self, system: str, turns: Sequence[Turn], timeout_s: float) -> ChatResult:
        """決まりの文とやりとりを送り、返事・拒否・失敗のどれかを返す。

        `turns` は user で始まり、user と assistant が交互で、user で終わる。
        誤りは例外にせず `Failed` で返す。返すまでの長さは `timeout_s` 程度。
        """
        ...
