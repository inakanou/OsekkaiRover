"""ExecStopPost 用の安全停止のコマンドのテスト（ハードは偽物）。"""

from __future__ import annotations

import fcntl
import importlib.util
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from osekkai.core import safe_stop
from osekkai.core.paths import Paths


class FakeHardware:
    def __init__(self, calls: list[str], fail: set[str] | None = None) -> None:
        self.calls = calls
        self.fail = fail or set()

    def _step(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise OSError("secret detail /dev/i2c-1")

    def zero_motor_pwm(self) -> None:
        self._step("pwm")

    def direction_pins_low(self) -> None:
        self._step("pins")

    def reset_mcu(self) -> None:
        self._step("reset")

    def close(self) -> None:
        self.calls.append("close")


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"XDG_RUNTIME_DIR": str(tmp_path / "run"), **extra}


def _opener(calls: list[str], fail: set[str] | None = None) -> safe_stop.HardwareOpener:
    def open_hardware() -> FakeHardware:
        calls.append("open")
        return FakeHardware(calls, fail)

    return open_hardware


def test_PWMを0にし方向ピンをLowにしマイコンをリセットする順(tmp_path: Path) -> None:
    calls: list[str] = []
    failed = safe_stop.run_safe_stop(_env(tmp_path), _opener(calls))
    assert calls == ["open", "pwm", "pins", "reset", "close"]
    assert failed == []


