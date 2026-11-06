"""実機の道具の入口（`python3 -m rover_deploy.agent <サブコマンド>`）。

どのサブコマンドも、標準出力に JSON を 1 つ書く（`follow` は 1 行に 1 つの JSON）。
人に向けた文言は開発 PC が作るので、JSON には決まった理由の名前だけを入れる。
外のコマンドの出力は写さない（10.3）。

終了コード:
- 0: サブコマンドは最後まで動いた（よいかだめかは JSON の `ok` と `reason` で返す）
- 2: 使い方の誤り（引数・版・識別子の形が違う）
- 1: 思わぬ失敗（例外の型の名前だけを JSON で返す）

入れ替えのサブコマンド（1.5、1.7、1.9、2.4、3.1、3.2、4.5、9.2）:
- `preflight`: 入れる前の確認。実機を変えない
- `backup`: ロックを取り、バックアップを作る。初めての入れ替えならサンプルのバックアップも作る
- `mark-sample-saved`: 開発 PC がサンプルのバックアップを受け取れたことを記録する
- `apply`: `systemd-run` で切り離して `run-apply` を始める
- `run-apply`: 切り離された側。ロックを取り、適用の流れを最後まで行う
- `follow`: 入れ替えの記録を、結果の行まで流す

そのほかのサブコマンド（3.7、4.6、5.2、5.5、6.8、7.2、8.1、11.2）:
- `status`: 動いている版・普段の版・最後の入れ替え・試しの経過。実機を変えない
- `set-everyday`: 普段の版を記録する。入れ替えはしない
- `self-check`: 今の版の self-check を行う
- `backups` / `restore`: バックアップの一覧と復元
  （安全の確認 → 今のバックアップ → 停止 → 戻す → 起動）
- `stop`: 両方のユニットを止め、安全停止を `--strict` で行う
- `remind`: 戻し忘れを確かめる（タイマーから呼ぶ）
- `provision`: 初期設定。経過は標準エラーに出し、最後に JSON を 1 つ返す
- `log`: 入れ替えの記録を返す（識別子がなければ最新の 1 件。`--list` で一覧）

サブコマンドを足すときは、`_handlers` の表に名前と、引数を決める関数と、行う関数を足す。
"""

from __future__ import annotations

import argparse
import os
import pwd
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, TextIO

from rover_deploy.backups import (
    BackupFailed,
    BackupInfo,
    RestoreFailed,
    backup_path,
    core_data_dir,
    create_backup,
    create_sample_backup,
    estimate_backup_bytes,
    list_backups,
    prune,
    recover_interrupted_restore,
    restore_backup,
    sample_backup_path,
)
from rover_deploy.common import (
    DeployResult,
    Running,
    Target,
    dump_json,
    load_json_object,
    result_from_dict,
    result_to_dict,
    target_from_dict,
    target_to_dict,
    version_label,
)
from rover_deploy.layout import CORE_UNIT_NAME, SAMPLE_UNIT_NAME, Layout
from rover_deploy.notices import clear_notice, remind, send_reminder
from rover_deploy.provision import ProvisionContext, provision_steps, run_provision
from rover_deploy.releases import ReleaseError, Releases
from rover_deploy.service_status import SafetyVerdict, check_safety, detect_running
from rover_deploy.state import (
    LOG_SUFFIX,
    DeployLock,
    DeployLog,
    DeployState,
    StateBroken,
    StateStore,
    exception_reason,
    latest_log_id,
    list_log_ids,
    make_deploy_id,
    read_log_events,
    read_log_result,
    validate_deploy_id,
)
from rover_deploy.system import (
    CommandError,
    Runner,
    SubprocessRunner,
    is_active,
    start_unit,
    stop_unit,
)
from rover_deploy.transaction import Transaction
from rover_deploy.verify import SELF_CHECK_TIMEOUT_S, Clock, SystemClock, parse_self_check

USAGE_ERROR = 2  # argparse の使い方の誤りと同じ
UNEXPECTED_ERROR = 1

MIB = 1024 * 1024
RELEASE_SPACE_MB = 500  # 版のディレクトリと仮想環境に見込む空き（2.4）

DEPLOY_UNIT = "osekkai-deploy"  # 切り離した適用のユニットの名前
AGENT_PYTHON = "/usr/bin/python3"  # 切り離した適用を動かす python（システムの python）
AGENT_MODULE = "rover_deploy.agent"
START_TIMEOUT_S = 60.0  # systemd-run が切り離した適用を始めるまでの時間切れ

FOLLOW_POLL_S = 1.0  # 記録を読み直す間隔
FOLLOW_START_TIMEOUT_S = 30.0  # 記録のファイルができるのを待つ時間
FOLLOW_TIMEOUT_S = 3600.0  # 追跡の全体の時間切れ（準備の pip が長くかかることがある）

