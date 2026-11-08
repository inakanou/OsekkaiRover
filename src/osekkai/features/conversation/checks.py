"""おしゃべりの self-check の項目（8.1、9.4）。

- `conversation.setup`: 鍵のファイルが使えるか。使えなくてもだめにしない
  （版を戻しても直らない）。よいに理由を添える

設定で無効なとき・設定を使えないとき（ファイルがない・誤りがある）は飛ばす。
誤りは基盤の `core.config` が知らせる。会話のライブラリ（anthropic）がない
PC と CI でも飛ばす。開発 PC と CI の self-check はこれで赤くならない。

鍵の値は読んでも返さない。理由はすべて固定の文言にする。パス・設定の値を入れない。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from osekkai.core.selfcheck import Check, CheckContext, CheckResult, CheckStatus
from osekkai.features.conversation.adapters.claude import anthropic_available
from osekkai.features.conversation.config import ConversationConfig
from osekkai.features.conversation.envfile import SetupProblem, read_setup

__all__ = [
    "CONVERSATION_CHECKS",
    "PROBLEM_REASONS",
    "REASON_CONFIG_UNUSABLE",
    "REASON_DISABLED",
    "REASON_NOT_PRIVATE",
    "REASON_NOT_WRITTEN",
    "REASON_NO_FILE",
    "REASON_NO_LIBRARY",
    "REASON_UNREADABLE",
]

FEATURE_NAME: Final = "conversation"

# 固定の理由の文言
REASON_CONFIG_UNUSABLE: Final = "おしゃべりの設定を使えない"
REASON_DISABLED: Final = "無効"
REASON_NO_LIBRARY: Final = "会話のライブラリがない"
REASON_NO_FILE: Final = "鍵のファイルがない"
REASON_NOT_PRIVATE: Final = "鍵のファイルを本人だけが読めるようにしていない"
REASON_NOT_WRITTEN: Final = "鍵が書かれていない"
REASON_UNREADABLE: Final = "鍵のファイルを読めない"

PROBLEM_REASONS: Final[Mapping[SetupProblem, str]] = MappingProxyType(
    {
        SetupProblem.NO_FILE: REASON_NO_FILE,
        SetupProblem.NOT_PRIVATE: REASON_NOT_PRIVATE,
        SetupProblem.NOT_WRITTEN: REASON_NOT_WRITTEN,
        SetupProblem.UNREADABLE: REASON_UNREADABLE,
    }
)


def _check_setup(ctx: CheckContext) -> CheckResult:
    config = ctx.report.features.get(FEATURE_NAME)
    if not isinstance(config, ConversationConfig):
        return CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)
    if not config.enabled:
        return CheckResult(CheckStatus.SKIPPED, REASON_DISABLED)
    if not anthropic_available():
        return CheckResult(CheckStatus.SKIPPED, REASON_NO_LIBRARY)
    found = read_setup(ctx.paths)
    if isinstance(found, SetupProblem):
        return CheckResult(CheckStatus.OK, PROBLEM_REASONS[found])
    # 値は使わずに捨てる
    return CheckResult(CheckStatus.OK)


CONVERSATION_CHECKS: Final[tuple[Check, ...]] = (Check(f"{FEATURE_NAME}.setup", _check_setup),)
