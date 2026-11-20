"""おしゃべりの公開の口（api）。

要件 6.4、6.6、9.5、10.4（morning-night-talk）。
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import pytest

from osekkai.core.paths import Paths
from osekkai.features.conversation import api, envfile, ports, privacy, texts
from osekkai.features.conversation.adapters.claude import ClaudeChatModel

FAKE_VALUE = "zqfake9"  # 偽の鍵の値（10 文字より短く）

# 取り出す前の決まりの文（HEAD のまま）。1 文字も変わらないことを縛る
FROZEN_SYSTEM_PROMPT = (
    "あなたは「ローバー」という名前の、小さな車の形をしたロボットです。\n"
    "4 さいくらいの子どもと、日本語でおしゃべりします。\n"
    "\n"
    "話し方の決まり:\n"
    "返事は 1〜2 文にしてください。4 さいの子どもでも分かる、やさしい言葉だけを使ってください。\n"
    "絵文字・記号・箇条書きは使わないでください。文の終わりには「。」「！」「？」だけを使ってください。\n"
    "子どもを呼ぶときは、名前の代わりに必ず {name} と書いてください（例: {name}、すごいね！）。\n"
    "\n"
    "性格と守ること:\n"
    "明るくやさしく、少しおせっかいな性格で、子どもをよくほめてください。\n"
    "分からないことは分からないと言い、うそをつかないでください。\n"
    "住所・幼稚園の名前・電話番号・パスワードなどの個人のことは聞かないでください。"
    "教えられても覚えないでください。\n"
    "怖い話・暴力・大人向けの話はしないでください。"
    "そういう話になったら、やさしく別の話に変えてください。\n"
    "けが・病気・危ないこと（火・薬・電気・高い所など）の話が出たら、"
    "「ママか パパに すぐ いってね」と伝えてください。\n"
    "悲しいこと・痛いこと・いやなことをされたと聞いたら、"
    "その気持ちを受け止めて、家の大人に話すよう勧めてください。\n"
    "家の人に秘密にしようとは決して言わないでください。\n"
    "自分はロボットだと正直に言い、人間のふりをしないでください。\n"
    "歯みがき・お片づけ・早寝などのいい習慣を、楽しく応援してください。\n"
)
FROZEN_SHA256 = "c2caa60406806a67abb92c7e690a8340349dce06bbfd6afefc32611c3be10c39"


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(env={"XDG_CONFIG_HOME": str(tmp_path / "config")}, home=tmp_path)


def _write_env(paths: Paths, text: str) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    target = paths.config_dir / "env"
    target.write_text(text, encoding="utf-8")
    os.chmod(target, 0o600)


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


# --- 子ども向けの決まりの文（6.4） ---


def test_決まりの文は取り出す前と1文字も変わらない() -> None:
    assert texts.SYSTEM_PROMPT == FROZEN_SYSTEM_PROMPT
    assert hashlib.sha256(texts.SYSTEM_PROMPT.encode("utf-8")).hexdigest() == FROZEN_SHA256


def test_決まりの文は子ども向けの決まりを含めて組み立てる() -> None:
    assert texts.KID_RULES
    assert texts.SYSTEM_PROMPT.endswith(texts.KID_RULES)


@pytest.mark.parametrize(
    "rule",
    ["うそ", "住所", "電話番号", "覚えない", "怖い話", "暴力", "ママか パパ", "秘密", "家の大人"],
)
def test_子ども向けの決まりに守ることが入っている(rule: str) -> None:
    assert rule in texts.KID_RULES


def test_子ども向けの決まりには呼び名の目印とロボットの名前を入れない() -> None:
    # 豆知識の決まりの文に足して使うので、おしゃべりだけの決まりは入れない
    assert "{name}" not in texts.KID_RULES
    assert "ローバー" not in texts.KID_RULES


# --- 公開の名前（10.4） ---


def test_口は中のモジュールの名前をそのまま出す() -> None:
    assert api.ChatModelPort is ports.ChatModelPort
    assert api.ChatResult is ports.ChatResult
    assert api.Replied is ports.Replied
    assert api.Refused is ports.Refused
    assert api.Failed is ports.Failed
    assert api.FailureKind is ports.FailureKind
    assert api.Turn is ports.Turn
    assert api.shape_reply is privacy.shape_reply
    assert api.KID_RULES is texts.KID_RULES
    assert api.SetupProblem is envfile.SetupProblem


def test_口の名前の一覧() -> None:
    assert set(api.__all__) == {
        "ChatModelPort",
        "ChatResult",
        "Replied",
        "Refused",
        "Failed",
        "FailureKind",
        "Turn",
        "shape_reply",
        "KID_RULES",
        "SetupProblem",
        "setup_problem",
        "library_available",
        "claude_chat_model",
    }


# --- 鍵の確かめ（9.5） ---


def test_鍵のファイルがなければ問題の種類を返す(paths: Paths) -> None:
    assert api.setup_problem(paths) is api.SetupProblem.NO_FILE


def test_鍵が書かれていなければ問題の種類を返す(paths: Paths) -> None:
    _write_env(paths, "ANTHROPIC_API_KEY=\n")
    assert api.setup_problem(paths) is api.SetupProblem.NOT_WRITTEN


def test_鍵が使えれば問題はなく_値は返さない(paths: Paths) -> None:
    _write_env(paths, f"ANTHROPIC_API_KEY={FAKE_VALUE}\n")
    assert api.setup_problem(paths) is None


def test_ライブラリがあるかは_Claude_のアダプタの確かめと同じ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from osekkai.features.conversation.adapters import claude

    monkeypatch.setattr(claude, "anthropic_available", lambda: True)
    assert api.library_available() is True
    monkeypatch.setattr(claude, "anthropic_available", lambda: False)
    assert api.library_available() is False


# --- Claude のアダプタを作る（9.5） ---


def test_鍵とライブラリがあれば_Claude_のアダプタを作る(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(api, "library_available", lambda: True)
    _write_env(paths, f"ANTHROPIC_API_KEY={FAKE_VALUE}\n")

    model = api.claude_chat_model(paths)

    assert isinstance(model, ClaudeChatModel)
    assert FAKE_VALUE not in _logged(caplog)
    assert FAKE_VALUE not in repr(model)


@pytest.mark.parametrize("library", [True, False])
def test_鍵がなければ何も作らない(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, library: bool
) -> None:
    monkeypatch.setattr(api, "library_available", lambda: library)
    assert api.claude_chat_model(paths) is None


def test_鍵があってもライブラリがなければ何も作らない(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(api, "library_available", lambda: False)
    _write_env(paths, f"ANTHROPIC_API_KEY={FAKE_VALUE}\n")

    assert api.claude_chat_model(paths) is None
    assert FAKE_VALUE not in _logged(caplog)


def test_本物のライブラリがない所では何も作らない(paths: Paths) -> None:
    # 開発 PC と CI には anthropic がない。その時はなにも差し替えずに None
    if api.library_available():
        pytest.skip("anthropic が入っている")
    _write_env(paths, f"ANTHROPIC_API_KEY={FAKE_VALUE}\n")
    assert api.claude_chat_model(paths) is None
