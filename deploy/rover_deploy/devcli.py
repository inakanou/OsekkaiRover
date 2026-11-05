"""開発 PC の入口（`rover-deploy`）。

入れ替え（`deploy <タグかブランチ>`）と普段の版に戻す（`back`）の流れ:
1. 設定を読む。版を解決する（deploy だけ）
2. 道具を実機に写す。つながらなければ中止（10）
3. 事前の確認で、今の版とこれからの版を、実機を変える前に表示する。中止の理由があれば 10
4. バックアップを作って開発 PC に受け取る。初回はサンプルのバックアップも受け取り、
   受け取れたら実機に記録する。どれかが失敗したら 10（実機はバックアップのほかは変わらない）
5. 適用を切り離して始め、記録を追う。途中で切れたら 13（`rover-deploy status` を案内）
6. 結果を表示し、終了コードで終える（成功 0・中止 10・切り戻し 11・停止 12）

表示には、設定の値（接続先など）・実機の置き場所・外のコマンドの標準エラー・記録の中身を
出さない（10.3）。バックアップは開発 PC の場所と大きさだけを出す。

サブコマンドを足すときは、`_subcommands` の表に名前と、引数を決める関数と、行う関数を足す。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol, TextIO

from rover_deploy.common import (
    DeployResult,
    ExitCode,
    Target,
    dump_json,
    result_from_dict,
    target_from_dict,
    target_to_dict,
    version_label,
)
from rover_deploy.devconfig import ConfigError, DevConfig, load_dev_config
from rover_deploy.refs import RefError, Resolution, resolve_ref
from rover_deploy.remote import AgentReply, Remote, RemoteError
from rover_deploy.state import make_deploy_id
from rover_deploy.system import Runner, SubprocessRunner

USAGE_ERROR = 2  # argparse の使い方の誤りと同じ

PACKAGE_DIR = Path(__file__).resolve().parent  # 実機に写す道具のパッケージ
REPO_ROOT = PACKAGE_DIR.parents[1]  # このリポジトリの作業コピー

MIB = 1024 * 1024
HALT_DOC = "docs/deploy.md の「失敗したときに人がすること」"

# --- 表示の文言 ---

MSG_ALSO_BRANCH = "tag と branch の両方にあります。tag を使います"
MSG_DISCONNECTED = "接続が切れました。結果は rover-deploy status で確かめてください"
MSG_AGENT_FAILED = "実機の道具が思わぬ形で終わりました。何も変えずに中止します"
MSG_NO_EVERYDAY = (
    "普段の版がまだ決まっていません。rover-deploy everyday <タグか sample> で決めてください"
)
MSG_ALREADY_SAMPLE = "もう普段の版（サンプル）で動いています。何も変えずに終えます"
MSG_RECOVER_INTERRUPTED = "前の入れ替えが途中で終わっています。普段の版に戻します"
REASON_INTERRUPTED = "interrupted"
MSG_STATE_BROKEN = "実機の道具の状態のファイルが読めません。何も変えずに中止します"
MSG_BAD_BACKUP = "実機のバックアップの答えの形が違います。何も変えずに中止します"

# 中止の理由の名前 → 文言。busy と disk_low は詳しい文言を別に作る
REASON_MESSAGES: Mapping[str, str] = {
    "driving": "実機が走行中です。止まってからやり直してください",
    "unknown": "実機のサービスの様子を確かめられません。少し待ってからやり直してください",
    "sample_running": (
        "サンプルが動いています。使っていないことを確かめてから rover-deploy stop で止めてください"
    ),
    "lock_busy": "ほかの入れ替えなどが実機で動いています。終わってからやり直してください",
    "interrupted": (
        "前の入れ替えが途中で終わっています。rover-deploy status で確かめ、"
        "rover-deploy back で戻してください"
    ),
    "state_broken": "実機の道具の状態のファイルが読めません。手順書を見て直してください",
    "dirty": "今の版に手の変更があります。変更を片付けてからやり直してください",
    "dirty_unknown": "今の版の手の変更を数えられません",
    "restore_leftover": "途中で止まった復元を片付けられません。手順書を見て直してください",
    "backup_failed": "実機でバックアップを作れません",
    "no_sample_backup": "実機にサンプルのバックアップが見つかりません",
    "apply_running": "実機で入れ替えがもう動いています。rover-deploy status で確かめてください",
    "apply_unknown": "実機で入れ替えが動いているか確かめられません",
    "start_failed": "実機で入れ替えを始められません",
    "bad_target": "版の形が違います",
    "bad_deploy_id": "入れ替えの識別子の形が違います",
    "unexpected": "実機の道具が思わぬ失敗をしました",
}

# 記録の手順の名前 → 表示の名前
STEP_NAMES: Mapping[str, str] = {
    "prepare": "準備",
    "recheck": "安全の確認",
    "calibration": "調整値の移し替え",
    "switch": "切り替え",
    "start": "起動",
    "verify": "確認",
    "enable": "自動の起動の設定",
    "prune": "古い版の片付け",
    "rollback": "切り戻し",
    "halt": "停止",
    "lock": "ロック",
    "apply": "適用",
}
STATUS_NAMES: Mapping[str, str] = {"ok": "よい", "failed": "失敗"}

OUTCOME_NAMES: Mapping[str, str] = {
    "succeeded": "成功",
    "aborted": "中止（実機は変えていません）",
    "rolled_back": "切り戻した（前の版に戻しました）",
    "halted": "止めた（サービスを止めました）",
}
OUTCOME_CODES: Mapping[str, ExitCode] = {
    "succeeded": ExitCode.OK,
    "aborted": ExitCode.ABORTED,
    "rolled_back": ExitCode.ROLLED_BACK,
    "halted": ExitCode.HALTED,
}
RUNNING_NAMES: Mapping[str, str] = {
    "none": "止まっています",
    "unknown": "分かりません",
    "both": "分かりません（両方のサービスが動いています）",
}

ENABLE_FAILED = "enable"  # 成功したが、普段の版の自動の起動を設定できなかった

_FILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class RemotePort(Protocol):
    """devcli が使う実機とのやり取り（`remote.Remote`）。テストでは偽物に差し替える。"""

    def push_tool(self, package_dir: Path) -> None: ...

    def agent(self, *args: str, tty: bool = False) -> AgentReply: ...

    def stream(self, *args: str) -> Iterator[dict[str, object]]: ...

    def fetch(self, remote_path: str, local_path: Path) -> None: ...


def _default_remote(host: str, run: Runner) -> RemotePort:
    return Remote(host, run)


@dataclass
class DevContext:
    """devcli の外とのつながり。テストでは偽物に差し替える。"""

    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    repo_root: Path = REPO_ROOT
    run: Runner = field(default_factory=SubprocessRunner)
    out: TextIO = field(default_factory=lambda: sys.stdout)
    load_config: Callable[[Mapping[str, str], Path, Runner], DevConfig] = load_dev_config
    resolve: Callable[[str, Path, Runner], Resolution] = resolve_ref
    make_remote: Callable[[str, Runner], RemotePort] = _default_remote
    now: Callable[[], float] = time.time
    package_dir: Path = PACKAGE_DIR

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)


class Abort(Exception):
    """流れを止めて、決まった文言と終了コードで終える。"""

    def __init__(self, message: str, code: int = ExitCode.ABORTED) -> None:
        super().__init__(message)
        self.code = code


# --- 小さな道具 ---


def _label(data: Mapping[str, object], key: str, running: object) -> str:
    """今の版の表示。版が分からなければ、動いているサービスの様子で表す。"""
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    if isinstance(running, str) and running in RUNNING_NAMES:
        return RUNNING_NAMES[running]
    return "分かりません"


def _size_text(size: int) -> str:
    return f"{size / MIB:.1f} MB"


def _reason_text(reason: str, report: Mapping[str, object]) -> str:
    if reason == "busy":
        safety = report.get("safety")
        busy = safety.get("busy") if isinstance(safety, dict) else None
        names = [n for n in busy if isinstance(n, str)] if isinstance(busy, list) else []
        joined = "、".join(names) if names else "分かりません"
        return (
            f"実機で作業中の機能があります（作業中の機能: {joined}）。"
            "終わってからやり直してください"
        )
    if reason == "disk_low":
        disk = report.get("disk")
        free = disk.get("free_bytes") if isinstance(disk, dict) else None
        needed = disk.get("needed_bytes") if isinstance(disk, dict) else None
        if isinstance(free, int) and isinstance(needed, int):
            return (
                f"実機の空き容量が足りません（空き: {_size_text(free)}、"
                f"要る: {_size_text(needed)}）"
            )
        return "実機の空き容量が足りないか、確かめられません"
    return REASON_MESSAGES.get(reason, f"中止の理由: {reason}")


def _reasons(report: Mapping[str, object]) -> list[str]:
    raw = report.get("reasons")
    if isinstance(raw, list):
        names = [r for r in raw if isinstance(r, str) and r]
        if names:
            return names
    reason = report.get("reason")
    return [reason] if isinstance(reason, str) and reason else ["unexpected"]


def _ok_reply(reply: AgentReply) -> dict[str, object]:
    """実機の道具の答えを確かめる。最後まで動けなかったか、だめなら中止（10）。"""
    data = reply.data
    if reply.exit_code != 0 or data is None:
        raise Abort(MSG_AGENT_FAILED)
    if data.get("ok") is not True:
        raise Abort("\n".join(_reason_text(r, data) for r in _reasons(data)))
    return data


# --- 流れの手順 ---


@dataclass(frozen=True)
class Plan:
    target: Target
    everyday: bool


def _load_config(ctx: DevContext) -> DevConfig:
    try:
        return ctx.load_config(ctx.env, ctx.repo_root, ctx.run)
    except ConfigError as e:
        raise Abort(str(e), e.exit_code) from None


def _resolve(ctx: DevContext, name: str) -> Target:
    try:
        resolution = ctx.resolve(name, ctx.repo_root, ctx.run)
    except RefError as e:
        raise Abort(str(e), e.exit_code) from None
    if resolution.also_branch:
        ctx.say(MSG_ALSO_BRANCH)
    return resolution.target


def _everyday_from_status(remote: RemotePort) -> Target:
    """実機に記録された普段の版。決まっていなければ中止（10）。"""
    reply = remote.agent("status")
    data = reply.data
    if reply.exit_code != 0 or data is None:
        raise Abort(MSG_AGENT_FAILED)
    if data.get("ok") is not True:
        raise Abort(MSG_STATE_BROKEN)
    raw = data.get("everyday")
    if raw is None:
        raise Abort(MSG_NO_EVERYDAY)
    if not isinstance(raw, dict):
        raise Abort(MSG_AGENT_FAILED)
    try:
        return target_from_dict(raw)
    except ValueError:
        raise Abort(MSG_AGENT_FAILED) from None


def _same_target(raw: object, target: Target) -> bool:
    if not isinstance(raw, dict):
        return False
    try:
        return target_from_dict(raw) == target
    except ValueError:
        return False


def _preflight(ctx: DevContext, remote: RemotePort, target: Target) -> dict[str, object]:
    """事前の確認。今の版とこれからの版を、何かを変える前に表示する（1.5）。"""
    reply = remote.agent("preflight", "--target", dump_json(target_to_dict(target)))
    data = reply.data
    if reply.exit_code != 0 or data is None:
        raise Abort(MSG_AGENT_FAILED)
    current = _label(data, "current_label", data.get("running"))
    ctx.say(f"今の版: {current} / これからの版: {version_label(target)}")
    return data


def _check_reasons(data: Mapping[str, object], *, allowed: frozenset[str] = frozenset()) -> None:
    """事前の確認の中止の理由を確かめる。`allowed` のほかに理由があれば中止（10）。"""
    if data.get("ok") is True:
        return
    blocking = [r for r in _reasons(data) if r not in allowed]
    if blocking:
        raise Abort("\n".join(_reason_text(r, data) for r in blocking))


def _local_name(remote_path: object) -> str:
    if not isinstance(remote_path, str):
        raise Abort(MSG_BAD_BACKUP)
    name = PurePosixPath(remote_path).name
    if not _FILE_NAME_RE.fullmatch(name):
        raise Abort(MSG_BAD_BACKUP)
    return name


def _receive(ctx: DevContext, remote: RemotePort, info: object, dest_dir: Path, what: str) -> None:
    """実機のバックアップを開発 PC に受け取り、場所と大きさだけを表示する（3.3）。"""
    if not isinstance(info, dict):
        raise Abort(MSG_BAD_BACKUP)
    remote_path = info.get("path")
    local = dest_dir / _local_name(remote_path)
    assert isinstance(remote_path, str)
    try:
        remote.fetch(remote_path, local)
    except ValueError:
        raise Abort(MSG_BAD_BACKUP) from None
    try:
        size = local.stat().st_size
    except OSError:
        raise Abort("受け取ったバックアップが見つかりません。中止します") from None
    ctx.say(f"{what}を受け取りました: {local}（{_size_text(size)}）")


def _backup(ctx: DevContext, remote: RemotePort, config: DevConfig, deploy_id: str) -> None:
    """バックアップを作って受け取る。初回はサンプルのバックアップも受け取り、記録する（9.2）。"""
    data = _ok_reply(remote.agent("backup", "--deploy-id", deploy_id))
    dest_dir = config.backup_dir / deploy_id
    _receive(ctx, remote, data.get("backup"), dest_dir, "バックアップ")
    sample = data.get("sample_backup")
    if sample is None:
        if data.get("first_deploy") is True:
            raise Abort(MSG_BAD_BACKUP)
        return
    _receive(ctx, remote, sample, dest_dir, "サンプルのバックアップ")
    _ok_reply(remote.agent("mark-sample-saved", deploy_id))
    ctx.say("サンプルのバックアップを受け取れたことを、実機に記録しました")


def _start_apply(remote: RemotePort, config: DevConfig, plan: Plan, deploy_id: str) -> None:
    args = ["apply", "--target", dump_json(target_to_dict(plan.target)), "--deploy-id", deploy_id]
    if plan.everyday:
        args.append("--everyday")
    args += ["--repo-url", config.repo_url]
    try:
        reply = remote.agent(*args)
    except RemoteError:
        # 実機で始まったかどうか分からない
        raise Abort(MSG_DISCONNECTED, ExitCode.UNKNOWN) from None
    _ok_reply(reply)


def _step_text(event: Mapping[str, object]) -> str | None:
    """記録の 1 行を短い表示にする。始まりの行と、知らない形の行は出さない。"""
    step = event.get("step")
    status = event.get("status")
    if not isinstance(step, str) or status not in STATUS_NAMES:
        return None
    assert isinstance(status, str)
    prefix = ""
    if step.startswith("rollback."):
        prefix, step = "切り戻しの", step.removeprefix("rollback.")
    name = STEP_NAMES.get(step, step)
    return f"  {prefix}{name}: {STATUS_NAMES[status]}"


def _follow(ctx: DevContext, remote: RemotePort, deploy_id: str) -> DeployResult:
    """記録を結果の行まで追う。結果が分からなければ 13。"""
    try:
        for line in remote.stream("follow", deploy_id):
            raw = line.get("result")
            if "result" in line:
                if not isinstance(raw, dict):
                    break
                try:
                    return result_from_dict(raw)
                except ValueError:
                    break
            if "error" in line or "ok" in line:
                break
            text = _step_text(line)
            if text is not None:
                ctx.say(text)
    except RemoteError:
        pass
    raise Abort(MSG_DISCONNECTED, ExitCode.UNKNOWN)


def _report(ctx: DevContext, result: DeployResult) -> int:
    """結果を表示し、終了コードを返す（1.10、4.3）。"""
    ctx.say(f"結果: {OUTCOME_NAMES[result.outcome]}")
    running = "なし（止まっています）" if result.running is None else version_label(result.running)
    ctx.say(f"動いている版: {running}")
    failed = [name for name in result.failed_checks if name != ENABLE_FAILED]
    if failed:
        ctx.say(f"失敗した確認: {', '.join(failed)}")
    if ENABLE_FAILED in result.failed_checks:
        ctx.say(
            "注意: 自動の起動の設定ができませんでした。再起動すると別の版で立ち上がることがあります"
        )
    if result.outcome == "halted":
        ctx.say(f"人の手が要ります。{HALT_DOC}を見てください")
    return OUTCOME_CODES[result.outcome]


def _run_flow(ctx: DevContext, config: DevConfig, remote: RemotePort, plan: Plan) -> int:
    """事前の確認の後: バックアップ → 切り離した適用 → 追跡 → 結果。"""
    deploy_id = make_deploy_id(ctx.now())
    _backup(ctx, remote, config, deploy_id)
    _start_apply(remote, config, plan, deploy_id)
    ctx.say(f"入れ替えを始めました（識別子: {deploy_id}）")
    return _report(ctx, _follow(ctx, remote, deploy_id))


def _connect(ctx: DevContext, config: DevConfig) -> RemotePort:
    remote = ctx.make_remote(config.host, ctx.run)
    remote.push_tool(ctx.package_dir)
    return remote


# --- サブコマンド ---


def cmd_deploy(ctx: DevContext, args: argparse.Namespace) -> int:
    """タグかブランチを入れる（1）。"""
    config = _load_config(ctx)
    target = _resolve(ctx, args.ref)
    remote = _connect(ctx, config)
    report = _preflight(ctx, remote, target)
    _check_reasons(report)
    plan = Plan(target, everyday=_same_target(report.get("everyday"), target))
    return _run_flow(ctx, config, remote, plan)


def cmd_back(ctx: DevContext, args: argparse.Namespace) -> int:
    """普段の版に戻す（5.1）。"""
    config = _load_config(ctx)
    remote = _connect(ctx, config)
    target = _everyday_from_status(remote)
    report = _preflight(ctx, remote, target)
    reasons = [] if report.get("ok") is True else _reasons(report)
    interrupted = REASON_INTERRUPTED in reasons
    if target.kind == "sample" and report.get("running") == "sample" and not interrupted:
        ctx.say(MSG_ALREADY_SAMPLE)
        return ExitCode.OK
    # 途中で終わった入れ替えは、普段の版に戻して片付ける（back のほかに出口がない）
    _check_reasons(report, allowed=frozenset({REASON_INTERRUPTED}))
    if interrupted:
        ctx.say(MSG_RECOVER_INTERRUPTED)
    return _run_flow(ctx, config, remote, Plan(target, everyday=True))


def _add_deploy(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("ref", help="入れる版（タグかブランチの名前）")


def _no_arguments(parser: argparse.ArgumentParser) -> None:
    del parser


Handler = Callable[[DevContext, argparse.Namespace], int]


@dataclass(frozen=True)
class Subcommand:
    name: str
    help: str
    configure: Callable[[argparse.ArgumentParser], None]
    handler: Handler


def _subcommands() -> list[Subcommand]:
    """サブコマンドの表。足すときはここに並べる。"""
    return [
        Subcommand("deploy", "タグかブランチを実機に入れる", _add_deploy, cmd_deploy),
        Subcommand("back", "普段の版に戻す", _no_arguments, cmd_back),
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rover-deploy",
        description="開発 PC から、実機（PiCar-X）に版を入れる道具",
    )
    subparsers = parser.add_subparsers(dest="command")
    for sub in _subcommands():
        child = subparsers.add_parser(sub.name, help=sub.help)
        sub.configure(child)
        child.set_defaults(handler=sub.handler)
    return parser


def main(argv: Sequence[str] | None = None, *, context: DevContext | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Handler | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_usage(sys.stderr)
        print("rover-deploy: サブコマンドを指定してください", file=sys.stderr)
        return USAGE_ERROR
    ctx = DevContext() if context is None else context
    try:
        return handler(ctx, args)
    except Abort as e:
        ctx.say(str(e))
        return e.code
    except RemoteError as e:
        # 文言は決まった文だけ（接続先や標準エラーは含まない）
        ctx.say(str(e))
        return e.exit_code
