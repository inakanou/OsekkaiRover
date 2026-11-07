"""Claude API の会話の AI のアダプタ（3.1、3.2、6.1〜6.4、8.1、8.5）。

anthropic は関数の中で遅れて読み込む（開発 PC と CI にはない）。
送るのは決まりの文とやりとりだけ（8.5）。鍵は環境変数やプロファイルに頼らず、渡された値を使う。
ログには拒否の種類と例外の型の名前だけを出す。返事・問い・鍵は出さない（8.1）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from importlib.util import find_spec
from typing import Any, Final

from osekkai.features.conversation.ports import (
    ChatResult,
    Failed,
    FailureKind,
    Refused,
    Replied,
    Turn,
)

logger = logging.getLogger(__name__)

ANTHROPIC_MODULE: Final = "anthropic"
MODEL: Final = "claude-opus-5-5"
MAX_TOKENS: Final = 2048  # 思考の分を含む。返事は短いが、切れないように余裕を持つ
EFFORT: Final = "low"
FALLBACK_BETA: Final = "server-side-fallback-2026-07-01"
FALLBACKS: Final = "default"
REFUSAL: Final = "refusal"


def anthropic_available() -> bool:
    """会話のライブラリ（anthropic）を読み込めるかを返す。読み込みはしない。"""
    try:
        return find_spec(ANTHROPIC_MODULE) is not None
    except ValueError:
        # すでに読み込まれていて、出どころの情報がないとき
        return True
    except ImportError:
        # 読み込めないと印が付いているとき
        return False


class ClaudeChatModel:
    """Claude API で返事を作る。誤りは例外にせず Failed で返す。"""

    def __init__(self, credential: str) -> None:
        self._credential = credential
        self._client: Any = None
        self._client_timeout: float | None = None

    def reply(self, system: str, turns: Sequence[Turn], timeout_s: float) -> ChatResult:
        try:
            import anthropic
        except Exception as exc:
            logger.warning("会話のライブラリを読み込めません: %s", type(exc).__name__)
            return Failed(FailureKind.OTHER)
        try:
            client = self._client_for(anthropic, timeout_s)
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": t.role, "content": t.text} for t in turns],
                output_config={"effort": EFFORT},
                betas=[FALLBACK_BETA],
                fallbacks=FALLBACKS,
            )
        # 子の型を先に捕まえる（時間切れは接続の失敗の子、529 は状態の誤りの子）
        except anthropic.APITimeoutError as exc:
            return _failed(FailureKind.TIMEOUT, exc)
        except anthropic.APIConnectionError as exc:
            return _failed(FailureKind.CONNECTION, exc)
        except (anthropic.RateLimitError, anthropic.OverloadedError) as exc:
            return _failed(FailureKind.BUSY, exc)
        except anthropic.APIStatusError as exc:
            return _failed(FailureKind.OTHER, exc)
        except Exception as exc:
            return _failed(FailureKind.OTHER, exc)
        try:
            return _result(response)
        except Exception as exc:
            return _failed(FailureKind.OTHER, exc)

    def _client_for(self, anthropic: Any, timeout_s: float) -> Any:
        """待つ時間が同じ間は、クライアントを使い回す（自動のやり直しはしない）。"""
        if self._client is None or self._client_timeout != timeout_s:
            self._client = anthropic.Anthropic(
                api_key=self._credential, timeout=timeout_s, max_retries=0
            )
            self._client_timeout = timeout_s
        return self._client


def _result(response: Any) -> ChatResult:
    # 中身を読む前に拒否かを確かめる
    if response.stop_reason == REFUSAL:
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        logger.info("拒否されました: %s", category if isinstance(category, str) else "unknown")
        return Refused()
    # text のブロックだけをつなぐ（fallback などほかの種類は読まない）
    text = "".join(block.text for block in response.content if block.type == "text")
    return Replied(text)


def _failed(kind: FailureKind, exc: BaseException) -> Failed:
    logger.warning("会話の AI の返事をもらえません: %s", type(exc).__name__)
    return Failed(kind)
