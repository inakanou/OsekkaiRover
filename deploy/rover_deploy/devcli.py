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

そのほかのサブコマンド: status・everyday・self-check・backups・restore・stop・log・provision。
どのサブコマンドも、道具を写した後に版の確認を見て、試しの版が長く入っていれば
最初に警告する（5.5）。

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
from rover_deploy.layout import TOOL_APP_NAME
from rover_deploy.notices import REMIND_AFTER_H
from rover_deploy.refs import GIT_ENV, NETWORK_TIMEOUT_S, RefError, Resolution, resolve_ref
from rover_deploy.refs import RemoteUnreachable as OriginUnreachable
from rover_deploy.releases import prepare_message
from rover_deploy.remote import REMOTE_TOOL_DIR, AgentReply, Remote, RemoteError
from rover_deploy.state import make_deploy_id, validate_deploy_id
from rover_deploy.system import CommandError, Runner, SubprocessRunner

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

ABORT_PREFIX = "中止の理由: "

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
    "no_backup": "そのバックアップが実機にありません。rover-deploy backups で確かめてください",
    "bad_backup_file": "送ったファイルを実機で受け付けられません",
    "backups_unreadable": "実機のバックアップの一覧を読めません",
    "stop_failed": "サービスを止められません。何も戻していません",
    "restore_failed": "バックアップから戻せませんでした",
    "start_failed_after": "戻した後にサービスを起動できません",
    "halt_failed": "サービスか安全停止のどこかが失敗しました",
    "no_release": (
        "実機に新しい基盤の版がありません。rover-deploy provision か deploy で入れてください"
    ),
    "self_check_error": "self-check を行えませんでした",
    "self_check_unreadable": "self-check の結果が読めません",
    "provision_failed": "初期設定の手順が失敗しました",
    "no_log": "入れ替えの記録が見つかりません",
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

# self-check の項目の様子 → 表示の名前（6.1）
CHECK_STATUS_NAMES: Mapping[str, str] = {"ok": "よい", "failed": "だめ", "skipped": "飛ばした"}
SELF_CHECK_FAILED = 1  # self-check にだめの項目がある（基盤の self-check と同じ終了コード）

# 停止の段階の名前 → 表示の名前
HALT_STEP_NAMES: Mapping[str, str] = {
    "stop_core": "新しい基盤のサービス",
    "stop_sample": "サンプルのサービス",
    "safe_stop": "安全停止",
}
HALT_SAFE_STOP = "safe_stop"

REMIND_AFTER_S = REMIND_AFTER_H * 3600
MSG_TRIAL_REMINDER = (
    f"試しの版が {REMIND_AFTER_H} 時間以上入っています。rover-deploy back で戻してください"
)
MSG_EVERYDAY_TAG = (
    "普段の版にはタグを指定してください（ブランチは rover-deploy deploy で試しの版として入れます）"
)
MSG_NO_LOCAL_FILE = (
    "戻すファイルが見つかりません。開発 PC のバックアップのファイルを指定してください"
)
MSG_STOPPED_AFTER_RESTORE = (
    "サービスは止まったままです。rover-deploy back で普段の版を起動してください"
)
MSG_STOP_UNCONFIRMED = (
    "サービスは止めました。モーターが止まったことを確かめられませんでした"
    "（版がないか、安全停止が失敗しました）。実機のモーターが止まっているかを確かめてください"
)

SAMPLE_TARGET = Target(kind="sample", ref=None, ref_kind=None, sha=None)
SAMPLE_NAME = "sample"  # everyday で普段の版をサンプルにする名前
DEFAULT_PROVISION_BRANCH = "main"  # 初期設定で、版のタグがないときに使うブランチ
_VERSION_TAG_RE = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)")

# 開発 PC のファイルから戻すときに送る先。実機の家のディレクトリから見た、道具の
# バックアップの置き場所（XDG_STATE_HOME を変えていないとき。変えていれば実機の道具が断る）
REMOTE_BACKUPS_DIR = f".local/state/{TOOL_APP_NAME}/backups"
UPLOAD_PREFIX = "uploaded-"
# 実機の道具は REMOTE_TOOL_DIR で動くので、そこから家のディレクトリへ戻る道
_TOOL_TO_HOME = "/".join([".."] * len(PurePosixPath(REMOTE_TOOL_DIR.removeprefix("~/")).parts))

