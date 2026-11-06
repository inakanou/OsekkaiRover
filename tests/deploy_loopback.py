"""開発 PC の道具（devcli）から実機の道具（agent）までを、同じプロセスでつなぐ偽物。

device-deployment の結合テスト（tests/integration/test_deploy_end_to_end.py）で使う。
`from deploy_loopback import ...` で読む（tests/ が import の道にある）。

- `LoopbackDevice`: 一時ディレクトリの家を持つ偽の実機。systemctl・journalctl・git・
  systemd-run・self-check のコマンドを、ユニットの様子を持って答える Runner でもある。
  `systemd-run ... run-apply` は、同じ文脈で実機の道具の `run-apply` をその場で最後まで行う
- `loopback_remote`: 本物の `remote.Remote` に、ssh と scp を実機の道具の呼び出しと
  ファイルの写しに置き換えた口を渡す（引数の引用と JSON の読み取りは本物のまま）

本物の ssh・systemd・ネットワークは使わない。
"""

from __future__ import annotations

import io
import shlex
import shutil
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from osekkai.core.runtime import VERSION_LOG_PREFIX
from osekkai.core.status import STATUS_FILE_NAME, ServiceSnapshot, write_snapshot
from rover_deploy import agent
from rover_deploy.agent import AgentContext, TransactionPort
from rover_deploy.common import Target
from rover_deploy.layout import CORE_UNIT_NAME, SAMPLE_UNIT_NAME, Layout
from rover_deploy.releases import READY_MARKER, UNIT_SOURCE, Releases
from rover_deploy.remote import (
    AGENT_MODULE,
    PACKAGE_NAME,
    PUSH_COMMAND,
    REMOTE_PYTHON,
    REMOTE_TOOL_DIR,
    SSH_UNREACHABLE,
    Remote,
)
from rover_deploy.service_status import SafetyVerdict, check_safety
from rover_deploy.system import SYSTEMCTL, Runner, RunResult, check_result
from rover_deploy.transaction import EXAMPLE_CONFIG, Transaction
from shared_fixtures import FIXTURES_DIR

HOST = "rover"  # ~/.ssh/config の別名の例（本物の接続先は書かない）
EPOCH = 1_793_000_000.0  # 2026-10-26T07:33:20Z
MONOTONIC_START = 5_000.0
FIRST_PID = 4_000
REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_UNIT = agent.DEPLOY_UNIT  # systemd-run の --unit= と同じ名前（.service なし）


class DeviceClock:
    """実機の単調な時計と壁時計。`sleep` で進み、そのたびに基盤の心拍を書き直す。"""

    def __init__(self) -> None:
        self.t = MONOTONIC_START
        self._device: LoopbackDevice | None = None

    def bind(self, device: LoopbackDevice) -> None:
        self._device = device

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.t += seconds
        if self._device is not None:
            self._device.heartbeat()

    def now(self) -> float:
        return EPOCH + (self.t - MONOTONIC_START)


@dataclass
class Unit:
    active: bool = False
    main_pid: int = 0
    n_restarts: int = 0
    enabled: bool = False


class FakePrepareReleases(Releases):
    """`prepare` だけを偽物にした `Releases`。git と pip を使わずに版のディレクトリを作る。

    切り替え・ユニットの写し・片付け・手の変更の数え方は本物のまま。
    """

    def __init__(self, layout: Layout, run: Runner) -> None:
        super().__init__(layout, run)
        self.layout = layout
        self.prepared: list[str] = []

    def prepare(self, target: Target, repo_url: str) -> Path:
        assert target.sha is not None and repo_url.startswith("https://")
        directory = self.layout.release_dir(target.sha)
        venv = directory / ".venv"
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "bin/python").write_text("", encoding="utf-8")
        (venv / READY_MARKER).write_text(target.sha + "\n", encoding="utf-8")
        (directory / ".git").write_text("gitdir: ../../repo.git/worktrees/x\n", encoding="utf-8")
        for relative in (UNIT_SOURCE, EXAMPLE_CONFIG):
            (directory / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / relative, directory / relative)
        self.prepared.append(target.sha)
        return directory


