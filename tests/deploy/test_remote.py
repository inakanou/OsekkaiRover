"""ssh・scp のやり取りのテスト（1.6、3.3、3.4、4.5）。"""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from fake_runner import FakeRunner

from rover_deploy.common import ExitCode
from rover_deploy.remote import (
    PUSH_COMMAND,
    AgentProtocolError,
    AgentReply,
    DeviceDisconnected,
    DeviceUnreachable,
    Remote,
    RemoteTimedOut,
    StreamProtocolError,
    TransferFailed,
    build_tool_archive,
    popen_spawner,
)
from rover_deploy.system import CommandTimedOut, RunResult

HOST = "rover"
OPTS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=3",
)
SSH = ("ssh", *OPTS, "-T", "--", HOST)
SSH_TTY = ("ssh", *OPTS, "-t", "--", HOST)
AGENT_PREFIX = "cd ~/osekkai-rover/tool && /usr/bin/python3 -m rover_deploy.agent"
PACKAGE_DIR = Path(__file__).resolve().parents[2] / "deploy" / "rover_deploy"
TARGET_JSON = json.dumps({"kind": "branch", "name": 'it\'s a "test" $(rm -rf ~); x', "n": 1})


class FakeProcess:
    """決めた出力を少しずつ返す、偽の動いているコマンド。"""

    def __init__(self, chunks: Sequence[bytes], returncode: int = 0) -> None:
        self.chunks = list(chunks)
        self.returncode = returncode
        self.reads = 0
        self.killed = False
        self.waited = False

    def read_chunk(self) -> bytes:
        self.reads += 1
        return self.chunks.pop(0) if self.chunks else b""

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class FakeSpawner:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.calls: list[tuple[tuple[str, ...], bool]] = []

    def __call__(self, argv: Sequence[str], interactive: bool) -> FakeProcess:
        self.calls.append((tuple(argv), interactive))
        return self.process


def make_remote(
    run: FakeRunner, process: FakeProcess | None = None
) -> tuple[Remote, FakeSpawner, list[bytes]]:
    spawner = FakeSpawner(process or FakeProcess([]))
    echoed: list[bytes] = []
    return Remote(HOST, run, spawn=spawner, echo=echoed.append), spawner, echoed


def remote_args(command: str) -> list[str]:
    """実機のシェルが分ける形で、道具の引数を取り出す。"""
    words = shlex.split(command)
    assert words[:6] == ["cd", "~/osekkai-rover/tool", "&&", "/usr/bin/python3", "-m"] + [
        "rover_deploy.agent"
    ]
    return words[6:]


# --- 引数 ---


def test_host_starting_with_dash_is_refused(fake_runner: FakeRunner) -> None:
    with pytest.raises(ValueError):
        Remote("-oProxyCommand=x", fake_runner)
    with pytest.raises(ValueError):
        Remote("", fake_runner)


def test_agent_argv_has_no_tty_and_timeouts(fake_runner: FakeRunner) -> None:
    fake_runner.script(SSH, stdout='{"ok": true}\n')
    remote, _, _ = make_remote(fake_runner)
    reply = remote.agent("status")
    assert reply == AgentReply(0, {"ok": True})
    (call,) = fake_runner.calls
    assert call.argv == (*SSH, f"{AGENT_PREFIX} status")
    assert call.timeout is not None
    assert call.input is None


def test_agent_arguments_survive_the_remote_shell(fake_runner: FakeRunner) -> None:
    fake_runner.script(SSH, stdout='{"ok": true}')
    remote, _, _ = make_remote(fake_runner)
    remote.agent("preflight", "--target", TARGET_JSON)
    command = fake_runner.calls[0].argv[-1]
    # ssh は引数を 1 つの文にして実機のシェルに渡す。分けると元に戻る
    assert remote_args(command) == ["preflight", "--target", TARGET_JSON]
    assert json.loads(remote_args(command)[2])["name"] == 'it\'s a "test" $(rm -rf ~); x'


# --- JSON を読む ---


@pytest.mark.parametrize("code", [0, 1, 2])
def test_agent_returns_json_and_exit_code(fake_runner: FakeRunner, code: int) -> None:
    fake_runner.script(SSH, returncode=code, stdout='  {"ok": false, "reason": "lock_busy"}\n')
    remote, _, _ = make_remote(fake_runner)
    assert remote.agent("backup") == AgentReply(code, {"ok": False, "reason": "lock_busy"})


def test_usage_error_without_json(fake_runner: FakeRunner) -> None:
    fake_runner.script(SSH, returncode=2, stdout="", stderr="usage: secret-host\n")
    remote, _, _ = make_remote(fake_runner)
    assert remote.agent("nope") == AgentReply(2, None)


