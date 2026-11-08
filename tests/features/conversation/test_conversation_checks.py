"""おしゃべりの self-check の項目（conversation.setup。8.1、9.4）のテスト。

一時の保存先（XDG）と設定ファイルから基盤の ConfigLoader で報告を作り、
基盤の SelfCheckRunner で動かす。anthropic は開発 PC と CI にないので、
ライブラリがあるかは checks の `anthropic_available` を差し替える。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from osekkai.core.config import ConfigLoader, ConfigReport
from osekkai.core.paths import Paths
from osekkai.core.selfcheck import (
    CheckContext,
    CheckResult,
    CheckStatus,
    SelfCheckRunner,
    render_lines,
)
from osekkai.features.conversation import checks as checks_module
from osekkai.features.conversation.checks import (
    CONVERSATION_CHECKS,
    PROBLEM_REASONS,
    REASON_CONFIG_UNUSABLE,
    REASON_DISABLED,
    REASON_NO_FILE,
    REASON_NO_LIBRARY,
    REASON_NOT_PRIVATE,
    REASON_NOT_WRITTEN,
    REASON_UNREADABLE,
)
from osekkai.features.conversation.config import ConversationConfig
from osekkai.features.conversation.envfile import FILE_NAME, VARIABLE_NAME, SetupProblem

SETUP = "conversation.setup"
# 出力に出てはいけない、目立つ偽の値（10 文字より短く）
FAKE_VALUE = "zzfake123"


@pytest.fixture
def paths(xdg_dirs: dict[str, Path]) -> Paths:
    return Paths()


@pytest.fixture
def library(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """ライブラリがあるかの口を差し替える。中の値を変えると答えが変わる。"""
    state = [True]
    monkeypatch.setattr(checks_module, "anthropic_available", lambda: state[0])
    return state


def _report(paths: Paths, text: str | None) -> ConfigReport:
    if text is not None:
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        paths.config_file.write_text(text, encoding="utf-8")
    return ConfigLoader(paths, {"conversation": ConversationConfig}).load(create_missing=False)


def _run(paths: Paths, text: str | None = "conversation: {}\n") -> CheckResult:
    ctx = CheckContext(
        paths=paths, report=_report(paths, text), service=None, hardware_allowed=True
    )
    return dict(SelfCheckRunner().run(CONVERSATION_CHECKS, ctx))[SETUP]


def _write_env(paths: Paths, content: str | bytes, mode: int = 0o600) -> Path:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    target = paths.config_dir / FILE_NAME
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    os.chmod(target, mode)
    return target


def _assert_hidden(result: CheckResult) -> None:
    text = "\n".join(render_lines([(SETUP, result)]))
    assert FAKE_VALUE not in text
    assert FAKE_VALUE not in (result.reason or "")


# --- 申告 ---


def test_項目の名前と種類() -> None:
    assert [check.name for check in CONVERSATION_CHECKS] == [SETUP]
    (check,) = CONVERSATION_CHECKS
    assert check.hardware is False
    assert check.owner == "conversation"
    lowered = check.name.lower()
    for word in ("key", "token", "secret", "password"):
        assert word not in lowered


def test_理由は決まった文言() -> None:
    assert REASON_CONFIG_UNUSABLE == "おしゃべりの設定を使えない"
    assert REASON_DISABLED == "無効"
    assert REASON_NO_LIBRARY == "会話のライブラリがない"
    assert REASON_NO_FILE == "鍵のファイルがない"
    assert REASON_NOT_PRIVATE == "鍵のファイルを本人だけが読めるようにしていない"
    assert REASON_NOT_WRITTEN == "鍵が書かれていない"
    assert REASON_UNREADABLE == "鍵のファイルを読めない"


# --- 飛ばす ---


def test_設定のファイルがなければ飛ばす(paths: Paths, library: list[bool]) -> None:
    _write_env(paths, f"{VARIABLE_NAME}={FAKE_VALUE}\n")
    assert _run(paths, None) == CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)


def test_設定に誤りがあれば飛ばす(paths: Paths, library: list[bool]) -> None:
    _write_env(paths, f"{VARIABLE_NAME}={FAKE_VALUE}\n")
    result = _run(paths, "conversation:\n  daily_limit: 0\n")
    assert result == CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)


def test_無効なら飛ばす(paths: Paths, library: list[bool]) -> None:
    _write_env(paths, f"{VARIABLE_NAME}={FAKE_VALUE}\n")
    result = _run(paths, "conversation:\n  enabled: false\n")
    assert result == CheckResult(CheckStatus.SKIPPED, REASON_DISABLED)


def test_ライブラリがなければ飛ばす(paths: Paths, library: list[bool]) -> None:
    library[0] = False
    _write_env(paths, f"{VARIABLE_NAME}={FAKE_VALUE}\n")
    assert _run(paths) == CheckResult(CheckStatus.SKIPPED, REASON_NO_LIBRARY)


def test_ライブラリがなく鍵のファイルもなくても飛ばす(paths: Paths, library: list[bool]) -> None:
    library[0] = False
    assert _run(paths) == CheckResult(CheckStatus.SKIPPED, REASON_NO_LIBRARY)


# --- 鍵 ---


def test_鍵があればよい(paths: Paths, library: list[bool]) -> None:
    _write_env(paths, f"{VARIABLE_NAME}={FAKE_VALUE}\n")
    result = _run(paths)
    assert result == CheckResult(CheckStatus.OK)
    _assert_hidden(result)


def test_鍵のファイルがなければよいに理由を添える(paths: Paths, library: list[bool]) -> None:
    assert _run(paths) == CheckResult(CheckStatus.OK, REASON_NO_FILE)


def test_ほかの人が読めればよいに理由を添える(paths: Paths, library: list[bool]) -> None:
    _write_env(paths, f"{VARIABLE_NAME}={FAKE_VALUE}\n", mode=0o644)
    result = _run(paths)
    assert result == CheckResult(CheckStatus.OK, REASON_NOT_PRIVATE)
    _assert_hidden(result)


def test_鍵が書かれていなければよいに理由を添える(paths: Paths, library: list[bool]) -> None:
    _write_env(paths, f"OTHER_NAME={FAKE_VALUE}\n{VARIABLE_NAME}=\n")
    result = _run(paths)
    assert result == CheckResult(CheckStatus.OK, REASON_NOT_WRITTEN)
    _assert_hidden(result)


def test_鍵のファイルが読めなければよいに理由を添える(paths: Paths, library: list[bool]) -> None:
    _write_env(paths, b"\xff\xfe" + FAKE_VALUE.encode())
    result = _run(paths)
    assert result == CheckResult(CheckStatus.OK, REASON_UNREADABLE)
    _assert_hidden(result)


def test_鍵の問題はだめにしない(paths: Paths, library: list[bool]) -> None:
    # 版を戻しても直らないので FAILED にしない（9.4）
    _write_env(paths, f"{VARIABLE_NAME}={FAKE_VALUE}\n", mode=0o666)
    assert _run(paths).status is CheckStatus.OK


def test_問題の種類ごとに理由がある() -> None:
    assert set(PROBLEM_REASONS) == set(SetupProblem)
    assert PROBLEM_REASONS == {
        SetupProblem.NO_FILE: REASON_NO_FILE,
        SetupProblem.NOT_PRIVATE: REASON_NOT_PRIVATE,
        SetupProblem.NOT_WRITTEN: REASON_NOT_WRITTEN,
        SetupProblem.UNREADABLE: REASON_UNREADABLE,
    }