ENABLE_FAILED = "enable"  # 成功したが、普段の版の自動の起動を設定できなかった

_FILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class RemotePort(Protocol):
    """devcli が使う実機とのやり取り（`remote.Remote`）。テストでは偽物に差し替える。"""

    def push_tool(self, package_dir: Path) -> None: ...

    def agent(self, *args: str, tty: bool = False) -> AgentReply: ...

    def stream(self, *args: str) -> Iterator[dict[str, object]]: ...

    def fetch(self, remote_path: str, local_path: Path) -> None: ...

    def send(self, local_path: Path, remote_path: str) -> None: ...


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
    return REASON_MESSAGES.get(reason, f"{ABORT_PREFIX}{reason}")


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


def _everyday_from_status(ctx: DevContext, remote: RemotePort) -> Target:
    """実機に記録された普段の版。決まっていなければ中止（10）。"""
    reply = remote.agent("status")
    data = reply.data
    if reply.exit_code != 0 or data is None:
        raise Abort(MSG_AGENT_FAILED)
    _warn_trial(ctx, data)
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


def _abort_texts(names: Sequence[str]) -> list[str]:
    """適用の側の中止の名前（prepare:<段階>・safety:<理由>・busy:<機能>）を人向けの文言にする。"""
    busy = [n.removeprefix("busy:") for n in names if n.startswith("busy:")]
    texts: list[str] = []
    for name in names:
        kind, _, detail = name.partition(":")
        if kind == "busy":
            continue
        if kind == "prepare":
            texts.append(f"準備: {prepare_message(detail)}")
        elif kind == "safety":
            # 知らない理由の文言は「中止の理由: 」で始まるので、表示の頭と重ねない
            texts.append(
                _reason_text(detail, {"safety": {"busy": busy}}).removeprefix(ABORT_PREFIX)
            )
        else:
            texts.append(REASON_MESSAGES.get(name, name))
    return texts


def _report(ctx: DevContext, result: DeployResult) -> int:
    """結果を表示し、終了コードを返す（1.10、4.3）。"""
    ctx.say(f"結果: {OUTCOME_NAMES[result.outcome]}")
    running = "なし（止まっています）" if result.running is None else version_label(result.running)
    ctx.say(f"動いている版: {running}")
    failed = [name for name in result.failed_checks if name != ENABLE_FAILED]
    if result.outcome == "aborted":
        for text in _abort_texts(failed):
            ctx.say(f"{ABORT_PREFIX}{text}")
    elif failed:
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


# --- 戻し忘れの警告（5.5） ---


def _warn_trial(ctx: DevContext, data: Mapping[str, object]) -> None:
    """試しの版が `REMIND_AFTER_H` を超えて入っていれば（実機が知らせていれば）警告する。"""
    if data.get("is_trial") is not True:
        return
    elapsed = data.get("trial_elapsed_s")
    long_trial = (
        isinstance(elapsed, (int, float))
        and not isinstance(elapsed, bool)
        and elapsed >= REMIND_AFTER_S
    )
    if long_trial or data.get("reminded_at") is not None:
        ctx.say(MSG_TRIAL_REMINDER)


def _connect_and_warn(ctx: DevContext, config: DevConfig) -> RemotePort:
    """道具を写し、版の確認を見て戻し忘れを警告する。

    確かめられなくても、サブコマンドは進める（停止などを止めないため）。
    """
    remote = _connect(ctx, config)
    try:
        reply = remote.agent("status")
    except RemoteError:
        return remote
    if reply.exit_code == 0 and reply.data is not None:
        _warn_trial(ctx, reply.data)
    return remote


def _agent_data(reply: AgentReply, *, usage_ok: bool = False) -> dict[str, object]:
    """実機の道具の答え。最後まで動けなかったなら中止（10）。

    `usage_ok` なら、使い方の誤り（2）で理由を返したときも答えとして受け取る。
    """
    allowed = (0, USAGE_ERROR) if usage_ok else (0,)
    if reply.exit_code not in allowed or reply.data is None:
        raise Abort(MSG_AGENT_FAILED)
    return reply.data


