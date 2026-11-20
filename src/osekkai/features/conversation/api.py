"""おしゃべりの公開の口。ほかの機能は、おしゃべりの中をここからだけ読む。

要件 6.4、6.6、9.5、10.4（morning-night-talk）。

会話の AI の口と結果の型、やりとりの型、返事の整え、子ども向けの決まりの文、
鍵の問題の種類を、中のモジュールから選んで出す。新しい振る舞いは足さない。
鍵の値はこの口から外へ出さない。`claude_chat_model` の中でアダプタに渡すだけ（ログにも出さない）。
"""

from __future__ import annotations

from osekkai.core.paths import Paths
from osekkai.features.conversation.adapters import claude as _claude
from osekkai.features.conversation.envfile import SetupProblem, read_setup
from osekkai.features.conversation.ports import (
    ChatModelPort,
    ChatResult,
    Failed,
    FailureKind,
    Refused,
    Replied,
    Turn,
)
from osekkai.features.conversation.privacy import shape_reply
from osekkai.features.conversation.texts import KID_RULES

__all__ = [
    "KID_RULES",
    "ChatModelPort",
    "ChatResult",
    "Failed",
    "FailureKind",
    "Refused",
    "Replied",
    "SetupProblem",
    "Turn",
    "claude_chat_model",
    "library_available",
    "setup_problem",
    "shape_reply",
]


def setup_problem(paths: Paths) -> SetupProblem | None:
    """鍵が使えるかを確かめる。使えなければ問題の種類、使えれば None。値は返さない。"""
    found = read_setup(paths)
    return found if isinstance(found, SetupProblem) else None


def library_available() -> bool:
    """会話のライブラリ（anthropic）が入っているかを返す。読み込みはしない。"""
    return _claude.anthropic_available()


def claude_chat_model(paths: Paths) -> ChatModelPort | None:
    """鍵を読め、ライブラリがあれば Claude のアダプタを作る。どちらかがなければ None。

    鍵の置き場所はおしゃべりと同じ（9.5）。ここではログに何も出さない。
    """
    found = read_setup(paths)
    if isinstance(found, SetupProblem) or not library_available():
        return None
    return _claude.ClaudeChatModel(found)