@pytest.mark.parametrize(
    "stdout",
    ["", "not json", '{"ok": true}\n{"ok": true}\n', "[1, 2]", "Traceback ...\n"],
)
def test_non_json_is_protocol_error(fake_runner: FakeRunner, stdout: str) -> None:
    fake_runner.script(SSH, stdout=stdout)
    remote, _, _ = make_remote(fake_runner)
    with pytest.raises(AgentProtocolError) as info:
        remote.agent("status")
    assert info.value.exit_code == ExitCode.ABORTED


def test_ssh_255_is_unreachable(fake_runner: FakeRunner) -> None:
    fake_runner.script(SSH, returncode=255, stderr="ssh: connect to host rover: timed out")
    remote, _, _ = make_remote(fake_runner)
    with pytest.raises(DeviceUnreachable) as info:
        remote.agent("status")
    assert info.value.exit_code == ExitCode.ABORTED
    # 標準エラーの中身を文言に写さない
    assert "rover" not in str(info.value)
    assert "timed out" not in str(info.value)


def test_agent_timeout(fake_runner: FakeRunner) -> None:
    fake_runner.raise_on(SSH, CommandTimedOut("ssh"))
    remote, _, _ = make_remote(fake_runner)
    with pytest.raises(RemoteTimedOut):
        remote.agent("status")


# --- 端末を割り当てる（初期設定） ---


def test_tty_agent_reads_last_line_and_echoes_progress(fake_runner: FakeRunner) -> None:
    process = FakeProcess(
        [
            b"[1/9] apt \xe3\x81\xae\r\n[sudo] password for ",
            b"pi: ",
            b'\r\nok\r\n{"ok": true, ',
            b'"reason": ""}\r\n',
        ]
    )
    remote, spawner, echoed = make_remote(fake_runner, process)
    reply = remote.agent("provision", "--target", TARGET_JSON, tty=True)
    assert reply == AgentReply(0, {"ok": True, "reason": ""})
    ((argv, interactive),) = spawner.calls
    assert interactive is True
    assert argv[: len(SSH_TTY)] == SSH_TTY
    assert remote_args(argv[-1]) == ["provision", "--target", TARGET_JSON]
    assert fake_runner.calls == []
    shown = b"".join(echoed)
    # sudo の問いは改行がなくてもすぐ見せる。JSON の行は見せない
    assert echoed[0].endswith(b"[sudo] password for ")
    assert b"pi: " in shown
    assert b"ok\r\n" in shown
    assert b"{" not in shown


def test_tty_agent_unreachable(fake_runner: FakeRunner) -> None:
    remote, _, _ = make_remote(fake_runner, FakeProcess([b"\r\n"], returncode=255))
    with pytest.raises(DeviceUnreachable):
        remote.agent("provision", tty=True)


def test_tty_agent_non_json_last_line(fake_runner: FakeRunner) -> None:
    remote, _, _ = make_remote(fake_runner, FakeProcess([b'{"ok": true}\r\nbye\r\n']))
    with pytest.raises(AgentProtocolError):
        remote.agent("provision", tty=True)


# --- 道具を写す ---


def tar_names(data: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return tar.getmembers()


def test_push_tool_sends_archive(fake_runner: FakeRunner) -> None:
    fake_runner.script(SSH)
    remote, _, _ = make_remote(fake_runner)
    remote.push_tool(PACKAGE_DIR)
    (call,) = fake_runner.calls
    assert call.argv == (*SSH, PUSH_COMMAND)
    assert call.input is not None
    members = tar_names(call.input)
    names = {m.name for m in members}
    assert {"rover_deploy", "rover_deploy/__init__.py", "rover_deploy/agent.py"} <= names
    assert "rover_deploy/remote.py" in names
    assert all(n == "rover_deploy" or n.startswith("rover_deploy/") for n in names)
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert all(m.uid == 0 and m.uname == "" and m.gname == "" for m in members)


def test_archive_excludes_cache(tmp_path: Path) -> None:
    pkg = tmp_path / "rover_deploy"
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "x.pyc").write_bytes(b"")
    (pkg / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"")
    (pkg / "sub").mkdir()
    (pkg / "sub" / "m.py").write_text("A = 1\n")
    names = {m.name for m in tar_names(build_tool_archive(pkg))}
    assert names == {
        "rover_deploy",
        "rover_deploy/__init__.py",
        "rover_deploy/sub",
        "rover_deploy/sub/m.py",
    }