def test_段階の失敗は型の名前だけ記録して次へ進む(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []
    with caplog.at_level(logging.INFO, logger="osekkai"):
        failed = safe_stop.run_safe_stop(_env(tmp_path), _opener(calls, {"pwm", "pins"}))
    assert calls == ["open", "pwm", "pins", "reset", "close"]
    assert failed == ["pwm", "pins"]
    assert "OSError" in caplog.text
    assert "secret detail" not in caplog.text


def test_終了コード75の後は何もしない(tmp_path: Path) -> None:
    calls: list[str] = []
    failed = safe_stop.run_safe_stop(_env(tmp_path, EXIT_STATUS="75"), _opener(calls))
    assert calls == []
    assert failed == []


def test_ほかの終了コードの後は止める(tmp_path: Path) -> None:
    calls: list[str] = []
    safe_stop.run_safe_stop(_env(tmp_path, EXIT_STATUS="1"), _opener(calls))
    assert "pwm" in calls


def test_ロックをほかが持っているときは何もしない(tmp_path: Path) -> None:
    env = _env(tmp_path)
    lock_file = Paths(env).lock_file
    lock_file.parent.mkdir(parents=True)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls: list[str] = []
        safe_stop.run_safe_stop(env, _opener(calls))
        assert calls == []
    finally:
        os.close(fd)


def test_空いているロックは止める間だけ持ち放す(tmp_path: Path) -> None:
    env = _env(tmp_path)
    lock_file = Paths(env).lock_file
    lock_file.parent.mkdir(parents=True)
    lock_file.write_text("")
    seen: list[bool] = []

    class Probe(FakeHardware):
        def zero_motor_pwm(self) -> None:
            super().zero_motor_pwm()
            fd = os.open(lock_file, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append(False)
            except BlockingIOError:
                seen.append(True)
            finally:
                os.close(fd)

    calls: list[str] = []
    safe_stop.run_safe_stop(env, lambda: Probe(calls))
    assert seen == [True]
    fd = os.open(lock_file, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 終わった後は取れる
    finally:
        os.close(fd)


def test_ロックのファイルがなければ作らずに止める(tmp_path: Path) -> None:
    env = _env(tmp_path)
    calls: list[str] = []
    safe_stop.run_safe_stop(env, _opener(calls))
    assert "pwm" in calls
    assert not (tmp_path / "run").exists()


def test_ロックのファイルが読めないときは止める側に倒す(tmp_path: Path) -> None:
    env = _env(tmp_path)
    lock_file = Paths(env).lock_file
    lock_file.parent.mkdir(parents=True)
    lock_file.write_text("")
    lock_file.chmod(0o000)
    try:
        if os.access(lock_file, os.R_OK | os.W_OK):
            pytest.skip("root では権限の誤りを作れない")
        calls: list[str] = []
        safe_stop.run_safe_stop(env, _opener(calls))
        assert "pwm" in calls
    finally:
        lock_file.chmod(0o600)


def test_実機のアダプタがなければ何もしない(tmp_path: Path) -> None:
    def missing() -> FakeHardware:
        raise ImportError("No module named 'lgpio'")

    assert safe_stop.run_safe_stop(_env(tmp_path), missing) == []
    assert not (tmp_path / "run").exists()


def test_ハードを開けないときは記録して終える(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> FakeHardware:
        raise OSError("boom")

    with caplog.at_level(logging.INFO, logger="osekkai"):
        assert safe_stop.run_safe_stop(_env(tmp_path), broken) == ["open"]
    assert "OSError" in caplog.text


def test_mainは失敗しても終了コード0(tmp_path: Path) -> None:
    calls: list[str] = []
    code = safe_stop.main(environ=_env(tmp_path), open_hardware=_opener(calls, {"reset"}))
    assert code == 0


@pytest.mark.skipif(
    importlib.util.find_spec("lgpio") is not None or importlib.util.find_spec("smbus2") is not None,
    reason="ハードのライブラリが入っている環境では PC の振る舞いを確かめられない",
)
def test_PCではコマンドが何もせずに終了コード0で終わる(tmp_path: Path) -> None:
    env = {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    env.pop("EXIT_STATUS", None)
    result = subprocess.run(
        [sys.executable, "-m", "osekkai.core.safe_stop"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "run").exists()


def _hold_lock(env: dict[str, str]) -> int:
    lock_file = Paths(env).lock_file
    lock_file.parent.mkdir(parents=True)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_strictはすべての段階が済めば終了コード0(tmp_path: Path) -> None:
    calls: list[str] = []
    code = safe_stop.main(environ=_env(tmp_path), open_hardware=_opener(calls), argv=["--strict"])
    assert code == 0
    assert calls == ["open", "pwm", "pins", "reset", "close"]


@pytest.mark.parametrize("stage", ["pwm", "pins", "reset"])
def test_strictは段階の失敗があれば終了コード1(tmp_path: Path, stage: str) -> None:
    calls: list[str] = []
    code = safe_stop.main(
        environ=_env(tmp_path), open_hardware=_opener(calls, {stage}), argv=["--strict"]
    )
    assert code == 1
    assert calls == ["open", "pwm", "pins", "reset", "close"]  # 失敗しても次の段階へ進む


def test_strictなしは段階の失敗があっても終了コード0(tmp_path: Path) -> None:
    calls: list[str] = []
    code = safe_stop.main(environ=_env(tmp_path), open_hardware=_opener(calls, {"pwm"}), argv=[])
    assert code == 0


def test_strictはハードを開けなければ終了コード1(tmp_path: Path) -> None:
    def broken() -> FakeHardware:
        raise OSError("boom")

    assert safe_stop.main(environ=_env(tmp_path), open_hardware=broken, argv=["--strict"]) == 1
    assert safe_stop.main(environ=_env(tmp_path), open_hardware=broken, argv=[]) == 0


def test_strictはロックをほかが持っていれば触らずに終了コード1(tmp_path: Path) -> None:
    # 両方のユニットを止めた後にロックが持たれているなら、まだ何かが動いている
    env = _env(tmp_path)
    fd = _hold_lock(env)
    try:
        calls: list[str] = []
        assert safe_stop.main(environ=env, open_hardware=_opener(calls), argv=["--strict"]) == 1
        assert safe_stop.main(environ=env, open_hardware=_opener(calls), argv=[]) == 0
        assert calls == []
    finally:
        os.close(fd)


def test_strictは実機のアダプタがなければ終了コード1(tmp_path: Path) -> None:
    def missing() -> FakeHardware:
        raise ImportError("No module named 'lgpio'")

    assert safe_stop.main(environ=_env(tmp_path), open_hardware=missing, argv=["--strict"]) == 1
    assert safe_stop.main(environ=_env(tmp_path), open_hardware=missing, argv=[]) == 0


def test_strictは終了コード75の後で省いても終了コード1(tmp_path: Path) -> None:
    calls: list[str] = []
    env = _env(tmp_path, EXIT_STATUS="75")
    assert safe_stop.main(environ=env, open_hardware=_opener(calls), argv=["--strict"]) == 1
    assert safe_stop.main(environ=env, open_hardware=_opener(calls), argv=[]) == 0
    assert calls == []


def test_strictのログは段階の名前と型の名前だけ(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []
    with caplog.at_level(logging.INFO, logger="osekkai"):
        safe_stop.main(
            environ=_env(tmp_path), open_hardware=_opener(calls, {"reset"}), argv=["--strict"]
        )
    assert "reset" in caplog.text
    assert "OSError" in caplog.text
    assert "secret detail" not in caplog.text


@pytest.mark.skipif(
    importlib.util.find_spec("lgpio") is not None or importlib.util.find_spec("smbus2") is not None,
    reason="ハードのライブラリが入っている環境では PC の振る舞いを確かめられない",
)
def test_PCではstrictのコマンドが確かめられずに終了コード1で終わる(tmp_path: Path) -> None:
    env = {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    env.pop("EXIT_STATUS", None)
    result = subprocess.run(
        [sys.executable, "-m", "osekkai.core.safe_stop", "--strict"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert not (tmp_path / "run").exists()


def test_strictは途中で止まれば終了コード1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def crash(*_args: object, **_kwargs: object) -> list[str]:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(safe_stop, "run_safe_stop", crash)
    assert safe_stop.main(environ=_env(tmp_path), argv=["--strict"]) == 1
    assert safe_stop.main(environ=_env(tmp_path), argv=[]) == 0