class LoopbackDevice:
    """一時ディレクトリの家を持つ偽の実機。自分が Runner でもある。"""

    def __init__(self, root: Path, *, sample_running: bool = False) -> None:
        self.home = root / "device-home"
        self.home.mkdir(parents=True)
        self.env: dict[str, str] = {"XDG_RUNTIME_DIR": str(root / "device-run")}
        self.layout = Layout(self.env, home=self.home, uid=1000)
        self.clock = DeviceClock()
        self.clock.bind(self)
        self.units: dict[str, Unit] = {
            CORE_UNIT_NAME: Unit(),
            SAMPLE_UNIT_NAME: Unit(enabled=True),
            DEPLOY_UNIT: Unit(),
        }
        self._next_pid = FIRST_PID
        self.journal: list[tuple[float, str]] = []
        self.describe = "v0.1.0-3-gaaaaaaa"  # 入れた版の git describe
        self.self_check_fixture = "selfcheck_ok.json"
        self.calls: list[tuple[str, ...]] = []
        self.unexpected: list[tuple[str, ...]] = []
        self.run_apply_codes: list[int] = []
        self.releases: list[FakePrepareReleases] = []
        self._make_sample()
        if sample_running:
            self._start(SAMPLE_UNIT_NAME)

    # --- 置き場所 ---

    def _make_sample(self) -> None:
        """サンプルの作業コピーとユニットと、基盤の設定（バックアップの中身）を置く。"""
        sample = self.layout.sample_dir
        (sample / ".git").mkdir(parents=True)
        (sample / "data").mkdir()
        (sample / "data/memo.txt").write_text("sample data\n", encoding="utf-8")
        unit_dir = self.layout.user_unit_dir
        unit_dir.mkdir(parents=True)
        (unit_dir / SAMPLE_UNIT_NAME).write_text("[Service]\n", encoding="utf-8")
        config_dir = self.layout.core_config_dir
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text("schema: 1\n", encoding="utf-8")

    @property
    def status_file(self) -> Path:
        return self.layout.runtime_dir / STATUS_FILE_NAME

    @property
    def notice_file(self) -> Path:
        return self.layout.runtime_dir / "notice.json"

    # --- 基盤のサービスのふり ---

    def _start(self, name: str) -> None:
        unit = self.units[name]
        if unit.active:
            return
        unit.active = True
        unit.main_pid = self._next_pid
        self._next_pid += 1
        if name == CORE_UNIT_NAME:
            line = f"INFO osekkai.core.runtime: {VERSION_LOG_PREFIX}{self.describe}"
            self.journal.append((self.clock.now(), line))
            self.heartbeat()

    def _stop(self, name: str) -> None:
        unit = self.units[name]
        unit.active = False
        unit.main_pid = 0
        if name == CORE_UNIT_NAME:
            # 基盤は停止の最初に状態のファイルを消す
            self.status_file.unlink(missing_ok=True)

    def heartbeat(self) -> None:
        """基盤が動いていれば、基盤の型で状態のファイルを書き直す（毎秒の報告のふり）。"""
        core = self.units[CORE_UNIT_NAME]
        if not core.active:
            return
        snapshot = ServiceSnapshot(
            pid=core.main_pid,
            version=self.describe,
            heartbeat=self.clock.monotonic(),
            driving=False,
            busy={},
            unavailable=(),
            features=("demo",),
        )
        write_snapshot(self.status_file, snapshot)

    def running_units(self) -> set[str]:
        return {name for name, unit in self.units.items() if unit.active}

    # --- 実機の道具の文脈 ---

    def check_safety(self, run: Runner, layout: Layout) -> SafetyVerdict:
        # 心拍と同じ単調な時計で比べる
        return check_safety(run, layout, now_monotonic=self.clock.monotonic())

    def _transaction(self, ctx: AgentContext, repo_url: str) -> TransactionPort:
        releases = FakePrepareReleases(ctx.layout, ctx.run)
        self.releases.append(releases)
        return Transaction(
            ctx.layout,
            ctx.run,
            repo_url=repo_url,
            releases=releases,
            clock=ctx.clock,
            now=ctx.now,
            check_safety=self.check_safety,
        )

    def context(self, out: io.StringIO) -> AgentContext:
        return AgentContext(
            layout=self.layout,
            env=self.env,
            run=self,
            out=out,
            now=self.clock.now,
            clock=self.clock,
            free_bytes=lambda path: 64 * 1024**3,
            check_safety=self.check_safety,
            transaction=self._transaction,
        )

    def agent(self, args: Sequence[str]) -> tuple[int, str]:
        """実機の道具を同じプロセスで呼び、終了コードと標準出力を返す。"""
        out = io.StringIO()
        code = agent.main(list(args), context=self.context(out))
        return code, out.getvalue()

    # --- Runner ---

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
        args = tuple(argv)
        self.calls.append(args)
        result = self._answer(args, cwd)
        if result is None:
            self.unexpected.append(args)
            raise AssertionError("台本にないコマンド")
        return check_result(args, result, check=check)

    def _answer(self, args: tuple[str, ...], cwd: Path | None) -> RunResult | None:
        n = len(SYSTEMCTL)
        if args[:n] == SYSTEMCTL:
            return self._systemctl(args[n:])
        if args and args[0] == "systemd-run":
            return self._systemd_run(args)
        if args and args[0] == "journalctl":
            return self._journalctl(args)
        if args[:1] == ("git",) and len(args) >= 4 and args[1] == "-C":
            return self._git(Path(args[2]), args[3:])
        if args[1:] == ("-m", "osekkai", "--self-check", "--json"):
            release = Path(args[0]).parents[2]
            assert cwd == release, "self-check は版のディレクトリで行う"
            text = (FIXTURES_DIR / self.self_check_fixture).read_text(encoding="utf-8")
            failed = '"failed"' in text
            return RunResult(1 if failed else 0, text.encode("utf-8"), b"")
        return None

    def _systemctl(self, rest: tuple[str, ...]) -> RunResult | None:
        if rest == ("daemon-reload",):
            return RunResult(0, b"", b"")
        if len(rest) == 2 and rest[1] in self.units:
            verb, name = rest
            unit = self.units[name]
            if verb == "is-active":
                text = "active\n" if unit.active else "inactive\n"
                return RunResult(0 if unit.active else 3, text.encode(), b"")
            if verb == "start":
                self._start(name)
            elif verb == "stop":
                self._stop(name)
            elif verb in ("enable", "disable"):
                unit.enabled = verb == "enable"
            else:
                return None
            return RunResult(0, b"", b"")
        if len(rest) == 3 and rest[0] == "show" and rest[2] in self.units:
            unit = self.units[rest[2]]
            text = (
                f"MainPID={unit.main_pid}\nNRestarts={unit.n_restarts}\n"
                f"ActiveState={'active' if unit.active else 'inactive'}\n"
            )
            return RunResult(0, text.encode(), b"")
        return None

    def _systemd_run(self, args: tuple[str, ...]) -> RunResult | None:
        """切り離した適用を、その場で最後まで行う（ssh が切れても実機の側で進むふり）。"""
        if f"--unit={agent.DEPLOY_UNIT}" not in args or "--property=KillMode=process" not in args:
            return None
        start = args.index(agent.AGENT_MODULE) + 1
        if args[start] != "run-apply":
            return None
        unit = self.units[DEPLOY_UNIT]
        unit.active = True
        try:
            code, _ = self.agent(args[start:])
        finally:
            unit.active = False
        self.run_apply_codes.append(code)
        return RunResult(0, b"", b"")

    def _journalctl(self, args: tuple[str, ...]) -> RunResult | None:
        if f"--user-unit={CORE_UNIT_NAME}" not in args or "--since" not in args:
            return None
        since = float(args[args.index("--since") + 1].removeprefix("@"))
        lines = [line for t, line in self.journal if t >= since]
        return RunResult(0, "".join(f"{line}\n" for line in lines).encode("utf-8"), b"")

    def _git(self, repo: Path, rest: tuple[str, ...]) -> RunResult | None:
        if not repo.is_relative_to(self.layout.releases_dir):
            return None
        if rest == ("status", "--porcelain"):
            return RunResult(0, b"", b"")
        if rest == ("describe", "--tags", "--always", "--dirty"):
            return RunResult(0, f"{self.describe}\n".encode(), b"")
        return None


