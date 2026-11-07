"""音声の入口の self-check の項目（voice.mic・voice.model。6.4、8.4）のテスト。

一時の保存先（XDG）と設定ファイルから基盤の ConfigLoader で報告を作り、
基盤の SelfCheckRunner で動かす。arecord は subprocess.run を偽物に替える。
faster_whisper は開発 PC と CI にないので、読み込めるかは checks の補助を偽物に替える。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from osekkai.core.config import ConfigLoader, ConfigReport
from osekkai.core.paths import Paths
from osekkai.core.selfcheck import CheckContext, CheckResult, CheckStatus, SelfCheckRunner
from osekkai.voice import checks as checks_module
from osekkai.voice.adapters.whisper import MODEL_FILES, whisper_available
from osekkai.voice.checks import (
    REASON_CONFIG_UNUSABLE,
    REASON_DISABLED,
    REASON_NO_MIC,
    REASON_NO_MODEL,
    REASON_TYPED,
    VOICE_CHECKS,
)
from osekkai.voice.config import ModelSize, VoiceConfig, model_dir
from osekkai.voice.feature import VoiceGatewayFeature

MIC = "plughw:CARD=Himitsu42,DEV=0"
LIST_OUTPUT = f"null\n    Discard all samples\n{MIC}\n    USB Audio\n"


class FakeRun:
    """arecord -L の偽物。"""

    def __init__(self, output: str = LIST_OUTPUT, returncode: int = 0) -> None:
        self.output = output
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, self.returncode, self.output, "")


@pytest.fixture
def paths(xdg_dirs: dict[str, Path]) -> Paths:
    return Paths()


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def _set_whisper(monkeypatch: pytest.MonkeyPatch, available: bool) -> None:
    monkeypatch.setattr(checks_module, "whisper_available", lambda: available)


def _report(paths: Paths, voice_section: str) -> ConfigReport:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(voice_section, encoding="utf-8")
    return ConfigLoader(paths, {"voice": VoiceConfig}).load(create_missing=False)


def _ctx(paths: Paths, report: ConfigReport, *, hardware: bool = True) -> CheckContext:
    return CheckContext(paths=paths, report=report, service=None, hardware_allowed=hardware)


def _run(paths: Paths, voice_section: str, *, hardware: bool = True) -> dict[str, CheckResult]:
    ctx = _ctx(paths, _report(paths, voice_section), hardware=hardware)
    return dict(SelfCheckRunner().run(VOICE_CHECKS, ctx))


def _put_model(
    paths: Paths, model: ModelSize = "base", files: tuple[str, ...] = MODEL_FILES
) -> Path:
    directory = model_dir(paths, VoiceConfig(model=model))
    directory.mkdir(parents=True, exist_ok=True)
    for name in files:
        (directory / name).write_bytes(b"x")
    return directory


WHISPER_CONFIG = f'voice:\n  recognizer: whisper\n  mic_device: "{MIC}"\n'


# --- 申告 ---


def test_項目の名前と種類() -> None:
    by_name = {check.name: check for check in VOICE_CHECKS}
    assert list(by_name) == ["voice.mic", "voice.model"]
    assert by_name["voice.mic"].hardware is True
    # arecord -L の打ち切り（5 秒）の後に、基盤の時間切れになる
    assert 5.0 < by_name["voice.mic"].timeout_s <= 10.0
    assert by_name["voice.model"].hardware is False


def test_機能の申告に項目が入っている() -> None:
    assert VoiceGatewayFeature.spec.checks == VOICE_CHECKS


# --- よい・だめ ---


def test_マイクがあり_モデルがそろっていれば_どちらもよい(paths: Paths, fake_run: FakeRun) -> None:
    _put_model(paths)
    results = _run(paths, WHISPER_CONFIG)
    assert results["voice.mic"] == CheckResult(CheckStatus.OK)
    assert results["voice.model"] == CheckResult(CheckStatus.OK)
    assert fake_run.calls[0][0] == ["arecord", "-L"]


def test_マイクが一覧にないとだめ(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", FakeRun(output="null\n    Discard all samples\n"))
    _put_model(paths)
    results = _run(paths, WHISPER_CONFIG)
    assert results["voice.mic"] == CheckResult(CheckStatus.FAILED, REASON_NO_MIC)
    assert REASON_NO_MIC == "マイクが見つからない"


def test_arecord_が失敗するとマイクはだめ(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", FakeRun(returncode=1))
    results = _run(paths, WHISPER_CONFIG)
    assert results["voice.mic"] == CheckResult(CheckStatus.FAILED, REASON_NO_MIC)


def test_モデルがないとだめ(paths: Paths, fake_run: FakeRun) -> None:
    results = _run(paths, WHISPER_CONFIG)
    assert results["voice.model"] == CheckResult(CheckStatus.FAILED, REASON_NO_MODEL)
    assert REASON_NO_MODEL == "聞き取りのモデルがない"


@pytest.mark.parametrize("missing", MODEL_FILES)
def test_モデルのファイルが_1_つでも欠けるとだめ(
    paths: Paths, fake_run: FakeRun, missing: str
) -> None:
    _put_model(paths, files=tuple(name for name in MODEL_FILES if name != missing))
    results = _run(paths, WHISPER_CONFIG)
    assert results["voice.model"] == CheckResult(CheckStatus.FAILED, REASON_NO_MODEL)


def test_モデルは設定の大きさの置き場所で探す(paths: Paths, fake_run: FakeRun) -> None:
    _put_model(paths, model="base")
    results = _run(paths, WHISPER_CONFIG + "  model: small\n")
    assert results["voice.model"] == CheckResult(CheckStatus.FAILED, REASON_NO_MODEL)
    _put_model(paths, model="small")
    results = _run(paths, WHISPER_CONFIG + "  model: small\n")
    assert results["voice.model"] == CheckResult(CheckStatus.OK)


def test_auto_で_faster_whisper_を読み込めれば確かめる(
    paths: Paths, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_whisper(monkeypatch, True)
    results = _run(paths, f'voice:\n  mic_device: "{MIC}"\n')
    assert results["voice.mic"] == CheckResult(CheckStatus.OK)
    assert results["voice.model"] == CheckResult(CheckStatus.FAILED, REASON_NO_MODEL)


def test_ハードを触れなければマイクは基盤が飛ばし_モデルは確かめる(
    paths: Paths, fake_run: FakeRun
) -> None:
    _put_model(paths)
    results = _run(paths, WHISPER_CONFIG, hardware=False)
    assert results["voice.mic"].status is CheckStatus.SKIPPED
    assert results["voice.model"] == CheckResult(CheckStatus.OK)
    assert fake_run.calls == []


# --- 飛ばした ---


def test_typed_の設定ではどちらも飛ばす(
    paths: Paths, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_whisper(monkeypatch, True)
    results = _run(paths, "voice:\n  recognizer: typed\n")
    assert results["voice.mic"] == CheckResult(CheckStatus.SKIPPED, REASON_TYPED)
    assert results["voice.model"] == CheckResult(CheckStatus.SKIPPED, REASON_TYPED)
    assert REASON_TYPED == "打った文字の聞き取り"
    assert fake_run.calls == []


def test_auto_で_faster_whisper_を読み込めなければどちらも飛ばす(
    paths: Paths, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_whisper(monkeypatch, False)
    results = _run(paths, "voice:\n  recognizer: auto\n")
    assert results["voice.mic"] == CheckResult(CheckStatus.SKIPPED, REASON_TYPED)
    assert results["voice.model"] == CheckResult(CheckStatus.SKIPPED, REASON_TYPED)
    assert fake_run.calls == []


def test_開発_PC_の既定の設定ではどちらも飛ばす(paths: Paths, fake_run: FakeRun) -> None:
    # 開発 PC と CI には faster_whisper がない（本物の判定のまま動かす）
    assert whisper_available() is False
    results = _run(paths, "")
    assert results["voice.mic"] == CheckResult(CheckStatus.SKIPPED, REASON_TYPED)
    assert results["voice.model"] == CheckResult(CheckStatus.SKIPPED, REASON_TYPED)


def test_無効の設定ではどちらも飛ばす(paths: Paths, fake_run: FakeRun) -> None:
    results = _run(paths, WHISPER_CONFIG + "  enabled: false\n")
    assert results["voice.mic"] == CheckResult(CheckStatus.SKIPPED, REASON_DISABLED)
    assert results["voice.model"] == CheckResult(CheckStatus.SKIPPED, REASON_DISABLED)
    assert fake_run.calls == []


@pytest.mark.parametrize(
    "text",
    [
        "voice:\n  listen_seconds: 99\n",  # 入口の節の誤り（基盤の core.config が知らせる）
        "voice: [1, 2\n",  # YAML の誤り
    ],
)
def test_設定を使えなければどちらも飛ばす(paths: Paths, fake_run: FakeRun, text: str) -> None:
    results = _run(paths, text)
    assert results["voice.mic"] == CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)
    assert results["voice.model"] == CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)
    assert fake_run.calls == []


def test_設定ファイルがなければどちらも飛ばす(paths: Paths, fake_run: FakeRun) -> None:
    report = ConfigLoader(paths, {"voice": VoiceConfig}).load(create_missing=False)
    results = dict(SelfCheckRunner().run(VOICE_CHECKS, _ctx(paths, report)))
    assert results["voice.mic"] == CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)
    assert results["voice.model"] == CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)


# --- 文言 ---


def test_理由に値やパスを入れない(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, fake_run: FakeRun
) -> None:
    reasons = [REASON_NO_MIC, REASON_NO_MODEL, REASON_TYPED, REASON_DISABLED]
    reasons.append(REASON_CONFIG_UNUSABLE)
    monkeypatch.setattr(subprocess, "run", FakeRun(output="null\n"))
    for text in (
        WHISPER_CONFIG,
        WHISPER_CONFIG + "  model: small\n",
        "voice:\n  recognizer: typed\n",
    ):
        reasons.extend(result.reason for result in _run(paths, text).values())
    directory = str(model_dir(paths, VoiceConfig()))
    for reason in reasons:
        assert MIC not in reason
        assert "Himitsu42" not in reason
        assert "/" not in reason
        assert directory not in reason
        assert "small" not in reason
        assert "base" not in reason
        assert "faster-whisper" not in reason
