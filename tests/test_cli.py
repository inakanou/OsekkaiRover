"""起動の口（`python -m osekkai`）のテスト（13.1、13.2、13.5）。"""

import os
import signal
import subprocess
import sys
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path
from queue import Empty, Queue
from typing import IO

import pytest

from osekkai import __main__ as cli
from osekkai.adapters.fake.console import build_console_devices
from osekkai.core.config import CoreConfig
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.lcd_text import GLYPH_PATTERNS
from osekkai.core.paths import Paths
from osekkai.core.ports import Devices
from osekkai.core.runtime import ALREADY_RUNNING_MESSAGE, ExitCode

WAIT_S = 15.0

# --- 同じプロセスで呼ぶ ---


def test_ハードなしで話して終わると文章を端末に出す(
    xdg_dirs: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--fake-hardware", "--say", "こんにちは"]) == 0
    out, err = capsys.readouterr()
    assert "[声] こんにちは" in out
    assert "こんにちは" not in err  # ログには文章を出さない


def test_設定ファイルを指定できる(
    xdg_dirs: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "my.yaml"
    config.write_text("core:\n  cache_max_mb: 10\n", encoding="utf-8")
    assert cli.main(["--fake-hardware", "--config", str(config), "--say", "やあ"]) == 0
    assert "[声] やあ" in capsys.readouterr().out
    # 指定したときは、既定の場所に見本から作らない
    assert not (xdg_dirs["XDG_CONFIG_HOME"] / "osekkai" / "config.yaml").exists()


def test_実機のアダプタがなければ知らせて_1_で終わる(
    xdg_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "PI_ADAPTER_MODULE", "osekkai.adapters.no_such_module")
    assert cli.main(["--say", "こんにちは"]) == ExitCode.FATAL
    out, err = capsys.readouterr()
    assert cli.NO_PI_ADAPTER_MESSAGE in err
    assert "[声]" not in out


def test_PC_で実機のアダプタを選ぶとハードのライブラリがないので_1_で終わる(
    xdg_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import osekkai.adapters.pi.devices as pi_devices

    monkeypatch.setattr(pi_devices, "_has_library", lambda name: False)
    assert cli.main(["--say", "こんにちは"]) == ExitCode.FATAL
    assert cli.NO_PI_ADAPTER_MESSAGE in capsys.readouterr().err


class _PiModule(types.ModuleType):
    """実機のアダプタの身代わり。呼ばれた順と、そのときロックが持たれていたかを残す。"""

    def __init__(self, lock_file: Path) -> None:
        super().__init__("osekkai_test_pi_adapter")
        self.lock_file = lock_file
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.lock_held: list[bool] = []

    def _record(self, name: str, *args: object) -> Devices:
        self.calls.append((name, args))
        probe = InstanceLock(self.lock_file)
        held = not probe.acquire()  # 取れなければ、ほかが（この起動が）持っている
        probe.release()
        self.lock_held.append(held)
        return build_console_devices(time)

    def build_pi_devices(self, core_cfg: CoreConfig, glyphs: object) -> Devices:
        return self._record("build_pi_devices", core_cfg, glyphs)

    def build_pi_voice_devices(self, core_cfg: CoreConfig) -> Devices:
        return self._record("build_pi_voice_devices", core_cfg)


@pytest.fixture
def pi_module(xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> _PiModule:
    module = _PiModule(Paths().lock_file)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(cli, "PI_ADAPTER_MODULE", module.__name__)
    return module


@pytest.fixture
def held_lock(xdg_dirs: dict[str, Path]) -> Iterator[InstanceLock]:
    lock = InstanceLock(Paths().lock_file)
    assert lock.acquire()
    yield lock
    lock.release()


def test_実機のアダプタに基盤の設定と絵の文字の表を渡し_ロックを取ってから作る(
    pi_module: _PiModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[Devices] = []

    class _Recording(_StubRuntime):
        def __init__(self, devices: Devices, *args: object, **kwargs: object) -> None:
            super().__init__()
            received.append(devices)

        def run(self) -> ExitCode:
            return ExitCode.OK

    monkeypatch.setattr(cli, "Runtime", _Recording)
    assert cli.main([]) == 0
    [(name, (core_cfg, glyphs))] = pi_module.calls
    assert name == "build_pi_devices"
    assert isinstance(core_cfg, CoreConfig)
    assert glyphs is GLYPH_PATTERNS
    assert pi_module.lock_held == [True]
    assert len(received) == 1
    # 終えたらロックを放す
    probe = InstanceLock(Paths().lock_file)
    assert probe.acquire()
    probe.release()


def test_話して終わるは声のデバイスだけを_ロックを取ってから作る(
    pi_module: _PiModule, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--say", "こんにちは"]) == 0
    assert "[声] こんにちは" in capsys.readouterr().out
    [(name, (core_cfg,))] = pi_module.calls
    assert name == "build_pi_voice_devices"
    assert isinstance(core_cfg, CoreConfig)
    assert pi_module.lock_held == [True]


@pytest.mark.parametrize("argv", [[], ["--say", "こんにちは"]], ids=["起動", "話して終わる"])
def test_すでに動いていれば実機のデバイスを作らずに_75_で終わる(
    pi_module: _PiModule,
    held_lock: InstanceLock,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(argv) == ExitCode.ALREADY_RUNNING
    assert pi_module.calls == []
    out, err = capsys.readouterr()
    assert ALREADY_RUNNING_MESSAGE in err
    assert "[声]" not in out


def test_すでに動いていれば機能を読み込まずに_75_で終わる(
    held_lock: InstanceLock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 機能の発見は機能のモジュールを読み込むので、2 つ目の起動では行わない（1.7）
    built: list[object] = []
    monkeypatch.setattr(cli, "FeatureRegistry", lambda: built.append("registry"))
    assert cli.main(["--fake-hardware"]) == ExitCode.ALREADY_RUNNING
    assert built == []


@pytest.mark.parametrize(
    "argv",
    [["--fake-hardware"], ["--fake-hardware", "--say", "やあ"]],
    ids=["起動", "話して終わる"],
)
def test_すでに動いていれば端末のデバイスも作らずに_75_で終わる(
    held_lock: InstanceLock, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    import osekkai.adapters.fake.console as console

    built: list[object] = []
    monkeypatch.setattr(console, "build_console_devices", built.append)
    assert cli.main(argv) == ExitCode.ALREADY_RUNNING
    assert built == []


def test_実機のアダプタがなければロックを放してから_1_で終わる(
    xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "PI_ADAPTER_MODULE", "osekkai.adapters.no_such_module")
    assert cli.main([]) == ExitCode.FATAL
    probe = InstanceLock(Paths().lock_file)
    assert probe.acquire()
    probe.release()


class _StubRuntime:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.abandoned: list[str] = []

    def run(self) -> ExitCode:
        self.abandoned = ["stuck"]
        return ExitCode.OK


def test_見捨てた機能が残れば強制的に終える(
    xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    exits: list[int] = []
    monkeypatch.setattr(cli, "Runtime", _StubRuntime)
    monkeypatch.setattr(cli, "_force_exit", exits.append)
    assert cli.main(["--fake-hardware"]) == 0
    assert exits == [0]


def test_見捨てた機能がなければ強制的に終えない(
    xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Clean(_StubRuntime):
        def run(self) -> ExitCode:
            return ExitCode.FATAL

    exits: list[int] = []
    monkeypatch.setattr(cli, "Runtime", _Clean)
    monkeypatch.setattr(cli, "_force_exit", exits.append)
    assert cli.main(["--fake-hardware"]) == 1
    assert exits == []


# --- サブプロセス ---


@pytest.fixture
def env(xdg_dirs: dict[str, Path]) -> dict[str, str]:
    values = dict(os.environ)
    values.pop("JOURNAL_STREAM", None)
    values["PYTHONUNBUFFERED"] = "1"
    return values


def _spawn(env: dict[str, str], *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "osekkai", *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )


class _Reader:
    """流れの行を裏のスレッドで集め、待てるようにする。"""

    def __init__(self, stream: IO[str]) -> None:
        self.lines: list[str] = []
        self._queue: Queue[str] = Queue()
        threading.Thread(target=self._pump, args=(stream,), daemon=True).start()

    def _pump(self, stream: IO[str]) -> None:
        for line in stream:
            self._queue.put(line)

    def wait_for(self, text: str, timeout: float = WAIT_S) -> bool:
        deadline = time.monotonic() + timeout
        while not any(text in line for line in self.lines):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                self.lines.append(self._queue.get(timeout=remaining))
            except Empty:
                return False
        return True

    def text(self) -> str:
        while True:
            try:
                self.lines.append(self._queue.get_nowait())
            except Empty:
                return "".join(self.lines)


@pytest.fixture
def running(env: dict[str, str]) -> Iterator[tuple[subprocess.Popen[str], _Reader, _Reader]]:
    proc = _spawn(env, "--fake-hardware", "--verbose")
    assert proc.stdout is not None and proc.stderr is not None
    out, err = _Reader(proc.stdout), _Reader(proc.stderr)
    try:
        yield proc, out, err
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=WAIT_S)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()


def test_サブプロセスで話して終わる(env: dict[str, str]) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "osekkai", "--fake-hardware", "--say", "こんにちは"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=WAIT_S,
    )
    assert result.returncode == 0, result.stderr
    assert "[声] こんにちは" in result.stdout


def test_サブプロセスで待機画面を出し_Enter_の押下がログに出て_SIGINT_で_0_で終わる(
    running: tuple[subprocess.Popen[str], _Reader, _Reader],
) -> None:
    proc, out, err = running
    assert out.wait_for("[LCD] |"), err.text()
    assert proc.stdin is not None
    proc.stdin.write("\n")
    proc.stdin.flush()
    # 機能がないので、押下は受け手がいないとしてログに出る
    assert err.wait_for("受け手のいない押下を捨てました"), err.text()
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=WAIT_S) == 0, err.text()
    log = err.text()
    assert "停止の要求を受けたので止めます" in log
    assert "Traceback" not in log


def test_動いている間の話して終わるは_75_で終わる(
    running: tuple[subprocess.Popen[str], _Reader, _Reader], env: dict[str, str]
) -> None:
    proc, out, err = running
    assert out.wait_for("[LCD] |"), err.text()
    result = subprocess.run(
        [sys.executable, "-m", "osekkai", "--fake-hardware", "--say", "こんにちは"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=WAIT_S,
    )
    assert result.returncode == ExitCode.ALREADY_RUNNING
    assert ALREADY_RUNNING_MESSAGE in result.stderr
    assert "[声] こんにちは" not in result.stdout
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=WAIT_S) == 0
