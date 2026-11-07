"""開発 PC の入口（devcli）のテストで共通に使う偽物と準備。

`from devcli_fakes import ...` で読む（tests/deploy が import の道にある）。
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from fake_runner import FakeRunner

from rover_deploy import devcli
from rover_deploy.common import Target
from rover_deploy.devconfig import ConfigError, DevConfig
from rover_deploy.refs import Resolution
from rover_deploy.remote import AgentReply
from rover_deploy.system import Runner

HOST = "hidden-host-name"
REPO_URL = "https://example.invalid/owner/hidden-repo.git"
SHA = "a" * 40
NOW = 1_793_000_000.0  # 2026-10-26T07:33:20Z
DEPLOY_ID = "20261026T073320Z"
TAG = Target(kind="release", ref="v0.2.0", ref_kind="tag", sha=SHA)
SAMPLE = Target(kind="sample", ref=None, ref_kind=None, sha=None)

Reply = AgentReply | BaseException
TAG_ONLY = Resolution(TAG, also_branch=False)


def ok(**data: object) -> AgentReply:
    return AgentReply(0, {"ok": True, "reason": "", **data})


def ng(reason: str, **data: object) -> AgentReply:
    return AgentReply(0, {"ok": False, "reason": reason, "reasons": [reason], **data})


@dataclass
class FakeRemote:
    """呼ばれた操作を記録し、決めた答えを返す偽の Remote。"""

    replies: dict[str, list[Reply]] = field(default_factory=dict)
    lines: list[dict[str, object] | BaseException] = field(default_factory=list)
    push_error: BaseException | None = None
    fetch_error: BaseException | None = None
    send_error: BaseException | None = None
    calls: list[tuple[object, ...]] = field(default_factory=list)
    ttys: list[bool] = field(default_factory=list)  # agent の呼び出しごとの tty
    out: io.StringIO | None = None  # 呼ばれたときの表示の長さを記録する

    def _mark(self, *call: object) -> None:
        pos = -1 if self.out is None else len(self.out.getvalue())
        self.calls.append((*call, pos))

    def push_tool(self, package_dir: Path) -> None:
        self._mark("push", package_dir)
        if self.push_error is not None:
            raise self.push_error

    def agent(self, *args: str, tty: bool = False) -> AgentReply:
        self._mark("agent", args)
        self.ttys.append(tty)
        queue = self.replies.get(args[0])
        assert queue, f"台本にない呼び出し: {args[0]}"
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def stream(self, *args: str) -> Iterator[dict[str, object]]:
        self._mark("stream", args)
        for line in self.lines:
            if isinstance(line, BaseException):
                raise line
            yield line

    def fetch(self, remote_path: str, local_path: Path) -> None:
        self._mark("fetch", remote_path, local_path)
        if self.fetch_error is not None:
            raise self.fetch_error
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"x" * 3 * 1024 * 1024)

    def send(self, local_path: Path, remote_path: str) -> None:
        self._mark("send", local_path, remote_path)
        if self.send_error is not None:
            raise self.send_error

    def agent_names(self) -> list[str]:
        return [str(c[1][0]) for c in self.calls if c[0] == "agent"]  # type: ignore[index]

    def agent_args(self, name: str) -> tuple[str, ...]:
        for c in self.calls:
            if c[0] == "agent" and c[1][0] == name:  # type: ignore[index]
                return c[1]  # type: ignore[return-value]
        raise AssertionError(name)


@dataclass
class Setup:
    remote: FakeRemote
    out: io.StringIO
    ctx: devcli.DevContext
    hosts: list[str]
    resolved: list[str]  # resolve に渡された版の名前

    def run(self, *argv: str) -> int:
        return devcli.main(list(argv), context=self.ctx)

    @property
    def text(self) -> str:
        return self.out.getvalue()


def make_setup(
    tmp_path: Path,
    remote: FakeRemote,
    *,
    resolution: Resolution | BaseException = TAG_ONLY,
    config_error: ConfigError | None = None,
    run: FakeRunner | None = None,
) -> Setup:
    out = io.StringIO()
    remote.out = out
    hosts: list[str] = []
    resolved: list[str] = []
    config = DevConfig(host=HOST, backup_dir=tmp_path / "backups", repo_url=REPO_URL)

    def load_config(env: Mapping[str, str], repo_root: Path, run: Runner) -> DevConfig:
        if config_error is not None:
            raise config_error
        return config

    def resolve(name: str, repo_root: Path, run: Runner) -> Resolution:
        resolved.append(name)
        if isinstance(resolution, BaseException):
            raise resolution
        return resolution

    def make_remote(host: str, run: Runner) -> FakeRemote:
        hosts.append(host)
        return remote

    ctx = devcli.DevContext(
        env={},
        repo_root=tmp_path,
        run=FakeRunner() if run is None else run,
        out=out,
        load_config=load_config,
        resolve=resolve,
        make_remote=make_remote,
        now=lambda: NOW,
        package_dir=tmp_path / "pkg",
    )
    return Setup(remote, out, ctx, hosts, resolved)