# --- 理由の名前（開発 PC が文言にする） ---
REASON_LOCK_BUSY = "lock_busy"  # ほかの入れ替えなどがロックを持っている（1.7）
REASON_INTERRUPTED = "interrupted"  # 前の入れ替えが途中で終わった
REASON_STATE_BROKEN = "state_broken"  # 道具の状態のファイルが読めない
REASON_DISK_LOW = "disk_low"  # 空き容量が足りない（2.4）
REASON_DIRTY = "dirty"  # 今の版に手の変更がある（1.9）
REASON_DIRTY_UNKNOWN = "dirty_unknown"  # 手の変更を数えられない
REASON_BACKUP_FAILED = "backup_failed"  # バックアップを作れない（3.2）
REASON_RESTORE_LEFTOVER = "restore_leftover"  # 途中で止まった復元を片付けられない
REASON_NO_SAMPLE_BACKUP = "no_sample_backup"  # その識別子のサンプルのバックアップがない
REASON_APPLY_RUNNING = "apply_running"  # 切り離した適用がもう動いている（1.7、4.5）
REASON_APPLY_UNKNOWN = "apply_unknown"  # 切り離した適用が動いているか分からない
REASON_START_FAILED = "start_failed"  # 切り離した適用を始められない
REASON_BAD_TARGET = "bad_target"
REASON_BAD_DEPLOY_ID = "bad_deploy_id"
REASON_UNEXPECTED = "unexpected"

# follow の終わり方（結果の行がないとき）
FOLLOW_NO_LOG = "no_log"  # 記録のファイルができなかった
FOLLOW_BROKEN_RESULT = "broken_result"  # 結果の行の形が違う
FOLLOW_ENDED_WITHOUT_RESULT = "ended_without_result"  # 適用が結果を書かずに終わった
FOLLOW_TIMEOUT = "timeout"

RUNNING_UNKNOWN = "unknown"  # systemctl が答えず、何が動いているか分からない


