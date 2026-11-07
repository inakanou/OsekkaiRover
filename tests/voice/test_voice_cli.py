"""開発用の聞き取りの確認のコマンド（`python -m osekkai.voice --listen-once`。9.3、9.4）のテスト。

実機の聞き取りは開発 PC と CI にないので、聞き取りを作る関数（`_build_recognizer`）を
台本どおりの偽の聞き取りに替える。時刻は偽の時計の単調な時刻を渡す。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest
from voice_fakes import Fail, ScriptedRecognizer

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.paths import Paths
from osekkai.voice import __main__ as voice_cli
from osekkai.voice.adapters.whisper import WhisperRecognizer
from osekkai.voice.config import VoiceConfig, model_dir
from osekkai.voice.recognizer import Recognizer

PHRASE = "だるまさん しよう"


class _Builder:
    """`_build_recognizer` の代わり。呼ばれた回数と受けた設定を残す。"""

    def __init__(self, recognizer: ScriptedRecognizer) -> None:
        self.recognizer = recognizer
        self.configs: list[VoiceConfig] = []

    def __call__(self, config: VoiceConfig, paths: Paths) -> Recognizer:
        self.configs.append(config)
        return self.recognizer


def _install(monkeypatch: pytest.MonkeyPatch, recognizer: ScriptedRecognizer) -> _Builder:
    builder = _Builder(recognizer)
    monkeypatch.setattr(voice_cli, "_build_recognizer", builder)
    return builder


def _run(args: list[str], clock: FakeClock) -> int:
    return voice_cli.main(args, monotonic=clock.monotonic)


def test_聞き取った文字とかかった時間を標準出力にだけ出す(
    xdg_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    clock: FakeClock,
) -> None:
    caplog.set_level(logging.DEBUG)
    rec = ScriptedRecognizer([PHRASE], clock=clock, record_seconds=5.0, transcribe_seconds=1.5)
    builder = _install(monkeypatch, rec)

    assert _run(["--listen-once"], clock) == 0

    out, err = capsys.readouterr()
    assert f"聞き取った言葉: {PHRASE}" in out
    assert "録音 5.0 秒、文字にする 1.5 秒" in out
    assert PHRASE not in err
    assert PHRASE not in caplog.text
    assert all(PHRASE not in str(r.args) for r in caplog.records)
    assert rec.prepare_calls == 1
    # 設定ファイルがなければ既定値で動く（作らない）
    assert len(builder.configs) == 1
    assert rec.calls[0].seconds == VoiceConfig().listen_seconds
    assert rec.calls[0].hints == ()
    assert not Paths().config_file.exists()


def test_録音の長さを指定できる(
    xdg_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: FakeClock,
) -> None:
    rec = ScriptedRecognizer([PHRASE])
    _install(monkeypatch, rec)
    assert _run(["--listen-once", "--seconds", "3"], clock) == 0
    assert rec.calls[0].seconds == 3.0


def test_設定ファイルの録音の長さを使う(
    xdg_dirs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
) -> None:
    config = tmp_path / "my.yaml"
    config.write_text("voice:\n  listen_seconds: 7\n  model: small\n", encoding="utf-8")
    rec = ScriptedRecognizer([PHRASE])
    builder = _install(monkeypatch, rec)
    assert _run(["--listen-once", "--config", str(config)], clock) == 0
    assert rec.calls[0].seconds == 7.0
    assert builder.configs[0].model == "small"


def test_何も聞き取れなければそう出す(
    xdg_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: FakeClock,
) -> None:
    _install(monkeypatch, ScriptedRecognizer([""], clock=clock, record_seconds=2.0))
    assert _run(["--listen-once"], clock) == 0
    out = capsys.readouterr().out
    assert voice_cli.NOT_HEARD_TEXT in out
    assert "聞き取った言葉" not in out
    assert "録音 2.0 秒、文字にする 0.0 秒" in out


def test_ロボットが動いていれば録音せずに_75_で終える(
    xdg_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: FakeClock,
) -> None:
    builder = _install(monkeypatch, ScriptedRecognizer([PHRASE]))
    other = InstanceLock(Paths().lock_file)
    assert other.acquire()
    try:
        assert _run(["--listen-once"], clock) == voice_cli.EXIT_ALREADY_RUNNING == 75
    finally:
        other.release()
    out, err = capsys.readouterr()
    assert "ロボットが動いています。止めてから試してください" in err
    assert out == ""
    assert builder.configs == []  # 聞き取りを作らない（録音しない）


def test_終わったらロックを放す(
    xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    _install(monkeypatch, ScriptedRecognizer([PHRASE]))
    assert _run(["--listen-once"], clock) == 0
    lock = InstanceLock(Paths().lock_file)
    assert lock.acquire()
    lock.release()


def test_設定の誤りでは聞き取りを作らずに_1_で終える(
    xdg_dirs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: FakeClock,
) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("voice:\n  listen_seconds: 99\n", encoding="utf-8")
    builder = _install(monkeypatch, ScriptedRecognizer([PHRASE]))
    assert _run(["--listen-once", "--config", str(config)], clock) == 1
    out, err = capsys.readouterr()
    assert "voice.listen_seconds" in err
    assert "99" not in err
    assert out == ""
    assert builder.configs == []
    # ロックは放している
    lock = InstanceLock(Paths().lock_file)
    assert lock.acquire()
    lock.release()


def test_YAML_の誤りでも_1_で終える(
    xdg_dirs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: FakeClock,
) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("voice: [\n", encoding="utf-8")
    builder = _install(monkeypatch, ScriptedRecognizer([PHRASE]))
    assert _run(["--listen-once", "--config", str(config)], clock) == 1
    assert "YAML" in capsys.readouterr().err
    assert builder.configs == []


def test_指定した設定ファイルがなければ_1_で終える(
    xdg_dirs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
) -> None:
    builder = _install(monkeypatch, ScriptedRecognizer([PHRASE]))
    assert _run(["--listen-once", "--config", str(tmp_path / "none.yaml")], clock) == 1
    assert builder.configs == []


def test_ほかの機能の節があっても動く(
    xdg_dirs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: FakeClock,
) -> None:
    config = tmp_path / "my.yaml"
    config.write_text("conversation:\n  enabled: true\nvoice: {}\n", encoding="utf-8")
    _install(monkeypatch, ScriptedRecognizer([PHRASE]))
    assert _run(["--listen-once", "--config", str(config)], clock) == 0
    # ほかの機能の節を「知らない項目」と言わない（このコマンドは入口の節だけを読む）
    assert "知らない項目" not in capsys.readouterr().err


def test_準備ができなければ決まった理由で_1_で終える(
    xdg_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: FakeClock,
) -> None:
    rec = ScriptedRecognizer([PHRASE], prepare_fails=True)
    _install(monkeypatch, rec)
    assert _run(["--listen-once"], clock) == 1
    out, err = capsys.readouterr()
    assert "聞き取りの準備ができません（マイクかモデルを確かめてください）" in err
    assert out == ""
    assert rec.calls == []


def test_聞き取りに失敗すれば型の名前を出して_1_で終える(
    xdg_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: FakeClock,
) -> None:
    _install(monkeypatch, ScriptedRecognizer([Fail("CalledProcessError")]))
    assert _run(["--listen-once"], clock) == 1
    out, err = capsys.readouterr()
    assert voice_cli.FAILED_TEXT in err
    assert "CalledProcessError" in err
    assert out == ""


def test_listen_once_がなければ引数の誤り(
    xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    builder = _install(monkeypatch, ScriptedRecognizer([PHRASE]))
    with pytest.raises(SystemExit) as info:
        _run([], clock)
    assert info.value.code == 2
    assert builder.configs == []


@pytest.mark.parametrize("seconds", ["0", "-1", "abc"])
def test_録音の長さの誤りは引数の誤り(
    xdg_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    seconds: str,
) -> None:
    _install(monkeypatch, ScriptedRecognizer([PHRASE]))
    with pytest.raises(SystemExit) as info:
        _run(["--listen-once", "--seconds", seconds], clock)
    assert info.value.code == 2


def test_既定の聞き取りは実機の聞き取り(xdg_dirs: dict[str, Path]) -> None:
    config = VoiceConfig()
    paths = Paths()
    rec = voice_cli._build_recognizer(config, paths)
    assert isinstance(rec, WhisperRecognizer)
    assert rec._model_dir == model_dir(paths, config)


def test_help_を出せる() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "osekkai.voice", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert "--listen-once" in result.stdout
    assert "--seconds" in result.stdout
    assert "--config" in result.stdout
