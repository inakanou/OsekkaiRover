"""開発 PC: ssh・scp で実機とやり取りする（1.6、3.3、3.4、4.5）。

- 道具（rover_deploy のパッケージ）を tar にして送り、実機の `~/osekkai-rover/tool/` に展開する
- 実機のサブコマンドを呼び、標準出力の JSON を読む。`follow` の JSON の行を流す
- ファイルを受け取る（0700 のディレクトリに 0600 で）・送る

ssh は端末を割り当てず（`-T`）、`-o BatchMode=yes -o ConnectTimeout=10` で呼ぶ。
初期設定（sudo のパスワードを利用者が打つ）だけ `-t` で端末を割り当てる。
ssh と scp の標準エラーは、表示にも例外の文言にも写さない（設定の値や名前を含みうるため）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
from collections.abc import Callable, Generator, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rover_deploy.common import ExitCode
from rover_deploy.system import CommandNotFound, CommandTimedOut, Runner

SSH_UNREACHABLE = 255  # ssh が接続できない・切れたときの終了コード
USAGE_ERROR = 2  # 実機の道具の使い方の誤り

CONNECT_TIMEOUT_S = 10
SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",  # パスワードやホスト鍵を端末で聞いて止まらない
    "-o",
    f"ConnectTimeout={CONNECT_TIMEOUT_S}",
    "-o",
    "ServerAliveInterval=15",  # 途中で切れた接続を見つける（4.5）
    "-o",
    "ServerAliveCountMax=3",
)

PUSH_TIMEOUT_S = 120.0
AGENT_TIMEOUT_S = 600.0  # バックアップを作る間も待てる長さ
TRANSFER_TIMEOUT_S = 1800.0
STREAM_EXIT_WAIT_S = 10.0  # 結果の行の後、ssh が終わるのを待つ秒数
READ_CHUNK = 65536

# 実機の置き場所。~ は実機のシェルが広げるので、引用しない
REMOTE_TOOL_DIR = "~/osekkai-rover/tool"
REMOTE_PYTHON = "/usr/bin/python3"
AGENT_MODULE = "rover_deploy.agent"
PACKAGE_NAME = "rover_deploy"

# 道具を入れ替える実機のコマンド。利用者の値は入れない（決まった文）。
# 別のディレクトリに展開してから、名前の付け替えで入れ替える
PUSH_COMMAND = (
    "set -e; umask 077; "
    f"mkdir -p {REMOTE_TOOL_DIR}; chmod 700 {REMOTE_TOOL_DIR}; cd {REMOTE_TOOL_DIR}; "
    f"rm -rf .incoming {PACKAGE_NAME}.old; mkdir .incoming; "
    "tar -xzf - -C .incoming; "
    f"test -d .incoming/{PACKAGE_NAME}; "
    f"if [ -e {PACKAGE_NAME} ]; then mv {PACKAGE_NAME} {PACKAGE_NAME}.old; fi; "
    f"mv .incoming/{PACKAGE_NAME} {PACKAGE_NAME}; "
    f"rm -rf {PACKAGE_NAME}.old .incoming"
)

# scp の実機の側の置き場所に使える文字。scp は版によって実機のシェルを通すかが違うので、
# 引用に頼らず、シェルで意味を持つ文字を断る。先頭の ~/ は家のディレクトリ
_REMOTE_PATH_RE = re.compile(r"(?:~/)?[A-Za-z0-9._/][A-Za-z0-9._/-]*")

# follow の結果の行（これがあれば流れは終わり）のキー
_TERMINAL_KEYS = frozenset({"result", "error", "ok"})


class RemoteError(Exception):
    """実機とのやり取りの失敗。文言は決まった文だけ。"""

    exit_code: int = ExitCode.ABORTED


class DeviceUnreachable(RemoteError):
    """実機に接続できない（ssh の終了コード 255 か、接続の時間切れ）（1.6）。

    `refs.RemoteUnreachable`（リポジトリに届かない）とは別。
    """

    def __init__(self) -> None:
        super().__init__(
            "実機に接続できません。接続先と、実機の電源とネットワークを確かめてください"
        )


class DeviceDisconnected(RemoteError):
    """流している途中で接続が切れ、結果が分からない（4.5）。終了コードは 13。"""

    exit_code = ExitCode.UNKNOWN

    def __init__(self) -> None:
        super().__init__("実機との接続が途中で切れました。結果は分かりません")


class RemoteTimedOut(RemoteError):
    def __init__(self) -> None:
        super().__init__("実機の操作が時間内に終わりませんでした")


class AgentProtocolError(RemoteError):
    """実機の道具の答えが JSON の形でない。"""

    def __init__(self) -> None:
        super().__init__("実機の道具の答えの形が違います")


class StreamProtocolError(AgentProtocolError):
    """流している途中の行が JSON の形でない。結果が分からないので、終了コードは 13。"""

    exit_code = ExitCode.UNKNOWN


class TransferFailed(RemoteError):
    """ファイルを受け取れない・送れない。"""

    def __init__(self) -> None:
        super().__init__("実機とのファイルのやり取りに失敗しました")


@dataclass(frozen=True)
class AgentReply:
    """実機のサブコマンドの答え。`data` は使い方の誤り（2）で JSON がないときだけ None。"""

    exit_code: int
    data: dict[str, object] | None


class StreamProcess(Protocol):
    """出力を少しずつ読める、動いているコマンド。"""

    def read_chunk(self) -> bytes: ...  # 終わりなら b""

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


# (argv, interactive) -> StreamProcess。interactive なら標準入力を利用者の端末につなぐ
Spawner = Callable[[Sequence[str], bool], StreamProcess]


class _PopenProcess:
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc

    def read_chunk(self) -> bytes:
        stdout = self._proc.stdout
        if stdout is None:
            return b""
        return os.read(stdout.fileno(), READ_CHUNK)

    def wait(self, timeout: float | None = None) -> int:
        try:
            return self._proc.wait(timeout=timeout)
        finally:
            if self._proc.returncode is not None and self._proc.stdout is not None:
                self._proc.stdout.close()

    def kill(self) -> None:
        self._proc.kill()


def popen_spawner(argv: Sequence[str], interactive: bool) -> StreamProcess:
    """subprocess で本当に起こす Spawner。標準エラーは捨てる。"""
    args = list(argv)
    try:
        proc = subprocess.Popen(
            args,
            stdin=None if interactive else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise CommandNotFound(Path(args[0]).name if args else "") from None
    return _PopenProcess(proc)


def _write_stdout(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.flush()


class _TtyEcho:
    """端末を割り当てた ssh の出力を、利用者に見せる。

    sudo の問いは改行がないので、届いたらすぐ見せる。`{` で始まる行（道具の JSON）は見せない
    （表示は devcli が作る）。
    """

    def __init__(self, write: Callable[[bytes], None]) -> None:
        self._write = write
        self._line_start = True
        self._hide = False

    def feed(self, chunk: bytes) -> None:
        out = bytearray()
        for c in chunk:
            if self._line_start:
                self._hide = c == ord("{")
                self._line_start = False
            if not self._hide:
                out.append(c)
            if c == ord("\n"):
                self._line_start = True
        if out:
            self._write(bytes(out))


def _parse_object(text: str) -> dict[str, object] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _last_line(output: bytes) -> str:
    """端末の出力（行の終わりが \\r\\n）の、空でない最後の行。"""
    lines = output.decode("utf-8", errors="replace").split("\n")
    for line in reversed(lines):
        stripped = line.strip("\r").strip()
        if stripped:
            return stripped
    return ""


def _filtered(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts
    if "__pycache__" in parts or info.name.endswith((".pyc", ".pyo")):
        return None
    if not (info.isfile() or info.isdir()):
        return None
    # 開発 PC の利用者の名前を写さない
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def build_tool_archive(package_dir: Path) -> bytes:
    """道具のパッケージを tar.gz にする。中の名前は `rover_deploy/...`。"""
    if not (package_dir / "__init__.py").is_file():
        raise ValueError("道具のパッケージのディレクトリではありません")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(str(package_dir), arcname=PACKAGE_NAME, recursive=False, filter=_filtered)
        for path in sorted(package_dir.rglob("*")):
            arcname = f"{PACKAGE_NAME}/{path.relative_to(package_dir).as_posix()}"
            tar.add(str(path), arcname=arcname, recursive=False, filter=_filtered)
    return buffer.getvalue()


def agent_command(args: Sequence[str]) -> str:
    """実機のシェルに渡す、道具を呼ぶ文。引数は 1 つずつ引用する（JSON の空白や引用符を守る）。"""
    quoted = " ".join(shlex.quote(arg) for arg in args)
    return f"cd {REMOTE_TOOL_DIR} && {REMOTE_PYTHON} -m {AGENT_MODULE} {quoted}".rstrip()


def check_remote_path(remote_path: str) -> str:
    """scp に渡す実機の置き場所を確かめる。シェルで意味を持つ文字があれば断る。"""
    if not _REMOTE_PATH_RE.fullmatch(remote_path) or ".." in Path(remote_path).parts:
        raise ValueError("実機の置き場所に使えない文字があります")
    return remote_path


def _shell_path(remote_path: str) -> str:
    """実機のシェルに渡す置き場所。先頭の ~/ は広げさせ、残りは引用する。"""
    if remote_path.startswith("~/"):
        return "~/" + shlex.quote(remote_path[2:])
    return shlex.quote(remote_path)


@contextlib.contextmanager
def _private_umask() -> Iterator[None]:
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


class Remote:
    """ssh で実機（`host`）とやり取りする。"""

    def __init__(
        self,
        host: str,
        run: Runner,
        *,
        spawn: Spawner = popen_spawner,
        echo: Callable[[bytes], None] = _write_stdout,
    ) -> None:
        if not host or host.startswith("-"):
            raise ValueError("接続先の形が違います")
        self.host = host
        self._run = run
        self._spawn = spawn
        self._echo = echo

    # --- 引数 ---

    def ssh_argv(self, command: str, *, tty: bool = False) -> list[str]:
        return ["ssh", *SSH_OPTIONS, "-t" if tty else "-T", "--", self.host, command]

    def scp_argv(self, source: str, dest: str, *, preserve: bool = False) -> list[str]:
        flags = ["-q", "-p"] if preserve else ["-q"]
        return ["scp", *SSH_OPTIONS, *flags, "--", source, dest]

    def _ssh(self, command: str, *, input: bytes | None, timeout: float) -> int:
        argv = self.ssh_argv(command)
        try:
            result = self._run(argv, input=input, timeout=timeout)
        except CommandTimedOut:
            raise RemoteTimedOut() from None
        if result.returncode == SSH_UNREACHABLE:
            raise DeviceUnreachable()
        return result.returncode

    # --- 道具を写す ---

    def push_tool(self, package_dir: Path) -> None:
        """道具を tar で送り、実機の `~/osekkai-rover/tool/rover_deploy` を入れ替える。"""
        archive = build_tool_archive(package_dir)
        if self._ssh(PUSH_COMMAND, input=archive, timeout=PUSH_TIMEOUT_S) != 0:
            raise TransferFailed()

    # --- 実機のサブコマンド ---

    def agent(self, *args: str, tty: bool = False, timeout: float = AGENT_TIMEOUT_S) -> AgentReply:
        """`python3 -m rover_deploy.agent <args>` を呼び、JSON を読む。

        `tty` なら端末を割り当てる（初期設定の sudo のため）。このときは利用者が端末で答えられる
        ようにし、出力の最後の行を JSON として読む（標準エラーと混ざり、行の終わりが \\r\\n）。
        """
        command = agent_command(args)
        if tty:
            code, output = self._interactive(self.ssh_argv(command, tty=True))
            text = _last_line(output)
        else:
            argv = self.ssh_argv(command)
            try:
                result = self._run(argv, timeout=timeout)
            except CommandTimedOut:
                raise RemoteTimedOut() from None
            code, text = result.returncode, result.text.strip()
        if code == SSH_UNREACHABLE:
            raise DeviceUnreachable()
        data = _parse_object(text)
        if data is None:
            if code == USAGE_ERROR:
                return AgentReply(code, None)
            raise AgentProtocolError()
        return AgentReply(code, data)

    def _interactive(self, argv: Sequence[str]) -> tuple[int, bytes]:
        proc = self._spawn(argv, True)
        echo = _TtyEcho(self._echo)
        output = bytearray()
        try:
            while chunk := proc.read_chunk():
                output.extend(chunk)
                echo.feed(chunk)
            return proc.wait(), bytes(output)
        except BaseException:
            proc.kill()
            raise

    def stream(self, *args: str) -> Generator[dict[str, object], None, None]:
        """道具の JSON の行を、届くたびに返す（follow）。

        結果の行（result・error・ok のキーを持つ行）で終わる。その前に ssh が終われば
        `DeviceDisconnected`（終了コード 13 に写す）。
        """
        proc = self._spawn(self.ssh_argv(agent_command(args)), False)
        finished = False
        try:
            pending = b""
            while True:
                try:
                    chunk = proc.read_chunk()
                except OSError:
                    raise DeviceDisconnected() from None
                if not chunk:
                    break
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    line = raw.decode("utf-8", errors="replace").strip("\r").strip()
                    if not line:
                        continue
                    data = _parse_object(line)
                    if data is None:
                        raise StreamProtocolError()
                    yield data
                    if _TERMINAL_KEYS & data.keys():
                        finished = True
                        with contextlib.suppress(Exception):
                            proc.wait(timeout=STREAM_EXIT_WAIT_S)
                        return
            finished = True
            with contextlib.suppress(Exception):
                proc.wait(timeout=STREAM_EXIT_WAIT_S)
            raise DeviceDisconnected()
        finally:
            if not finished:
                with contextlib.suppress(Exception):
                    proc.kill()
                    proc.wait(timeout=STREAM_EXIT_WAIT_S)

    # --- ファイル ---

    def fetch(self, remote_path: str, local_path: Path) -> None:
        """実機のファイルを受け取る。置き場所のディレクトリは 0700、ファイルは 0600（3.4）。"""
        source = f"{self.host}:{check_remote_path(remote_path)}"
        partial = local_path.with_name(f".{local_path.name}.part")
        with _private_umask():
            local_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            local_path.parent.chmod(0o700)
            try:
                self._transfer(self.scp_argv(source, str(partial)))
                partial.chmod(0o600)
                os.replace(partial, local_path)
            finally:
                partial.unlink(missing_ok=True)

    def send(self, local_path: Path, remote_path: str) -> None:
        """開発 PC のファイルを実機に送り、実機の側を 0600 にする（3.4）。"""
        dest = check_remote_path(remote_path)
        if not local_path.is_file():
            raise ValueError("送るファイルがありません")
        self._transfer(self.scp_argv(str(local_path), f"{self.host}:{dest}", preserve=True))
        if self._ssh(f"chmod 600 -- {_shell_path(dest)}", input=None, timeout=PUSH_TIMEOUT_S):
            raise TransferFailed()

    def _transfer(self, argv: Sequence[str]) -> None:
        try:
            result = self._run(argv, timeout=TRANSFER_TIMEOUT_S)
        except CommandTimedOut:
            raise RemoteTimedOut() from None
        if result.returncode == SSH_UNREACHABLE:
            raise DeviceUnreachable()
        if result.returncode != 0:
            raise TransferFailed()