class UsageError(Exception):
    """引数の形が違う。`reason` は決まった理由の名前。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- 差し替えられる口 ---


class ReleasesQuery(Protocol):
    """事前の確認が使う `Releases` の口（読むだけ）。"""

    def current(self) -> Path | None: ...

    def dirty_count(self, release_dir: Path) -> int: ...


class TransactionPort(Protocol):
    """`run-apply` が使う適用の流れの口。"""

    def run(self, target: Target, deploy_id: str, *, everyday: bool) -> DeployResult: ...


def _statvfs_free_bytes(path: Path) -> int:
    stats = os.statvfs(path)
    return stats.f_bavail * stats.f_frsize


@dataclass
class AgentContext:
    """サブコマンドが使うもの。テストでは偽物に差し替える。"""

    layout: Layout
    env: Mapping[str, str]  # Layout を作ったのと同じ環境変数（バックアップの置き場所に要る）
    run: Runner
    out: TextIO
    now: Callable[[], float] = time.time
    clock: Clock = field(default_factory=SystemClock)
    free_bytes: Callable[[Path], int] = _statvfs_free_bytes
    check_safety: Callable[[Runner, Layout], SafetyVerdict] = check_safety
    releases: ReleasesQuery | None = None
    transaction: Callable[[AgentContext, str], TransactionPort] | None = None

    def emit(self, data: Mapping[str, object]) -> None:
        """JSON を 1 行書く。"""
        self.out.write(dump_json(data) + "\n")
        self.out.flush()

    def get_releases(self) -> ReleasesQuery:
        return Releases(self.layout, self.run) if self.releases is None else self.releases

    def make_transaction(self, repo_url: str) -> TransactionPort:
        if self.transaction is not None:
            return self.transaction(self, repo_url)
        return Transaction(self.layout, self.run, repo_url=repo_url, clock=self.clock, now=self.now)

    def lock(self) -> DeployLock:
        return DeployLock(self.layout.lock_file)

    def state_store(self) -> StateStore:
        return StateStore(self.layout)


def default_context() -> AgentContext:
    env = dict(os.environ)
    layout = Layout(env, home=Path.home(), uid=os.getuid())
    return AgentContext(layout=layout, env=env, run=SubprocessRunner(), out=sys.stdout)


# --- 共通の小さな部品 ---


def _parse_target(text: str) -> Target:
    try:
        return target_from_dict(load_json_object(text))
    except ValueError:
        raise UsageError(REASON_BAD_TARGET) from None


def _parse_deploy_id(text: str) -> str:
    try:
        return validate_deploy_id(text)
    except ValueError:
        raise UsageError(REASON_BAD_DEPLOY_ID) from None


def _target_dict(target: Target | None) -> dict[str, object] | None:
    return None if target is None else target_to_dict(target)


def _label(target: Target | None) -> str | None:
    return None if target is None else version_label(target)


def _backup_dict(info: BackupInfo | None) -> dict[str, object] | None:
    if info is None:
        return None
    return {"id": info.backup_id, "path": str(info.path), "size_bytes": info.size_bytes}


def _lock_busy(ctx: AgentContext) -> bool:
    """ロックがほかに取られているか。実機を変えないよう、ロックのファイルがなければ作らない。"""
    if not ctx.layout.lock_file.exists():
        return False
    lock = ctx.lock()
    if not lock.try_acquire():
        return True
    lock.release()
    return False


def _first_deploy(ctx: AgentContext, state: DeployState) -> bool:
    """初めての入れ替えか（サンプルがあり、まだサンプルを保存していない）（9.2）。"""
    return state.sample_saved is None and ctx.layout.sample_exists()


def _running_label(verdict: SafetyVerdict) -> str:
    # systemctl が答えなかったときは、BOTH と unknown の組で返ってくる（動いているとは限らない）
    if verdict.running is Running.BOTH and verdict.reason == "unknown":
        return RUNNING_UNKNOWN
    return verdict.running.value


def _linked(releases: ReleasesQuery, layout: Layout, target: Target) -> bool:
    """`target` の版のディレクトリがあり、current のリンクがそこを指しているか。"""
    assert target.sha is not None
    directory = layout.release_dir(target.sha)
    link = releases.current()
    if link is None or not directory.is_dir():
        return False
    return os.path.realpath(link) == os.path.realpath(directory)


def running_target(ctx: AgentContext, state: DeployState | None, running: Running) -> Target | None:
    """実際に動いている版。記録ではなく、動いているユニットとリンクの先から求める。

    サンプルならサンプル。新しい基盤なら、状態の `current` がリンクの先と同じときだけその版。
    止まっている・分からないときは None。
    """
    if running is Running.SAMPLE:
        return Target(kind="sample", ref=None, ref_kind=None, sha=None)
    if running is not Running.CORE or state is None:
        return None
    current = state.current
    if current is None or current.kind != "release":
        return None
    return current if _linked(ctx.get_releases(), ctx.layout, current) else None


# --- preflight（1.5、1.7、1.9、2.1〜2.4） ---


def _dirty(ctx: AgentContext) -> tuple[int | None, str]:
    """今の版の手の変更の件数と、理由。今の版がなければ (None, "")。"""
    releases = ctx.get_releases()
    current = releases.current()
    if current is None or not current.is_dir():
        return None, ""  # 今の版がない（消えた版には、失う変更もない）
    try:
        count = releases.dirty_count(current)
    except (ReleaseError, CommandError, OSError):
        return None, REASON_DIRTY_UNKNOWN
    return count, REASON_DIRTY if count else ""


def cmd_preflight(ctx: AgentContext, args: argparse.Namespace) -> int:
    target = _parse_target(args.target)
    reasons: list[str] = []

    lock_busy = _lock_busy(ctx)
    if lock_busy:
        reasons.append(REASON_LOCK_BUSY)

    state: DeployState | None
    try:
        state = ctx.state_store().load()
    except StateBroken:
        state = None
        reasons.append(REASON_STATE_BROKEN)

    interrupted = None
    if state is not None and state.in_progress is not None and not lock_busy:
        # ロックが空いているのに途中の印が残っている = 前の入れ替えが途中で終わった
        interrupted = state.in_progress.deploy_id
        reasons.append(REASON_INTERRUPTED)

    verdict = ctx.check_safety(ctx.run, ctx.layout)
    if not verdict.ok:
        reasons.append(verdict.reason)

    first = state is not None and _first_deploy(ctx, state)
    needed = (
        estimate_backup_bytes(ctx.layout, include_sample=first, env=ctx.env)
        + RELEASE_SPACE_MB * MIB
    )
    try:
        free: int | None = ctx.free_bytes(ctx.layout.home)
    except OSError:
        free = None
    disk_ok = free is not None and free >= needed
    if not disk_ok:
        reasons.append(REASON_DISK_LOW)

    dirty, dirty_reason = _dirty(ctx)
    if dirty_reason:
        reasons.append(dirty_reason)

    current = running_target(ctx, state, verdict.running)
    ctx.emit(
        {
            "ok": not reasons,
            "reason": reasons[0] if reasons else "",
            "reasons": reasons,
            "running": _running_label(verdict),
            "current": _target_dict(current),
            "current_label": _label(current),
            "everyday": _target_dict(None if state is None else state.everyday),
            "target": target_to_dict(target),
            "target_label": version_label(target),
            "safety": {"ok": verdict.ok, "reason": verdict.reason, "busy": list(verdict.busy)},
            "disk": {"ok": disk_ok, "free_bytes": free, "needed_bytes": needed},
            "dirty": dirty,
            "lock": "busy" if lock_busy else "free",
            "interrupted": interrupted,
            "first_deploy": first,
        }
    )
    return 0


def _add_preflight(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True, help="入れる版（JSON）")


# --- backup（3.1、3.2、9.2） ---


def cmd_backup(ctx: AgentContext, args: argparse.Namespace) -> int:
    deploy_id = _parse_deploy_id(args.deploy_id)
    lock = ctx.lock()
    if not lock.try_acquire():
        ctx.emit({"ok": False, "reason": REASON_LOCK_BUSY})
        return 0
    try:
        try:
            state = ctx.state_store().load()
        except StateBroken:
            ctx.emit({"ok": False, "reason": REASON_STATE_BROKEN})
            return 0
        try:
            # 途中で止まった復元の残りを、バックアップの前に片付ける
            recover_interrupted_restore(ctx.layout, env=ctx.env)
        except RestoreFailed:
            ctx.emit({"ok": False, "reason": REASON_RESTORE_LEFTOVER})
            return 0
        first = _first_deploy(ctx, state)
        try:
            backup = create_backup(ctx.layout, deploy_id, env=ctx.env)
            prune(ctx.layout)
            sample = create_sample_backup(ctx.layout, deploy_id, ctx.run) if first else None
        except BackupFailed:
            ctx.emit({"ok": False, "reason": REASON_BACKUP_FAILED})
            return 0
        ctx.emit(
            {
                "ok": True,
                "reason": "",
                "first_deploy": first,
                "backup": _backup_dict(backup),
                "sample_backup": _backup_dict(sample),
            }
        )
        return 0
    finally:
        lock.release()


def _add_backup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deploy-id", required=True, help="入れ替えの識別子")


# --- mark-sample-saved（9.2） ---


def cmd_mark_sample_saved(ctx: AgentContext, args: argparse.Namespace) -> int:
    deploy_id = _parse_deploy_id(args.deploy_id)
    lock = ctx.lock()
    if not lock.try_acquire():
        ctx.emit({"ok": False, "reason": REASON_LOCK_BUSY})
        return 0
    try:
        store = ctx.state_store()
        try:
            state = store.load()
        except StateBroken:
            ctx.emit({"ok": False, "reason": REASON_STATE_BROKEN})
            return 0
        if not sample_backup_path(ctx.layout, deploy_id).is_file():
            ctx.emit({"ok": False, "reason": REASON_NO_SAMPLE_BACKUP})
            return 0
        store.save(replace(state, sample_saved=deploy_id))
        ctx.emit({"ok": True, "reason": "", "sample_saved": deploy_id})
        return 0
    finally:
        lock.release()


def _add_mark_sample_saved(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("deploy_id", help="サンプルのバックアップの識別子")


# --- apply（1.7、4.5） ---


def apply_command(
    layout: Layout, target: Target, deploy_id: str, *, everyday: bool, repo_url: str
) -> list[str]:
    """切り離した適用を始める `systemd-run` のコマンド。

    `KillMode=process` で、適用が止めるサービスの道連れにならないようにする。
    `--collect` で、終わったユニット（失敗でも）を残さない。
    """
    argv = [
        "systemd-run",
        "--user",
        f"--unit={DEPLOY_UNIT}",
        "--collect",
        "--property=KillMode=process",
        f"--working-directory={layout.tool_dir}",
        AGENT_PYTHON,
        "-m",
        AGENT_MODULE,
        "run-apply",
        "--target",
        dump_json(target_to_dict(target)),
        "--deploy-id",
        deploy_id,
    ]
    if everyday:
        argv.append("--everyday")
    argv += ["--repo-url", repo_url]
    return argv


def _apply_running(ctx: AgentContext) -> bool | None:
    """切り離した適用が動いているか。分からなければ None。"""
    try:
        return is_active(ctx.run, DEPLOY_UNIT)
    except CommandError:
        return None


def cmd_apply(ctx: AgentContext, args: argparse.Namespace) -> int:
    target = _parse_target(args.target)
    deploy_id = _parse_deploy_id(args.deploy_id)
    running = _apply_running(ctx)
    if running is None:
        ctx.emit({"ok": False, "reason": REASON_APPLY_UNKNOWN})
        return 0
    if running:
        ctx.emit({"ok": False, "reason": REASON_APPLY_RUNNING})
        return 0
    if _lock_busy(ctx):
        ctx.emit({"ok": False, "reason": REASON_LOCK_BUSY})
        return 0
    argv = apply_command(
        ctx.layout, target, deploy_id, everyday=args.everyday, repo_url=args.repo_url
    )
    try:
        ctx.run(argv, timeout=START_TIMEOUT_S, check=True)
    except CommandError:
        ctx.emit({"ok": False, "reason": REASON_START_FAILED})
        return 0
    ctx.emit({"ok": True, "reason": "", "started": True, "deploy_id": deploy_id})
    return 0


def _add_apply_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True, help="入れる版（JSON）")
    parser.add_argument("--deploy-id", required=True, help="入れ替えの識別子")
    parser.add_argument("--everyday", action="store_true", help="入れる版が普段の版")
    parser.add_argument("--repo-url", required=True, help="版を取るリポジトリの URL")


# --- run-apply（4.1〜4.6） ---


def cmd_run_apply(ctx: AgentContext, args: argparse.Namespace) -> int:
    target = _parse_target(args.target)
    deploy_id = _parse_deploy_id(args.deploy_id)
    lock = ctx.lock()
    if not lock.try_acquire():
        # follow が終われるよう、記録の最後に結果の行を書く。実機は変えていない
        log = DeployLog(ctx.layout.log_dir, deploy_id, clock=ctx.now)
        log.event("lock", "failed", REASON_LOCK_BUSY)
        log.result(DeployResult(deploy_id, "aborted", None, (REASON_LOCK_BUSY,)))
        ctx.emit({"ok": False, "reason": REASON_LOCK_BUSY})
        return 0
    try:
        transaction = ctx.make_transaction(args.repo_url)
        try:
            result = transaction.run(target, deploy_id, everyday=args.everyday)
        except Exception as e:
            # 結果は分からない（途中の印は残る）。結果の行は書かず、follow は終わりを見つける
            log = DeployLog(ctx.layout.log_dir, deploy_id, clock=ctx.now)
            log.event("apply", "failed", exception_reason("思わぬ失敗", e))
            raise
        ctx.emit({"ok": True, "reason": "", "result": result_to_dict(result)})
        return 0
    finally:
        lock.release()


# --- follow（4.5、4.6） ---


def _step_line(event: Mapping[str, object]) -> dict[str, object]:
    return {key: event.get(key) for key in ("t", "step", "status", "reason")}


def cmd_follow(ctx: AgentContext, args: argparse.Namespace) -> int:
    """記録を結果の行まで流す。結果の行がないまま終われば、`error` の行で終える。"""
    deploy_id = _parse_deploy_id(args.deploy_id)
    log_dir = ctx.layout.log_dir
    log_path = log_dir / f"{deploy_id}{LOG_SUFFIX}"
    started = ctx.clock.monotonic()
    emitted = 0
    while True:
        events = read_log_events(log_dir, deploy_id)
        for event in events[emitted:]:
            raw = event.get("result")
            if "result" in event:
                # 結果の行は 1 つだけで最後。形が違えば「まだない」と区別して終える
                if not isinstance(raw, dict):
                    ctx.emit({"error": FOLLOW_BROKEN_RESULT})
                    return 0
                try:
                    result = result_from_dict(raw)
                except ValueError:
                    ctx.emit({"error": FOLLOW_BROKEN_RESULT})
                    return 0
                ctx.emit({"t": event.get("t"), "result": result_to_dict(result)})
                return 0
            ctx.emit(_step_line(event))
        emitted = len(events)

        elapsed = ctx.clock.monotonic() - started
        if not log_path.exists():
            if elapsed >= args.start_timeout:
                ctx.emit({"error": FOLLOW_NO_LOG})
                return 0
        elif _apply_running(ctx) is False:
            # 適用が終わった。終わる直前に書いた行を読み落とさないよう、もう一度だけ読む
            if len(read_log_events(log_dir, deploy_id)) > emitted:
                continue
            ctx.emit({"error": FOLLOW_ENDED_WITHOUT_RESULT})
            return 0
        if elapsed >= args.timeout:
            ctx.emit({"error": FOLLOW_TIMEOUT})
            return 0
        ctx.clock.sleep(FOLLOW_POLL_S)


def _add_follow(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("deploy_id", help="入れ替えの識別子")
    parser.add_argument(
        "--timeout", type=float, default=FOLLOW_TIMEOUT_S, help="追跡をやめるまでの秒数"
    )
    parser.add_argument(
        "--start-timeout",
        type=float,
        default=FOLLOW_START_TIMEOUT_S,
        help="記録のファイルができるのを待つ秒数",
    )


# --- そのほかのサブコマンドの理由の名前 ---

REASON_NO_BACKUP = "no_backup"  # その識別子のバックアップがない
REASON_BAD_BACKUP_FILE = "bad_backup_file"  # バックアップの置き場所の外のファイル
REASON_BACKUPS_UNREADABLE = "backups_unreadable"  # バックアップの一覧を読めない
REASON_RESTORE_FAILED = "restore_failed"  # バックアップから戻せない
REASON_STOP_FAILED = "stop_failed"  # 動いているサービスを止められない
REASON_START_FAILED_AFTER = "start_failed_after"  # 戻した後にサービスを起動できない
REASON_HALT_FAILED = "halt_failed"  # 停止のどこかが失敗した
REASON_NO_RELEASE = "no_release"  # 今の版がない
REASON_SELF_CHECK_ERROR = "self_check_error"  # self-check を行えない
REASON_SELF_CHECK_UNREADABLE = "self_check_unreadable"  # self-check の結果が読めない
REASON_RUNNING_UNKNOWN = "running_unknown"  # 何が動いているか分からない
REASON_NOTICE_FAILED = "notice_failed"  # 知らせを書けない
REASON_PROVISION_FAILED = "provision_failed"  # 初期設定の手順が失敗した


# --- 版の確認（5.4、7.2） ---


def _last_deploy_dict(state: DeployState) -> dict[str, object] | None:
    last = state.last_deploy
    if last is None:
        return None
    return {"id": last.deploy_id, "outcome": last.outcome, "finished_at": last.finished_at}


def cmd_status(ctx: AgentContext, args: argparse.Namespace) -> int:
    """動いている版・普段の版・最後の入れ替え・試しの経過を返す。実機を変えない。"""
    state: DeployState | None
    try:
        state = ctx.state_store().load()
    except StateBroken:
        state = None
    verdict = ctx.check_safety(ctx.run, ctx.layout)
    current = running_target(ctx, state, verdict.running)
    everyday = None if state is None else state.everyday
    trial_since = None if state is None else state.trial_since
    is_trial = current is not None and current != everyday
    elapsed = None if trial_since is None or not is_trial else max(ctx.now() - trial_since, 0.0)
    ctx.emit(
        {
            "ok": state is not None,
            "reason": "" if state is not None else REASON_STATE_BROKEN,
            "running": _running_label(verdict),
            "current": _target_dict(current),
            "current_label": _label(current),
            "everyday": _target_dict(everyday),
            "everyday_label": _label(everyday),
            "is_trial": is_trial,
            "trial_since": trial_since if is_trial else None,
            "trial_elapsed_s": elapsed,
            "reminded_at": None if state is None else state.reminded_at,
            "last_deploy": None if state is None else _last_deploy_dict(state),
            "in_progress": None
            if state is None or state.in_progress is None
            else state.in_progress.deploy_id,
            "lock": "busy" if _lock_busy(ctx) else "free",
        }
    )
    return 0


# --- 普段の版の記録（5.2） ---


def cmd_set_everyday(ctx: AgentContext, args: argparse.Namespace) -> int:
    """普段の版を記録する。入れ替えはしない（入れ替えは開発 PC の back で行う）。

    今の版が新しい普段の版と同じになれば試しは終わりなので、試しの時刻と戻し忘れの知らせを消す。
    違う版になれば、試しを始めた時刻がなければ今から数える。
    """
    target = _parse_target(args.target)
    lock = ctx.lock()
    if not lock.try_acquire():
        ctx.emit({"ok": False, "reason": REASON_LOCK_BUSY})
        return 0
    try:
        store = ctx.state_store()
        try:
            state = store.load()
        except StateBroken:
            ctx.emit({"ok": False, "reason": REASON_STATE_BROKEN})
            return 0
        trial = state.current is not None and state.current != target
        if trial:
            since = ctx.now() if state.trial_since is None else state.trial_since
            new_state = replace(state, everyday=target, trial_since=since)
        else:
            new_state = replace(state, everyday=target, trial_since=None, reminded_at=None)
            if state.reminded_at is not None:
                clear_notice(ctx.layout.runtime_dir)
        store.save(new_state)
        ctx.emit(
            {
                "ok": True,
                "reason": "",
                "everyday": target_to_dict(target),
                "everyday_label": version_label(target),
            }
        )
        return 0
    finally:
        lock.release()


def _add_set_everyday(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True, help="普段の版（JSON。サンプルか版）")


# --- self-check（6.7、6.8） ---

SELF_CHECK_PYTHON = Path(".venv/bin/python")


def _self_check_results(text: str, returncode: int) -> list[dict[str, object]] | None:
    """self-check の `--json` の出力から項目の一覧を返す。形が違えば None。

    形の確かめは入れた後の確認と同じ `parse_self_check` で行う。理由は基盤の決まった文言だけ。
    """
    failed = parse_self_check(text)
    if failed is None or returncode != (1 if failed else 0):
        return None
    data = load_json_object(text)
    raw = data["results"]
    assert isinstance(raw, list)
    return [
        {"name": item["name"], "status": item["status"], "reason": item["reason"]} for item in raw
    ]


def cmd_self_check(ctx: AgentContext, args: argparse.Namespace) -> int:
    """今の版の self-check を行う（6.8）。

    サンプルが動いているかもしれないときは `--no-hardware` を付ける（サンプルは単一の実行の
    ロックを使わないので、ハードを取り合わないように）。基盤が動いていれば、ハードの項目は
    基盤の報告で判定される（6.7）。入れ替えの最中にサービスを止めないよう、ロックを持って行う。
    """
    lock = ctx.lock()
    if not lock.try_acquire():
        ctx.emit({"ok": False, "reason": REASON_LOCK_BUSY})
        return 0
    try:
        current = ctx.get_releases().current()
        if current is None or not current.is_dir():
            ctx.emit({"ok": False, "reason": REASON_NO_RELEASE})
            return 0
        verdict = ctx.check_safety(ctx.run, ctx.layout)
        argv = [str(current / SELF_CHECK_PYTHON), "-m", "osekkai", "--self-check", "--json"]
        no_hardware = verdict.running in (Running.SAMPLE, Running.BOTH)
        if no_hardware:
            argv.append("--no-hardware")
        try:
            result = ctx.run(argv, timeout=SELF_CHECK_TIMEOUT_S, cwd=current)
        except CommandError:
            ctx.emit({"ok": False, "reason": REASON_SELF_CHECK_ERROR})
            return 0
        results = _self_check_results(result.text, result.returncode)
        if results is None:
            ctx.emit({"ok": False, "reason": REASON_SELF_CHECK_UNREADABLE})
            return 0
        passed = all(item["status"] != "failed" for item in results)
        ctx.emit(
            {
                "ok": passed,
                "reason": "",
                "no_hardware": no_hardware,
                "running": _running_label(verdict),
                "results": results,
            }
        )
        return 0
    finally:
        lock.release()


def _add_self_check(parser: argparse.ArgumentParser) -> None:
    # 出力はいつも JSON。開発 PC の呼び方に合わせて受け付けるだけ
    parser.add_argument("--json", action="store_true", help="JSON で返す（いつも JSON）")


# --- バックアップの一覧と復元（3.7） ---


def cmd_backups(ctx: AgentContext, args: argparse.Namespace) -> int:
    """普段のバックアップの一覧（新しい順）。場所は返さない。"""
    try:
        found = list_backups(ctx.layout)
    except BackupFailed:
        ctx.emit({"ok": False, "reason": REASON_BACKUPS_UNREADABLE})
        return 0
    ctx.emit(
        {
            "ok": True,
            "reason": "",
            "backups": [{"id": info.backup_id, "size_bytes": info.size_bytes} for info in found],
        }
    )
    return 0


def _restore_source(ctx: AgentContext, args: argparse.Namespace) -> Path:
    """戻すバックアップのファイル。`--file` は実機のバックアップの置き場所の中だけを受け付ける。"""
    if args.file is not None:
        backups_dir = os.path.realpath(ctx.layout.backups_dir)
        path = os.path.realpath(args.file)
        if os.path.dirname(path) != backups_dir:
            raise UsageError(REASON_BAD_BACKUP_FILE)
        return Path(path)
    backup_id = _parse_deploy_id(args.backup_id)
    return backup_path(ctx.layout, backup_id)


def _running_units(running: Running) -> list[str]:
    """動いているユニットの名前。"""
    units: list[str] = []
    if running in (Running.CORE, Running.BOTH):
        units.append(CORE_UNIT_NAME)
    if running in (Running.SAMPLE, Running.BOTH):
        units.append(SAMPLE_UNIT_NAME)
    return units


def _start_units(ctx: AgentContext, units: Sequence[str]) -> list[str]:
    """ユニットを順に起動し、起動できたものを返す。"""
    started: list[str] = []
    for unit in units:
        try:
            start_unit(ctx.run, unit)
        except CommandError:
            continue
        started.append(unit)
    return started


def cmd_restore(ctx: AgentContext, args: argparse.Namespace) -> int:
    """バックアップから戻す（3.7）。

    安全の確認 → 今の状態のバックアップ → 動いているサービスを止める → 中身を元の場所に戻す
    （サンプルのデータは `--include-sample` のときだけ）→ 止めたサービスを起動する。
    """
    source = _restore_source(ctx, args)
    lock = ctx.lock()
    if not lock.try_acquire():
        ctx.emit({"ok": False, "reason": REASON_LOCK_BUSY})
        return 0
    try:
        report: dict[str, object] = {
            "ok": False,
            "reason": "",
            "include_sample": args.include_sample,
            "pre_backup": None,
            "stopped": [],
            "started": [],
        }

        def finish(reason: str) -> int:
            report["ok"] = not reason
            report["reason"] = reason
            ctx.emit(report)
            return 0

        if not source.is_file():
            return finish(REASON_NO_BACKUP)
        verdict = ctx.check_safety(ctx.run, ctx.layout)
        if not verdict.ok:
            return finish(verdict.reason)

        # 今の状態を残す。戻すファイルを上書きしないよう、同じ名前があれば作らない
        pre_id = make_deploy_id(ctx.now())
        if backup_path(ctx.layout, pre_id).exists():
            return finish(REASON_BACKUP_FAILED)
        try:
            pre = create_backup(ctx.layout, pre_id, env=ctx.env)
        except BackupFailed:
            return finish(REASON_BACKUP_FAILED)
        report["pre_backup"] = {"id": pre.backup_id, "size_bytes": pre.size_bytes}

        stopped: list[str] = []
        report["stopped"] = stopped
        for unit in _running_units(verdict.running):
            try:
                stop_unit(ctx.run, unit)
            except CommandError:
                report["started"] = _start_units(ctx, stopped)
                return finish(REASON_STOP_FAILED)
            stopped.append(unit)

        reason = ""
        try:
            restore_backup(ctx.layout, source, include_sample=args.include_sample, env=ctx.env)
        except RestoreFailed:
            reason = REASON_RESTORE_FAILED
            try:
                # 元の場所がない状態で起動しないよう、途中の残りを片付けてから起動する
                recover_interrupted_restore(ctx.layout, env=ctx.env)
            except RestoreFailed:
                return finish(REASON_RESTORE_LEFTOVER)

        started = _start_units(ctx, stopped)
        report["started"] = started
        if not reason and len(started) != len(stopped):
            reason = REASON_START_FAILED_AFTER
        return finish(reason)
    finally:
        lock.release()


def _add_restore(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("backup_id", nargs="?", help="戻すバックアップの識別子")
    group.add_argument("--file", help="実機のバックアップの置き場所に送ったファイル")
    parser.add_argument("--include-sample", action="store_true", help="サンプルのデータも戻す")


# --- 停止（11.2） ---


def cmd_stop(ctx: AgentContext, args: argparse.Namespace) -> int:
    """両方のユニットを止め、安全停止を `--strict` で行う。失敗した段階の名前を返す。"""
    lock = ctx.lock()
    if not lock.try_acquire():
        ctx.emit({"ok": False, "reason": REASON_LOCK_BUSY})
        return 0
    try:
        # 停止だけを使うので、リポジトリの URL は要らない
        failed = Transaction(ctx.layout, ctx.run, repo_url="", clock=ctx.clock, now=ctx.now).halt()
        ctx.emit(
            {"ok": not failed, "reason": REASON_HALT_FAILED if failed else "", "failed": failed}
        )
        return 0
    finally:
        lock.release()


# --- 戻し忘れ（5.5） ---


def cmd_remind(ctx: AgentContext, args: argparse.Namespace) -> int:
    """タイマーから呼ぶ。試しの版が長く入っていれば、LCD に知らせて時刻を記録する。

    入れ替えの最中（ロックが取られている）や、分からないことがあれば、何もしない。
    """
    lock = ctx.lock()
    if not lock.try_acquire():
        ctx.emit({"ok": True, "reason": REASON_LOCK_BUSY, "reminded": False})
        return 0
    try:
        store = ctx.state_store()
        try:
            state = store.load()
        except StateBroken:
            ctx.emit({"ok": False, "reason": REASON_STATE_BROKEN, "reminded": False})
            return 0
        try:
            running = detect_running(ctx.run)
        except CommandError:
            ctx.emit({"ok": False, "reason": REASON_RUNNING_UNKNOWN, "reminded": False})
            return 0
        now = ctx.now()
        if not remind(state, running, now):
            ctx.emit({"ok": True, "reason": "", "reminded": False})
            return 0
        try:
            new_state = send_reminder(ctx.layout.runtime_dir, state, now)
        except OSError:
            ctx.emit({"ok": False, "reason": REASON_NOTICE_FAILED, "reminded": False})
            return 0
        store.save(new_state)
        ctx.emit({"ok": True, "reason": "", "reminded": True})
        return 0
    finally:
        lock.release()


# --- 初期設定（8.1〜8.8） ---


def _user_name(env: Mapping[str, str]) -> str:
    name = env.get("USER") or env.get("LOGNAME")
    return name if name else pwd.getpwuid(os.getuid()).pw_name


def _say(step: str, text: str) -> None:
    # 標準出力は JSON だけにするので、人に向けた経過は標準エラーに出す
    sys.stderr.write(f"[{step}] {text}\n")
    sys.stderr.flush()


def cmd_provision(ctx: AgentContext, args: argparse.Namespace) -> int:
    """初期設定を行う。経過は標準エラーに出し、最後に結果の JSON を 1 つ返す。"""
    target = None if args.target is None else _parse_target(args.target)
    lock = ctx.lock()
    if not lock.try_acquire():
        ctx.emit({"ok": False, "reason": REASON_LOCK_BUSY})
        return 0
    try:
        pctx = ProvisionContext(
            run=ctx.run,
            layout=ctx.layout,
            releases=Releases(ctx.layout, ctx.run),
            user=_user_name(ctx.env),
            repo_url=args.repo_url,
            target=target,
            data_dir=core_data_dir(ctx.layout, ctx.env),
            say=_say,
            now=ctx.now,
        )
        report = run_provision(provision_steps(pctx), _say)
        ctx.emit(
            {
                "ok": report.ok,
                "reason": "" if report.ok else REASON_PROVISION_FAILED,
                "skipped": list(report.skipped),
                "applied": list(report.applied),
                "failed_step": report.failed_step,
                "retry_hint": report.retry_hint,
                "needs_reboot": report.needs_reboot,
                "self_check_ok": report.self_check_ok,
            }
        )
        return 0
    finally:
        lock.release()


def _add_provision(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", help="最初の版（JSON）。版のディレクトリがないときに使う")
    parser.add_argument("--repo-url", required=True, help="版を取るリポジトリの URL")


# --- 入れ替えの記録の表示（4.6） ---


def cmd_log(ctx: AgentContext, args: argparse.Namespace) -> int:
    """入れ替えの記録を返す。識別子がなければ最新の 1 件。`--list` なら識別子の一覧（新しい順）。"""
    log_dir = ctx.layout.log_dir
    if args.list:
        ctx.emit({"ok": True, "reason": "", "ids": list(reversed(list_log_ids(log_dir)))})
        return 0
    deploy_id = (
        latest_log_id(log_dir) if args.deploy_id is None else _parse_deploy_id(args.deploy_id)
    )
    if deploy_id is None or not (log_dir / f"{deploy_id}{LOG_SUFFIX}").is_file():
        ctx.emit({"ok": False, "reason": FOLLOW_NO_LOG})
        return 0
    events = [
        _step_line(event) for event in read_log_events(log_dir, deploy_id) if "result" not in event
    ]
    result = read_log_result(log_dir, deploy_id)
    ctx.emit(
        {
            "ok": True,
            "reason": "",
            "deploy_id": deploy_id,
            "events": events,
            "result": None if result is None else result_to_dict(result),
        }
    )
    return 0


def _add_log(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("deploy_id", nargs="?", help="入れ替えの識別子（なければ最新）")
    parser.add_argument("--list", action="store_true", help="識別子の一覧を返す")


def _no_arguments(parser: argparse.ArgumentParser) -> None:
    return None


# --- 入口 ---

Handler = Callable[[AgentContext, argparse.Namespace], int]


@dataclass(frozen=True)
class Subcommand:
    name: str
    help: str
    configure: Callable[[argparse.ArgumentParser], None]
    handler: Handler


def _handlers() -> list[Subcommand]:
    """サブコマンドの表。足すときはここに並べる。"""
    return [
        Subcommand("preflight", "入れる前の確認（実機を変えない）", _add_preflight, cmd_preflight),
        Subcommand("backup", "バックアップを作る", _add_backup, cmd_backup),
        Subcommand(
            "mark-sample-saved",
            "サンプルのバックアップを受け取れたことを記録する",
            _add_mark_sample_saved,
            cmd_mark_sample_saved,
        ),
        Subcommand("apply", "適用を切り離して始める", _add_apply_arguments, cmd_apply),
        Subcommand(
            "run-apply", "切り離された適用（apply から呼ぶ）", _add_apply_arguments, cmd_run_apply
        ),
        Subcommand("follow", "入れ替えの記録を結果まで流す", _add_follow, cmd_follow),
        Subcommand("status", "版の確認（実機を変えない）", _no_arguments, cmd_status),
        Subcommand("set-everyday", "普段の版を記録する", _add_set_everyday, cmd_set_everyday),
        Subcommand("self-check", "今の版の self-check を行う", _add_self_check, cmd_self_check),
        Subcommand("backups", "バックアップの一覧", _no_arguments, cmd_backups),
        Subcommand("restore", "バックアップから戻す", _add_restore, cmd_restore),
        Subcommand("stop", "両方のサービスを止め、厳しい安全停止を行う", _no_arguments, cmd_stop),
        Subcommand("remind", "戻し忘れを確かめる（タイマーから呼ぶ）", _no_arguments, cmd_remind),
        Subcommand("provision", "初期設定", _add_provision, cmd_provision),
        Subcommand("log", "入れ替えの記録を表示する", _add_log, cmd_log),
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rover_deploy.agent", description="実機の道具（開発 PC の rover-deploy から呼ぶ）"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for sub in _handlers():
        child = subparsers.add_parser(sub.name, help=sub.help)
        sub.configure(child)
        child.set_defaults(handler=sub.handler)
    return parser


def main(argv: Sequence[str] | None = None, *, context: AgentContext | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else USAGE_ERROR
    ctx = default_context() if context is None else context
    handler: Handler = args.handler
    try:
        return handler(ctx, args)
    except UsageError as e:
        ctx.emit({"ok": False, "reason": e.reason})
        return USAGE_ERROR
    except Exception as e:
        # 例外の文言は写さない（10.3）
        ctx.emit({"ok": False, "reason": REASON_UNEXPECTED, "error": type(e).__name__})
        return UNEXPECTED_ERROR


if __name__ == "__main__":
    sys.exit(main())