def _fail(data: Mapping[str, object]) -> Abort:
    return Abort("\n".join(_reason_text(r, data) for r in _reasons(data)))


def _names(raw: object) -> list[str]:
    """実機から返った名前の一覧。形の違うものは出さない。"""
    if not isinstance(raw, list):
        return []
    return [n for n in raw if isinstance(n, str) and _FILE_NAME_RE.fullmatch(n)]


def _parse_id(text: str) -> str:
    try:
        return validate_deploy_id(text)
    except ValueError:
        raise Abort(REASON_MESSAGES["bad_deploy_id"]) from None


def _duration_text(seconds: float) -> str:
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 時間 {minutes} 分" if hours else f"{minutes} 分"


def _time_text(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


# --- サブコマンド ---


def cmd_deploy(ctx: DevContext, args: argparse.Namespace) -> int:
    """タグかブランチを入れる（1）。"""
    config = _load_config(ctx)
    target = _resolve(ctx, args.ref)
    remote = _connect_and_warn(ctx, config)
    report = _preflight(ctx, remote, target)
    _check_reasons(report)
    plan = Plan(target, everyday=_same_target(report.get("everyday"), target))
    return _run_flow(ctx, config, remote, plan)


def cmd_back(ctx: DevContext, args: argparse.Namespace) -> int:
    """普段の版に戻す（5.1）。"""
    config = _load_config(ctx)
    remote = _connect(ctx, config)
    target = _everyday_from_status(ctx, remote)
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


def cmd_status(ctx: DevContext, args: argparse.Namespace) -> int:
    """動いている版・普段の版・試しの経過・最後の入れ替えを表示する（7.2、5.4）。"""
    config = _load_config(ctx)
    remote = _connect(ctx, config)
    data = _agent_data(remote.agent("status"))
    _warn_trial(ctx, data)
    ctx.say(f"動いている版: {_label(data, 'current_label', data.get('running'))}")
    if data.get("ok") is not True:
        raise Abort(REASON_MESSAGES["state_broken"])
    everyday = data.get("everyday_label")
    if not isinstance(everyday, str) or not everyday:
        everyday = "まだ決まっていません（rover-deploy everyday で決めます）"
    ctx.say(f"普段の版: {everyday}")
    if data.get("is_trial") is True:
        elapsed = data.get("trial_elapsed_s")
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
            ctx.say(f"試しの版です（入れてから {_duration_text(elapsed)}）")
        else:
            ctx.say("試しの版です")
    else:
        ctx.say("試しの版ではありません")
    ctx.say(f"最後の入れ替え: {_last_deploy_text(data.get('last_deploy'))}")
    in_progress = data.get("in_progress")
    if isinstance(in_progress, str):
        deploy_id = _parse_id(in_progress)
        if data.get("lock") == "busy":
            ctx.say(f"入れ替え {deploy_id} が実機で動いています")
        else:
            ctx.say(
                f"注意: 入れ替え {deploy_id} が途中で終わっています。"
                "rover-deploy back で普段の版に戻してください"
            )
    return ExitCode.OK


def _last_deploy_text(raw: object) -> str:
    if not isinstance(raw, dict):
        return "まだありません"
    deploy_id, outcome, finished = raw.get("id"), raw.get("outcome"), raw.get("finished_at")
    if not isinstance(deploy_id, str) or not isinstance(outcome, str):
        return "分かりません"
    text = f"{_parse_id(deploy_id)} {OUTCOME_NAMES.get(outcome, '分かりません')}"
    if isinstance(finished, (int, float)) and not isinstance(finished, bool):
        text += f"（{_time_text(finished)}）"
    return text


def cmd_everyday(ctx: DevContext, args: argparse.Namespace) -> int:
    """普段の版を記録する（5.2）。入れ替えはしない（back で行う）。"""
    config = _load_config(ctx)
    if args.version == SAMPLE_NAME:
        target = SAMPLE_TARGET
    else:
        target = _resolve(ctx, args.version)
        if target.ref_kind != "tag":
            raise Abort(MSG_EVERYDAY_TAG)
    remote = _connect_and_warn(ctx, config)
    _ok_reply(remote.agent("set-everyday", "--target", dump_json(target_to_dict(target))))
    ctx.say(
        f"普段の版を {version_label(target)} にしました。入れ替えは rover-deploy back で行います"
    )
    return ExitCode.OK


def cmd_self_check(ctx: DevContext, args: argparse.Namespace) -> int:
    """実機で self-check を行い、項目ごとに表示する（6.8）。だめがあれば 1。"""
    config = _load_config(ctx)
    remote = _connect_and_warn(ctx, config)
    data = _agent_data(remote.agent("self-check", "--json"))
    results = data.get("results")
    if not isinstance(results, list):
        raise _fail(data)
    for item in results:
        if not isinstance(item, dict):
            continue
        name, status, reason = item.get("name"), item.get("status"), item.get("reason")
        if not isinstance(name, str) or not isinstance(status, str):
            continue
        line = f"  {CHECK_STATUS_NAMES.get(status, status)}: {name}"
        # 理由は基盤の決まった文言だけ（6.6）
        if isinstance(reason, str) and reason:
            line += f"（{reason}）"
        ctx.say(line)
    if data.get("no_hardware") is True:
        ctx.say("サンプルが動いているので、ハードの項目は確かめていません")
    return ExitCode.OK if data.get("ok") is True else SELF_CHECK_FAILED


def cmd_backups(ctx: DevContext, args: argparse.Namespace) -> int:
    """実機のバックアップの一覧（新しい順）を、識別子と大きさで表示する（3.7）。"""
    config = _load_config(ctx)
    remote = _connect_and_warn(ctx, config)
    data = _ok_reply(remote.agent("backups"))
    raw = data.get("backups")
    items = raw if isinstance(raw, list) else []
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        backup_id, size = item.get("id"), item.get("size_bytes")
        if isinstance(backup_id, str) and isinstance(size, int):
            lines.append(f"  {_parse_id(backup_id)}  {_size_text(size)}")
    if not lines:
        ctx.say("実機にバックアップがありません")
        return ExitCode.OK
    ctx.say("実機のバックアップ（新しい順）:")
    for line in lines:
        ctx.say(line)
    return ExitCode.OK


def _upload(ctx: DevContext, remote: RemotePort, local: Path) -> str:
    """開発 PC のファイルを実機のバックアップの置き場所に送り、道具に渡す置き場所を返す。"""
    remote_path = f"{REMOTE_BACKUPS_DIR}/{UPLOAD_PREFIX}{make_deploy_id(ctx.now())}.tar.gz"
    try:
        remote.send(local, remote_path)
    except ValueError:
        raise Abort(MSG_NO_LOCAL_FILE) from None
    return f"{_TOOL_TO_HOME}/{remote_path}"


def cmd_restore(ctx: DevContext, args: argparse.Namespace) -> int:
    """バックアップから戻す（3.7）。実機の識別子か、開発 PC のファイルを指定する。

    実機の道具が、安全の確認 → 今のバックアップ → 停止 → 戻す → 起動、を行う。
    止めたサービスを起動し直せなければ 12（人の手が要る）。
    """
    local: Path | None = None
    backup_id: str | None = None
    if args.file is not None:
        local = Path(args.file)
        if not local.is_file():
            raise Abort(MSG_NO_LOCAL_FILE)
    else:
        backup_id = _parse_id(args.backup_id)
    config = _load_config(ctx)
    remote = _connect_and_warn(ctx, config)
    if local is not None:
        agent_args = ["restore", "--file", _upload(ctx, remote, local)]
    else:
        assert backup_id is not None
        agent_args = ["restore", backup_id]
    if args.include_sample:
        agent_args.append("--include-sample")
    data = _agent_data(remote.agent(*agent_args), usage_ok=True)
    stopped, started = _names(data.get("stopped")), _names(data.get("started"))
    if data.get("ok") is not True:
        return _restore_failed(ctx, data, stopped, started)
    source = "開発 PC のファイル" if local is not None else f"バックアップ {backup_id} "
    ctx.say(f"{source}から戻しました")
    pre = data.get("pre_backup")
    if isinstance(pre, dict):
        pre_id, size = pre.get("id"), pre.get("size_bytes")
        if isinstance(pre_id, str) and isinstance(size, int):
            ctx.say(
                f"戻す前の状態を実機に残しました（識別子: {_parse_id(pre_id)}、{_size_text(size)}）"
            )
    if started:
        ctx.say(f"起動したサービス: {'、'.join(started)}")
    if not stopped:
        # 動いていたサービスがない（stop で止めてから戻した）
        ctx.say(MSG_STOPPED_AFTER_RESTORE)
    return ExitCode.OK


def _restore_failed(
    ctx: DevContext, data: Mapping[str, object], stopped: list[str], started: list[str]
) -> int:
    ctx.say(str(_fail(data)))
    not_started = [unit for unit in stopped if unit not in started]
    halted = data.get("reason") == "restore_leftover" or bool(not_started)
    if not halted:
        return ExitCode.ABORTED
    if not_started:
        ctx.say(f"起動できなかったサービス: {'、'.join(not_started)}")
    ctx.say(f"人の手が要ります。{HALT_DOC}を見てください")
    return ExitCode.HALTED


def cmd_stop(ctx: DevContext, args: argparse.Namespace) -> int:
    """両方のサービスを止め、安全停止を `--strict` で行う（11.2）。

    終了コード: 止めてモーターの停止も確かめられれば 0。どこかが失敗したら 12
    （安全停止だけが失敗しても、モーターの停止を確かめられないので 12）。
    """
    config = _load_config(ctx)
    remote = _connect_and_warn(ctx, config)
    data = _agent_data(remote.agent("stop"))
    if data.get("ok") is True:
        ctx.say("サービスを止め、モーターが止まったことを確かめました")
        return ExitCode.OK
    raw = data.get("failed")
    failed = [n for n in raw if isinstance(n, str)] if isinstance(raw, list) else []
    if data.get("reason") != "halt_failed" or not failed:
        raise _fail(data)
    if failed == [HALT_SAFE_STOP]:
        # 版がないときも、安全停止が失敗・時間切れ・ハードに触れなかったときも、この答えになる
        ctx.say(MSG_STOP_UNCONFIRMED)
        ctx.say(f"人の手が要ります。{HALT_DOC}を見てください")
        return ExitCode.HALTED
    names = [HALT_STEP_NAMES.get(n, "分からない段階") for n in failed]
    ctx.say(f"止められなかったもの: {'、'.join(names)}")
    ctx.say(f"人の手が要ります。{HALT_DOC}を見てください")
    return ExitCode.HALTED


def cmd_log(ctx: DevContext, args: argparse.Namespace) -> int:
    """入れ替えの記録を表示する（4.6）。手順と結果だけで、記録の理由は出さない（10.3）。"""
    agent_args = ["log"]
    if args.deploy_id is not None:
        agent_args.append(_parse_id(args.deploy_id))
    if args.list:
        agent_args.append("--list")
    config = _load_config(ctx)
    remote = _connect_and_warn(ctx, config)
    data = _ok_reply(remote.agent(*agent_args))
    if args.list:
        raw = data.get("ids")
        ids = [_parse_id(i) for i in raw if isinstance(i, str)] if isinstance(raw, list) else []
        if not ids:
            ctx.say("入れ替えの記録がまだありません")
            return ExitCode.OK
        ctx.say("入れ替えの記録（新しい順）:")
        for log_id in ids:
            ctx.say(f"  {log_id}")
        return ExitCode.OK
    deploy_id = data.get("deploy_id")
    if not isinstance(deploy_id, str):
        raise Abort(MSG_AGENT_FAILED)
    ctx.say(f"入れ替え {_parse_id(deploy_id)} の記録:")
    events = data.get("events")
    for event in events if isinstance(events, list) else []:
        text = _step_text(event) if isinstance(event, dict) else None
        if text is not None:
            ctx.say(text)
    raw_result = data.get("result")
    if raw_result is None:
        ctx.say("結果: まだありません")
        return ExitCode.OK
    if not isinstance(raw_result, dict):
        raise Abort(MSG_AGENT_FAILED)
    try:
        result = result_from_dict(raw_result)
    except ValueError:
        raise Abort(MSG_AGENT_FAILED) from None
    _report(ctx, result)
    return ExitCode.OK


def _latest_tag(ctx: DevContext) -> str:
    """リポジトリ（origin）の vX.Y.Z の形のタグで、いちばん新しいもの。なければ main。"""
    argv = ["git", "-C", str(ctx.repo_root), "ls-remote", "--tags", "--refs", "origin"]
    try:
        result = ctx.run(argv, timeout=NETWORK_TIMEOUT_S, check=False, env=GIT_ENV)
    except CommandError:
        raise Abort(str(OriginUnreachable())) from None
    if result.returncode != 0:
        raise Abort(str(OriginUnreachable()))
    best: tuple[tuple[int, int, int], str] | None = None
    for line in result.text.splitlines():
        _, _, ref = line.partition("\t")
        name = ref.strip().removeprefix("refs/tags/")
        match = _VERSION_TAG_RE.fullmatch(name)
        if match is None:
            continue
        key = (int(match[1]), int(match[2]), int(match[3]))
        if best is None or key > best[0]:
            best = (key, name)
    return DEFAULT_PROVISION_BRANCH if best is None else best[1]


def cmd_provision(ctx: DevContext, args: argparse.Namespace) -> int:
    """初期設定（8）。ssh に端末を割り当てる（sudo の問いに答えるため）。

    版の既定は、いちばん新しいタグ。なければ main。成功 0・失敗 10。
    """
    config = _load_config(ctx)
    name = _latest_tag(ctx) if args.ref is None else args.ref
    target = _resolve(ctx, name)
    remote = _connect_and_warn(ctx, config)
    ctx.say(f"最初の版: {version_label(target)}")
    target_json = dump_json(target_to_dict(target))
    reply = remote.agent(
        "provision", "--target", target_json, "--repo-url", config.repo_url, tty=True
    )
    data = _agent_data(reply)
    if data.get("ok") is not True and data.get("reason") != "provision_failed":
        raise _fail(data)
    for key, title in (("skipped", "済んでいたので飛ばした手順"), ("applied", "行った手順")):
        names = _names(data.get(key))
        if names:
            ctx.say(f"{title}: {', '.join(names)}")
    failed_step = data.get("failed_step")
    if isinstance(failed_step, str):
        ctx.say(f"失敗した手順: {failed_step}")
        hint = data.get("retry_hint")
        if isinstance(hint, str) and hint:
            ctx.say(f"やり直し方: {hint}")
    if data.get("needs_reboot") is True:
        ctx.say("再起動が要ります。実機で sudo reboot を行ってください（この道具は再起動しません）")
    if data.get("self_check_ok") is False:
        ctx.say("self-check にだめの項目があります。rover-deploy self-check で確かめてください")
    if data.get("ok") is not True:
        return ExitCode.ABORTED
    ctx.say("初期設定が終わりました")
    return ExitCode.OK


def _add_deploy(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("ref", help="入れる版（タグかブランチの名前）")


def _no_arguments(parser: argparse.ArgumentParser) -> None:
    del parser


def _add_everyday(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("version", help="普段の版（タグの名前か sample）")


def _add_restore(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("backup_id", nargs="?", help="戻す実機のバックアップの識別子")
    group.add_argument("--file", help="戻す開発 PC のバックアップのファイル")
    parser.add_argument("--include-sample", action="store_true", help="サンプルのデータも戻す")


def _add_log(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("deploy_id", nargs="?", help="入れ替えの識別子（なければ最新）")
    parser.add_argument("--list", action="store_true", help="識別子の一覧を出す")


def _add_provision(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ref", help="最初の版（タグかブランチ。既定はいちばん新しいタグ）")


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
        Subcommand("status", "版の確認（実機を変えない）", _no_arguments, cmd_status),
        Subcommand(
            "everyday", "普段の版を記録する（入れ替えは back）", _add_everyday, cmd_everyday
        ),
        Subcommand("self-check", "実機で self-check を行う", _no_arguments, cmd_self_check),
        Subcommand("backups", "実機のバックアップの一覧", _no_arguments, cmd_backups),
        Subcommand("restore", "バックアップから戻す", _add_restore, cmd_restore),
        Subcommand("stop", "両方のサービスを止め、厳しい安全停止を行う", _no_arguments, cmd_stop),
        Subcommand("log", "入れ替えの記録を表示する", _add_log, cmd_log),
        Subcommand("provision", "初期設定", _add_provision, cmd_provision),
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
