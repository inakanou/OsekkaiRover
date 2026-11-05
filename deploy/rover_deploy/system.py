"""外のコマンドを呼ぶ口（Runner）と、systemctl・journalctl・git の薄い包み。

外のコマンドの標準エラーは、例外の文言にも記録にも写さない（設定の値や名前を含みうるため）。
例外には、コマンドの名前と終了コードだけを入れる。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# 読むだけのコマンドと、サービスを動かすコマンドの時間切れ（秒）
QUERY_TIMEOUT_S = 15.0
CHANGE_TIMEOUT_S = 120.0
JOURNAL_TIMEOUT_S = 30.0

# systemd の状態のうち、プロセスが動いている（動きかけ・止まりかけを含む）もの
RUNNING_STATES = frozenset({"active", "activating", "deactivating", "reloading", "refreshing"})

SYSTEMCTL = ("systemctl", "--user")
UNIT_PROPERTIES = ("MainPID", "NRestarts", "ActiveState")


def _program(argv: Sequence[str]) -> str:
    return Path(argv[0]).name if argv else ""


class CommandError(Exception):
    """外のコマンドの失敗。文言にはコマンドの名前だけを入れる。"""

    def __init__(self, program: str, message: str) -> None:
        super().__init__(message)
        self.program = program


class CommandFailed(CommandError):
    """コマンドが 0 でない終了コードで終わった。"""

    def __init__(self, program: str, returncode: int) -> None:
        super().__init__(program, f"{program} が失敗しました（終了コード {returncode}）")
        self.returncode = returncode


class CommandTimedOut(CommandError):
    def __init__(self, program: str) -> None:
        super().__init__(program, f"{program} が時間内に終わりませんでした")


class CommandNotFound(CommandError):
    def __init__(self, program: str) -> None:
        super().__init__(program, f"{program} が見つかりません")


class UnexpectedOutput(CommandError):
    """コマンドの出力が思った形でない。"""

    def __init__(self, program: str) -> None:
        super().__init__(program, f"{program} の出力の形が違います")


@dataclass(frozen=True)
class RunResult:
    """コマンドの結果。出力は表示（repr）に出さない。"""

    returncode: int
    stdout: bytes = field(default=b"", repr=False)
    stderr: bytes = field(default=b"", repr=False)

    @property
    def text(self) -> str:
        """標準出力を文字にする。読めない文字は置き換える。"""
        return self.stdout.decode("utf-8", errors="replace")


class Runner(Protocol):
    """外のコマンドを呼ぶ口。テストでは偽物に差し替える。"""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        input: bytes | None = None,
        timeout: float | None = None,
        check: bool = False,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult: ...


def check_result(argv: Sequence[str], result: RunResult, *, check: bool) -> RunResult:
    """`check` で 0 でない終了コードなら `CommandFailed` にする。"""
    if check and result.returncode != 0:
        raise CommandFailed(_program(argv), result.returncode)
    return result


class SubprocessRunner:
    """subprocess で本当にコマンドを呼ぶ Runner。

    `env` は今の環境変数に足す。入力がなければ標準入力を空にする（入力を待って止まらない）。
    """

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
        args = list(argv)
        program = _program(args)
        full_env = None if env is None else {**os.environ, **env}
        try:
            completed = subprocess.run(
                args,
                input=input,
                stdin=subprocess.DEVNULL if input is None else None,
                capture_output=True,
                timeout=timeout,
                cwd=cwd,
                env=full_env,
                check=False,
            )
        except FileNotFoundError:
            raise CommandNotFound(program) from None
        except subprocess.TimeoutExpired:
            raise CommandTimedOut(program) from None
        result = RunResult(completed.returncode, completed.stdout, completed.stderr)
        return check_result(args, result, check=check)


# --- systemctl --user ---


@dataclass(frozen=True)
class UnitInfo:
    main_pid: int | None  # 動いていなければ None
    n_restarts: int
    active_state: str


def is_active(run: Runner, unit: str) -> bool:
    """ユニットが動いているか。分からなければ `UnexpectedOutput`（止まっているとみなさない）。"""
    argv = [*SYSTEMCTL, "is-active", unit]
    state = run(argv, timeout=QUERY_TIMEOUT_S).text.strip()
    if not state:
        raise UnexpectedOutput("systemctl")
    return state in RUNNING_STATES


def show_unit(run: Runner, unit: str) -> UnitInfo:
    """ユニットの MainPID・再起動の回数・状態を読む。"""
    argv = [*SYSTEMCTL, "show", f"--property={','.join(UNIT_PROPERTIES)}", unit]
    text = run(argv, timeout=QUERY_TIMEOUT_S, check=True).text
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value
    try:
        main_pid = int(values["MainPID"])
        n_restarts = int(values["NRestarts"])
        active_state = values["ActiveState"]
    except (KeyError, ValueError):
        raise UnexpectedOutput("systemctl") from None
    return UnitInfo(main_pid or None, n_restarts, active_state)


def _unit_command(run: Runner, verb: str, unit: str) -> None:
    run([*SYSTEMCTL, verb, unit], timeout=CHANGE_TIMEOUT_S, check=True)


def start_unit(run: Runner, unit: str) -> None:
    _unit_command(run, "start", unit)


def stop_unit(run: Runner, unit: str) -> None:
    _unit_command(run, "stop", unit)


def enable_unit(run: Runner, unit: str) -> None:
    _unit_command(run, "enable", unit)


def disable_unit(run: Runner, unit: str) -> None:
    _unit_command(run, "disable", unit)


def daemon_reload(run: Runner) -> None:
    run([*SYSTEMCTL, "daemon-reload"], timeout=CHANGE_TIMEOUT_S, check=True)


# --- journalctl --user ---


def journal_since(run: Runner, unit: str, since: str) -> list[str]:
    """ユニットの、`since`（journalctl の時刻の書き方）以降のログの文を、行の一覧で返す。"""
    argv = [
        "journalctl",
        "--user",
        f"_SYSTEMD_USER_UNIT={unit}",
        "--since",
        since,
        "-o",
        "cat",
        "--no-pager",
        "-q",
    ]
    text = run(argv, timeout=JOURNAL_TIMEOUT_S, check=True).text
    return text.splitlines()


# --- git ---


def git_describe(run: Runner, repo: Path) -> str:
    """作業コピーの版の名前（`git describe --tags --always --dirty`）。"""
    argv = ["git", "-C", str(repo), "describe", "--tags", "--always", "--dirty"]
    name = run(argv, timeout=QUERY_TIMEOUT_S, check=True).text.strip()
    if not name:
        raise UnexpectedOutput("git")
    return name


def git_dirty_count(run: Runner, repo: Path) -> int:
    """作業コピーの手の変更の件数。ファイルの名前は返さない。"""
    argv = ["git", "-C", str(repo), "status", "--porcelain"]
    text = run(argv, timeout=QUERY_TIMEOUT_S, check=True).text
    return sum(1 for line in text.splitlines() if line.strip())