def test_archive_refuses_non_package(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_tool_archive(tmp_path)


@pytest.mark.parametrize(("code", "error"), [(255, DeviceUnreachable), (1, TransferFailed)])
def test_push_tool_failure(fake_runner: FakeRunner, code: int, error: type[Exception]) -> None:
    fake_runner.script(SSH, returncode=code)
    remote, _, _ = make_remote(fake_runner)
    with pytest.raises(error):
        remote.push_tool(PACKAGE_DIR)


@pytest.mark.skipif(
    shutil.which("sh") is None or shutil.which("tar") is None, reason="sh・tar がない"
)
def test_push_command_replaces_tool(tmp_path: Path) -> None:
    """実機で動く文を、手元の sh で動かして確かめる（家のディレクトリは一時の場所）。"""
    tool = tmp_path / "osekkai-rover" / "tool"
    (tool / "rover_deploy").mkdir(parents=True)
    (tool / "rover_deploy" / "stale.py").write_text("old\n")
    (tool / "rover_deploy.old").mkdir()  # 前に途中で止まった残り
    archive = build_tool_archive(PACKAGE_DIR)
    env = {**os.environ, "HOME": str(tmp_path)}
    result = subprocess.run(
        ["sh", "-c", PUSH_COMMAND], input=archive, env=env, capture_output=True, check=False
    )
    assert result.returncode == 0
    assert sorted(p.name for p in tool.iterdir()) == ["rover_deploy"]
    assert (tool / "rover_deploy" / "agent.py").read_bytes() == (
        PACKAGE_DIR / "agent.py"
    ).read_bytes()
    assert not (tool / "rover_deploy" / "stale.py").exists()
    assert stat.S_IMODE(tool.stat().st_mode) == 0o700


# --- JSON の行を流す ---


def test_stream_yields_lines_as_they_arrive(fake_runner: FakeRunner) -> None:
    process = FakeProcess(
        [
            b'{"t": 1, "step": "prepare", "status": "start", "reason": null}\n{"t": 2, ',
            b'"step": "prepare", "status": "ok", "reason": null}\n',
            b'\n{"t": 3, "result": {"outcome": "succeeded"}}\n',
        ]
    )
    remote, spawner, _ = make_remote(fake_runner, process)
    lines = remote.stream("follow", "20261105-1")
    first = next(lines)
    assert first["step"] == "prepare"
    assert process.reads == 1  # 1 行目は、残りを読む前に返る
    rest = list(lines)
    assert [line.get("t") for line in rest] == [2, 3]
    assert rest[-1]["result"] == {"outcome": "succeeded"}
    ((argv, interactive),) = spawner.calls
    assert interactive is False
    assert argv == (*SSH, f"{AGENT_PREFIX} follow 20261105-1")
    assert process.waited
    assert not process.killed


def test_stream_ends_on_error_line(fake_runner: FakeRunner) -> None:
    process = FakeProcess([b'{"error": "ended_without_result"}\r\n'])
    remote, _, _ = make_remote(fake_runner, process)
    assert list(remote.stream("follow", "x")) == [{"error": "ended_without_result"}]


@pytest.mark.parametrize("code", [255, 0])
def test_stream_disconnected_before_result(fake_runner: FakeRunner, code: int) -> None:
    process = FakeProcess([b'{"t": 1, "step": "verify", "status": "start"}\n'], returncode=code)
    remote, _, _ = make_remote(fake_runner, process)
    seen: list[dict[str, object]] = []
    with pytest.raises(DeviceDisconnected) as info:
        for line in remote.stream("follow", "x"):
            seen.append(line)
    assert len(seen) == 1
    assert info.value.exit_code == ExitCode.UNKNOWN


def test_stream_read_error_is_disconnect(fake_runner: FakeRunner) -> None:
    class Broken(FakeProcess):
        def read_chunk(self) -> bytes:
            raise OSError("reset")

    process = Broken([])
    remote, _, _ = make_remote(fake_runner, process)
    with pytest.raises(DeviceDisconnected):
        list(remote.stream("follow", "x"))
    assert process.killed


def test_stream_garbled_line(fake_runner: FakeRunner) -> None:
    process = FakeProcess([b"garbage\n", b'{"result": {}}\n'])
    remote, _, _ = make_remote(fake_runner, process)
    with pytest.raises(StreamProtocolError) as info:
        list(remote.stream("follow", "x"))
    assert info.value.exit_code == ExitCode.UNKNOWN
    assert process.killed


def test_stream_closed_early_kills_process(fake_runner: FakeRunner) -> None:
    process = FakeProcess([b'{"t": 1}\n', b'{"t": 2}\n'])
    remote, _, _ = make_remote(fake_runner, process)
    lines = remote.stream("follow", "x")
    next(lines)
    lines.close()
    assert process.killed


# --- ファイルの受け取りと送り ---


class ScpRunner(FakeRunner):
    """scp が呼ばれたら、受け取る側のファイルを今の umask で作る偽物。"""

    def __init__(self, content: bytes = b"backup") -> None:
        super().__init__()
        self.content = content
        self.umasks: list[int] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        input: bytes | None = None,
        timeout: float | None = None,
        check: bool = False,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        result = super().__call__(argv, input=input, timeout=timeout, check=check, cwd=cwd, env=env)
        if argv[0] == "scp" and result.returncode == 0:
            current = os.umask(0)
            os.umask(current)
            self.umasks.append(current)
            fd = os.open(argv[-1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
            with os.fdopen(fd, "wb") as f:
                f.write(self.content)
        return result


SCP = ("scp", *OPTS, "-q", "--")
SCP_SEND = ("scp", *OPTS, "-q", "-p", "--")


def test_fetch_makes_private_files(tmp_path: Path) -> None:
    run = ScpRunner()
    run.script(SCP)
    remote, _, _ = make_remote(run)
    local = tmp_path / "backups" / "20261105-1" / "backup-20261105-1.tar.gz"
    remote_path = "~/.local/share/osekkai/backups/backup-20261105-1.tar.gz"
    before = os.umask(0o022)
    try:
        remote.fetch(remote_path, local)
        after = os.umask(0o022)
    finally:
        os.umask(before)
    assert after == 0o022  # umask を元に戻す
    assert run.umasks == [0o077]
    (call,) = run.calls
    partial = local.with_name(".backup-20261105-1.tar.gz.part")
    assert call.argv == (*SCP, f"{HOST}:{remote_path}", str(partial))
    assert local.read_bytes() == b"backup"
    assert stat.S_IMODE(local.stat().st_mode) == 0o600
    assert stat.S_IMODE(local.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "backups").stat().st_mode) == 0o700
    assert not partial.exists()


def test_fetch_tightens_existing_dir(tmp_path: Path) -> None:
    run = ScpRunner()
    run.script(SCP)
    remote, _, _ = make_remote(run)
    folder = tmp_path / "id"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    remote.fetch("/home/u/b.tar.gz", folder / "b.tar.gz")
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


@pytest.mark.parametrize(("code", "error"), [(255, DeviceUnreachable), (1, TransferFailed)])
def test_fetch_failure_leaves_nothing(tmp_path: Path, code: int, error: type[Exception]) -> None:
    run = ScpRunner()
    run.script(SCP, returncode=code, stderr="scp: /home/secret: No such file")
    remote, _, _ = make_remote(run)
    local = tmp_path / "id" / "b.tar.gz"
    with pytest.raises(error) as info:
        remote.fetch("/home/u/b.tar.gz", local)
    assert "secret" not in str(info.value)
    assert list(local.parent.iterdir()) == []


@pytest.mark.parametrize(
    "path",
    ["a;rm -rf ~", "$(id)", "`id`", "-oProxyCommand=x", "a b", "~/../etc/x", "a'b", "", "~x/y"],
)
def test_bad_remote_paths_are_refused(tmp_path: Path, fake_runner: FakeRunner, path: str) -> None:
    remote, _, _ = make_remote(fake_runner)
    with pytest.raises(ValueError):
        remote.fetch(path, tmp_path / "x")
    with pytest.raises(ValueError):
        remote.send(PACKAGE_DIR / "__init__.py", path)
    assert fake_runner.calls == []


def test_send_preserves_and_sets_0600(tmp_path: Path, fake_runner: FakeRunner) -> None:
    local = tmp_path / "backup-1.tar.gz"
    local.write_bytes(b"x")
    local.chmod(0o600)
    fake_runner.script(SCP_SEND)
    fake_runner.script(SSH)
    remote, _, _ = make_remote(fake_runner)
    remote.send(local, "~/.local/share/osekkai/backups/upload-1.tar.gz")
    assert fake_runner.argvs == [
        (*SCP_SEND, str(local), f"{HOST}:~/.local/share/osekkai/backups/upload-1.tar.gz"),
        (*SSH, "chmod 600 -- ~/.local/share/osekkai/backups/upload-1.tar.gz"),
    ]


def test_send_failures(tmp_path: Path, fake_runner: FakeRunner) -> None:
    local = tmp_path / "b.tar.gz"
    local.write_bytes(b"x")
    remote, _, _ = make_remote(fake_runner)
    with pytest.raises(ValueError):
        remote.send(tmp_path / "missing", "/tmp/x")
    fake_runner.script(SCP_SEND, returncode=255)
    with pytest.raises(DeviceUnreachable):
        remote.send(local, "/tmp/x")
    run = FakeRunner()
    run.script(SCP_SEND)
    run.script(SSH, returncode=1)
    remote, _, _ = make_remote(run)
    with pytest.raises(TransferFailed):
        remote.send(local, "/tmp/x")


# --- 本当の Spawner ---


def test_popen_spawner_reads_output() -> None:
    process = popen_spawner(
        [sys.executable, "-c", "print('{\"ok\": 1}'); raise SystemExit(3)"], False
    )
    data = b""
    while chunk := process.read_chunk():
        data += chunk
    assert data.strip() == b'{"ok": 1}'
    assert process.wait(timeout=10) == 3
