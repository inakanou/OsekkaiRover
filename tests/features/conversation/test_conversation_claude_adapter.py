"""Claude API のアダプタ（3.1、3.2、6.1〜6.4、8.1、8.5）。

本物の SDK は読み込まない。SDK と同じ親子関係の例外の型を持つ偽の anthropic のモジュールを差し込む。
"""

from __future__ import annotations

import logging
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from osekkai.features.conversation.adapters import claude
from osekkai.features.conversation.adapters.claude import ClaudeChatModel, anthropic_available
from osekkai.features.conversation.ports import Failed, FailureKind, Refused, Replied, Turn

fake_value = "abc123"  # 偽の鍵の値（10 文字未満）
REPLY = "そうなんだ すごいね"
SYSTEM = "きまりの文"
TURNS = [
    Turn("user", "きょうね"),
    Turn("assistant", "うん"),
    Turn("user", "あそんだよ"),
]


# --- 偽の anthropic（SDK 2.6.0 と同じ親子関係） ---
class APIError(Exception):
    pass


class APIConnectionError(APIError):
    pass


class APITimeoutError(APIConnectionError):
    pass


class APIStatusError(APIError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class RateLimitError(APIStatusError):
    pass


class InternalServerError(APIStatusError):
    pass


class OverloadedError(InternalServerError):
    pass


@dataclass
class Block:
    type: str
    text: str = ""


@dataclass
class StopDetails:
    category: str


@dataclass
class Response:
    stop_reason: str = "end_turn"
    content: list[Block] = field(default_factory=list)
    stop_details: StopDetails | None = None


class FakeApi:
    """クライアントの作り方と呼び出しを覚える。"""

    def __init__(self) -> None:
        self.clients: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.response: Any = Response(content=[Block("text", REPLY)])
        self.error: BaseException | None = None

    def module(self) -> types.ModuleType:
        api = self
        fake = types.ModuleType("anthropic")

        class Messages:
            def create(self, **kwargs: Any) -> Any:
                api.calls.append(kwargs)
                if api.error is not None:
                    raise api.error
                return api.response

        class Anthropic:
            def __init__(self, **kwargs: Any) -> None:
                api.clients.append(kwargs)
                self.beta = types.SimpleNamespace(messages=Messages())

        for cls in (
            APIError,
            APIConnectionError,
            APITimeoutError,
            APIStatusError,
            RateLimitError,
            InternalServerError,
            OverloadedError,
            Anthropic,
        ):
            setattr(fake, cls.__name__, cls)
        return fake


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()
    monkeypatch.setitem(sys.modules, "anthropic", fake.module())
    return fake


def _reply(timeout_s: float = 20.0) -> Any:
    return ClaudeChatModel(fake_value).reply(SYSTEM, TURNS, timeout_s)


# --- 確かめる口 ---
def test_読み込んだことがあれば_ライブラリがあると答える(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", types.ModuleType("anthropic"))
    assert anthropic_available() is True


def test_見つからなければ_ライブラリがないと答える(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude, "find_spec", lambda name: None)
    assert anthropic_available() is False


def test_読み込めない印があれば_ライブラリがないと答える(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_import(name: str) -> None:
        raise ImportError(name)

    monkeypatch.setattr(claude, "find_spec", raise_import)
    assert anthropic_available() is False


def test_確かめる口は_anthropic_を読み込まない(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    anthropic_available()
    assert "anthropic" not in sys.modules


# --- 呼び出しの引数 ---
def test_クライアントは鍵と待つ時間を明示し_やり直しをしない(api: FakeApi) -> None:
    _reply(timeout_s=12.5)
    assert api.clients == [{"api_key": fake_value, "timeout": 12.5, "max_retries": 0}]


def test_beta_の_messages_を決まった引数で_1_回呼ぶ(api: FakeApi) -> None:
    _reply()
    assert api.calls == [
        {
            "model": "claude-opus-5-5",
            "max_tokens": 2048,
            "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            "messages": [
                {"role": "user", "content": "きょうね"},
                {"role": "assistant", "content": "うん"},
                {"role": "user", "content": "あそんだよ"},
            ],
            "output_config": {"effort": "low"},
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
    ]


def test_思考と_temperature_は渡さない(api: FakeApi) -> None:
    _reply()
    assert "thinking" not in api.calls[0]
    assert "temperature" not in api.calls[0]


def test_待つ時間が同じならクライアントを使い回す(api: FakeApi) -> None:
    model = ClaudeChatModel(fake_value)
    model.reply(SYSTEM, TURNS, 20.0)
    model.reply(SYSTEM, TURNS, 20.0)
    assert len(api.clients) == 1
    model.reply(SYSTEM, TURNS, 10.0)
    assert [c["timeout"] for c in api.clients] == [20.0, 10.0]
    assert len(api.calls) == 3


# --- 結果 ---
def test_text_のブロックをつないで返事にする(api: FakeApi) -> None:
    api.response = Response(content=[Block("text", "そうなんだ"), Block("text", " すごいね")])
    assert _reply() == Replied("そうなんだ すごいね")


def test_text_のほかのブロックは読まない(api: FakeApi) -> None:
    api.response = Response(
        content=[
            Block("thinking", "かんがえ"),
            Block("fallback", "べつのもの"),
            Block("text", REPLY),
        ]
    )
    assert _reply() == Replied(REPLY)


def test_stop_reason_が_refusal_なら拒否(api: FakeApi) -> None:
    api.response = Response(
        stop_reason="refusal",
        content=[Block("text", REPLY)],
        stop_details=StopDetails("cyber"),
    )
    assert _reply() == Refused()


def test_拒否の詳しさがなくても拒否(api: FakeApi) -> None:
    api.response = Response(stop_reason="refusal")
    assert _reply() == Refused()


# --- 例外の対応 ---
@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (APITimeoutError("t"), FailureKind.TIMEOUT),
        (APIConnectionError("c"), FailureKind.CONNECTION),
        (RateLimitError(429), FailureKind.BUSY),
        (OverloadedError(529), FailureKind.BUSY),
        (InternalServerError(500), FailureKind.OTHER),
        (APIStatusError(400), FailureKind.OTHER),
        (APIError("e"), FailureKind.OTHER),
        (RuntimeError("r"), FailureKind.OTHER),
    ],
)
def test_例外を失敗の種類にする(api: FakeApi, error: Exception, kind: FailureKind) -> None:
    api.error = error
    assert _reply() == Failed(kind)


def test_時間切れは接続の失敗より先に見る(api: FakeApi) -> None:
    # 時間切れは接続の失敗の子なので、順を間違えると CONNECTION になる
    assert issubclass(APITimeoutError, APIConnectionError)
    api.error = APITimeoutError("t")
    assert _reply() == Failed(FailureKind.TIMEOUT)


def test_クライアントを作れなくても例外にしない(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(**kwargs: Any) -> None:
        raise ValueError("x")

    monkeypatch.setattr(sys.modules["anthropic"], "Anthropic", broken)
    assert _reply() == Failed(FailureKind.OTHER)


def test_ライブラリがなくても例外にしない(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    assert _reply() == Failed(FailureKind.OTHER)


def test_返事の形が違っても例外にしない(api: FakeApi) -> None:
    api.response = types.SimpleNamespace(stop_reason="end_turn", content=None)
    assert _reply() == Failed(FailureKind.OTHER)


# --- ログ ---
def _all_logs(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records)


def test_返事のときログに返事も鍵も問いも出さない(
    api: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _reply()
    logs = _all_logs(caplog)
    for hidden_text in (REPLY, fake_value, SYSTEM, "あそんだよ"):
        assert hidden_text not in logs


def test_拒否のときはログに拒否の種類だけを出す(
    api: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    api.response = Response(
        stop_reason="refusal",
        content=[Block("text", REPLY)],
        stop_details=StopDetails("cyber"),
    )
    _reply()
    logs = _all_logs(caplog)
    assert "拒否されました" in logs
    assert "cyber" in logs
    for hidden_text in (REPLY, fake_value, SYSTEM, "あそんだよ"):
        assert hidden_text not in logs


def test_失敗のときはログに例外の型の名前だけを出す(
    api: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    api.error = RuntimeError(f"{fake_value} {REPLY}")
    _reply()
    logs = _all_logs(caplog)
    assert "RuntimeError" in logs
    assert fake_value not in logs
    assert REPLY not in logs