# --- ssh と scp を実機の道具につなぐ ---


@dataclass
class Loopback:
    """ssh・scp の代わり。`drop_stream_after` 行で follow の接続を切る。"""

    device: LoopbackDevice
    drop_stream_after: int | None = None
    pushed: list[set[str]] = field(default_factory=list)
    agent_calls: list[tuple[str, ...]] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)

    def _agent_args(self, command: str) -> tuple[str, ...]:
        prefix = f"cd {REMOTE_TOOL_DIR} && {REMOTE_PYTHON} -m {AGENT_MODULE}"
        assert command.startswith(prefix), "道具を呼ぶ文の形が違う"
        # 実機のシェルと同じく、引用を外して引数に分ける
        return tuple(shlex.split(command.removeprefix(prefix)))

    def _ssh_command(self, argv: Sequence[str]) -> str:
        assert argv[0] == "ssh" and argv[-2] == HOST, "接続先は別名だけ"
        return argv[-1]

    def run(
        self,
        argv: Sequence[str],
        *,
        input: bytes | None = None,
        timeout: float | None = None,
        check: bool = False,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        if argv[0] == "scp":
            return self._scp(argv)
        command = self._ssh_command(argv)
        if command == PUSH_COMMAND:
            assert input is not None
            with tarfile.open(fileobj=io.BytesIO(input), mode="r:gz") as tar:
                self.pushed.append(set(tar.getnames()))
            return RunResult(0, b"", b"")
        args = self._agent_args(command)
        self.agent_calls.append(args)
        code, text = self.device.agent(args)
        return RunResult(code, text.encode("utf-8"), b"")

    def _scp(self, argv: Sequence[str]) -> RunResult:
        source, dest = argv[-2], argv[-1]
        host, sep, remote_path = source.partition(":")
        if not sep or host != HOST:
            return RunResult(1, b"", b"")
        path = Path(remote_path)
        if not path.is_absolute() or not path.is_relative_to(self.device.home):
            return RunResult(1, b"", b"")
        if not path.is_file():
            return RunResult(1, b"", b"")
        shutil.copyfile(path, dest)
        self.fetched.append(path.name)
        return RunResult(0, b"", b"")

    def spawn(self, argv: Sequence[str], interactive: bool) -> _Stream:
        assert not interactive
        args = self._agent_args(self._ssh_command(argv))
        self.agent_calls.append(args)
        _, text = self.device.agent(args)
        lines = text.splitlines(keepends=True)
        if self.drop_stream_after is not None:
            lines = lines[: self.drop_stream_after]
            return _Stream("".join(lines).encode("utf-8"), SSH_UNREACHABLE)
        return _Stream("".join(lines).encode("utf-8"), 0)


class _Stream:
    """少しずつ読める出力。小さく区切って返し、行の途中で切れる場合も通す。"""

    CHUNK = 7

    def __init__(self, data: bytes, code: int) -> None:
        self._data = data
        self._code = code

    def read_chunk(self) -> bytes:
        chunk, self._data = self._data[: self.CHUNK], self._data[self.CHUNK :]
        return chunk

    def wait(self, timeout: float | None = None) -> int:
        return self._code

    def kill(self) -> None:
        self._data = b""


def loopback_remote(loopback: Loopback) -> Remote:
    """本物の Remote に、偽の ssh・scp をつないで返す。"""
    return Remote(HOST, loopback.run, spawn=loopback.spawn, echo=lambda data: None)


def pushed_package_ok(names: set[str]) -> bool:
    return f"{PACKAGE_NAME}/agent.py" in names and f"{PACKAGE_NAME}/devcli.py" in names
