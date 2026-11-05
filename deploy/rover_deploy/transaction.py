"""適用の流れ（1.1、1.2、1.10、3.6、4.1〜4.4、4.6、5.1、5.3、9.4、9.5）。

準備 → 停止の直前の安全の確認 → 切り替え → 確認 → 失敗なら前の版で同じ手順と確認 →
それも失敗なら両方のユニットを止めて、厳しい安全停止（`--strict`）を行う。

- 準備（Preparing）: 対象が版なら、版のディレクトリを用意する。今の版にも、動いているサービスにも
  触れない。失敗したら中止する
- 安全の確認（Rechecking）: 走行中・作業中・確かめられない・サンプルが動いている → 中止
- 切り替え（Switching）: 初回だけ首の調整値を移し、動いているサービスを止め、リンクとユニットを
  入れ替え、知らせを書き、相手のユニットが止まっていることを確かめてから起動する
- 確認（Verifying）: 版なら `verify_release`、サンプルなら `verify_sample`
- 有効にするのは普段の版のユニットだけ。確認がよいと分かってから行う（切り戻しのときに、
  前の有効の設定を変えずに済む）。試しの版は有効にしない（再起動すれば普段の版に戻る）

始めに状態に `in_progress` を書き、終わったら（どの結果でも）消す。途中でプロセスが止まると
残るので、次の preflight は「前の入れ替えが途中で終わった」と分かる。
記録には、手順の名前・固定の理由・例外の型の名前・確認の項目の名前だけを書く（10.3）。

結果の `failed_checks` の名前:
- 中止: "prepare:<段階>"・"prepare"・"safety:<理由>"・"busy:<機能の名前>"
- 確認の失敗: 確認の項目の名前（"alive" など）。切り替えの失敗は "switch"、起動の失敗は "start"、
  相手のユニットが動いていれば "conflict"、確認そのものが例外で終われば "verify"
- 切り戻しの失敗: "rollback:<項目>"（前の版がなければ "rollback:no_previous"）
- 停止の失敗: "halt:<段階>"（"stop_core"・"stop_sample"・"safe_stop"）
- 成功したが普段の版を有効にできなかった: "enable"（再起動で別の版になりうる）
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from rover_deploy.calibration import MigrationResult, migrate
from rover_deploy.common import DeployResult, Outcome, Running, Target
from rover_deploy.layout import CORE_UNIT_NAME, SAMPLE_UNIT_NAME, Layout
from rover_deploy.notices import clear_notice, version_notice, write_notice
from rover_deploy.releases import PrepareFailed, ReleaseError, Releases
from rover_deploy.service_status import SafetyVerdict, check_safety, detect_running
from rover_deploy.state import (
    DeployLog,
    DeployState,
    InProgress,
    LastDeploy,
    StateStore,
    exception_reason,
)
from rover_deploy.system import (
    CommandError,
    Runner,
    disable_unit,
    enable_unit,
    is_active,
    start_unit,
    stop_unit,
)
from rover_deploy.verify import Clock, SystemClock, VerifyReport, verify_release, verify_sample

SAFE_STOP_TIMEOUT_S = 60.0  # 厳しい安全停止を待つ時間
SAMPLE_CONF = Path("data/picar-x.conf")  # サンプルの作業コピーの中の調整値のファイル
CORE_CONFIG_FILE_NAME = "config.yaml"  # 基盤の設定（rover-core の Paths と同じ名前）
EXAMPLE_CONFIG = Path("src/osekkai/core/example_config.yaml")  # 版の中の設定の見本
VENV_PYTHON = Path(".venv/bin/python")

SAMPLE_TARGET = Target(kind="sample", ref=None, ref_kind=None, sha=None)

FAILED_SWITCH = "switch"
FAILED_START = "start"
FAILED_VERIFY = "verify"
FAILED_CONFLICT = "conflict"
FAILED_NO_PREVIOUS = "no_previous"
FAILED_ENABLE = "enable"
HALT_STOP_CORE = "stop_core"
HALT_STOP_SAMPLE = "stop_sample"
HALT_SAFE_STOP = "safe_stop"

# 切り替えの手順で受け止める失敗（ほかの例外は、途中の印を残したまま上へ投げる）
_SWITCH_ERRORS = (ReleaseError, CommandError, OSError)


class ReleasesPort(Protocol):
    """適用の流れが使う `Releases` の口。"""

    def prepare(self, target: Target, repo_url: str) -> Path: ...

    def current(self) -> Path | None: ...

    def switch(self, release_dir: Path) -> Path | None: ...

    def install_unit(self, release_dir: Path) -> None: ...

    def prune(self, keep: Collection[Path]) -> None: ...


class VerifyRelease(Protocol):
    def __call__(
        self,
        release_dir: Path,
        run: Runner,
        clock: Clock,
        layout: Layout,
        *,
        since_epoch: float,
        expected_describe: str | None = None,
    ) -> VerifyReport: ...


class VerifySample(Protocol):
    def __call__(self, run: Runner, clock: Clock) -> VerifyReport: ...


class CheckSafety(Protocol):
    def __call__(self, run: Runner, layout: Layout) -> SafetyVerdict: ...


Migrate = Callable[[Path, Path, Path], MigrationResult]


@dataclass(frozen=True)
class _Ending:
    """流れの終わり方。状態に書く値と、結果。"""

    outcome: Outcome
    running: Target | None
    failed: tuple[str, ...]
    state: DeployState


class Transaction:
    """1 回の入れ替え。`run()` は `agent run-apply` から、`halt()` は `agent stop` からも呼ぶ。

    ロックは呼ぶ側が持つ。
    """

    def __init__(
        self,
        layout: Layout,
        run: Runner,
        *,
        repo_url: str,
        releases: ReleasesPort | None = None,
        state_store: StateStore | None = None,
        clock: Clock | None = None,
        now: Callable[[], float] = time.time,
        check_safety: CheckSafety = check_safety,
        verify_release: VerifyRelease = verify_release,
        verify_sample: VerifySample = verify_sample,
        migrate: Migrate = migrate,
    ) -> None:
        self._layout = layout
        self._run = run
        self._repo_url = repo_url
        self._releases: ReleasesPort = Releases(layout, run) if releases is None else releases
        self._store = StateStore(layout) if state_store is None else state_store
        self._clock: Clock = SystemClock() if clock is None else clock
        self._now = now
        self._check_safety = check_safety
        self._verify_release = verify_release
        self._verify_sample = verify_sample
        self._migrate = migrate

    # --- 全体 ---

    def run(self, target: Target, deploy_id: str, *, everyday: bool) -> DeployResult:
        """`target` を入れる。`everyday` は、入れる版が普段の版かどうか。"""
        log = DeployLog(self._layout.log_dir, deploy_id, clock=self._now)
        state = self._store.load()
        running = self._detect_running()
        previous = self._previous(state, running)
        self._store.save(replace(state, in_progress=InProgress(deploy_id, previous)))

        ending = self._apply(log, state, target, previous, running, everyday=everyday)

        result = DeployResult(
            deploy_id=deploy_id,
            outcome=ending.outcome,
            running=ending.running,
            failed_checks=ending.failed,
        )
        self._store.save(
            replace(
                ending.state,
                in_progress=None,
                last_deploy=LastDeploy(deploy_id, ending.outcome, self._now()),
            )
        )
        log.result(result)
        return result

    def _apply(
        self,
        log: DeployLog,
        state: DeployState,
        target: Target,
        previous: Target | None,
        running: Running,
        *,
        everyday: bool,
    ) -> _Ending:
        unchanged = previous if running in (Running.CORE, Running.SAMPLE) else None

        def aborted(failed: tuple[str, ...]) -> _Ending:
            return _Ending("aborted", unchanged, failed, state)

        # 準備（今の版にも、動いているサービスにも触れない）
        release_dir: Path | None = None
        if target.kind == "release":
            log.event("prepare", "start")
            try:
                release_dir = self._releases.prepare(target, self._repo_url)
            except PrepareFailed as e:
                log.event("prepare", "failed", e.step)
                return aborted((f"prepare:{e.step}",))
            except _SWITCH_ERRORS as e:
                log.event("prepare", "failed", exception_reason("準備に失敗", e))
                return aborted(("prepare",))
            log.event("prepare", "ok")

        # 停止の直前の安全の確認
        log.event("recheck", "start")
        verdict = self._check_safety(self._run, self._layout)
        if not verdict.ok:
            log.event("recheck", "failed", verdict.reason)
            busy = tuple(f"busy:{name}" for name in verdict.busy)
            return aborted((f"safety:{verdict.reason}", *busy))
        log.event("recheck", "ok")

        # ここから実機を変える
        calibration = state.calibration_migrated
        if target.kind == "release" and calibration is None:
            assert release_dir is not None
            calibration = self._migrate_calibration(log, release_dir)
        state = replace(state, calibration_migrated=calibration)

        everyday_target = target if everyday else state.everyday
        report = self._bring_up(log, "", target, release_dir, everyday_target)
        if report.ok:
            return self._succeeded(log, state, target, previous, everyday_target, everyday=everyday)

        # 切り戻し（4.2、4.3）
        failed = report.failed
        if previous is None:
            log.event("rollback", "failed", "前の版がない")
            rollback_failed: tuple[str, ...] = (FAILED_NO_PREVIOUS,)
        else:
            log.event("rollback", "start")
            previous_dir = None
            if previous.kind == "release":
                assert previous.sha is not None
                previous_dir = self._layout.release_dir(previous.sha)
            back = self._bring_up(log, "rollback.", previous, previous_dir, state.everyday)
            if back.ok:
                log.event("rollback", "ok")
                return _Ending("rolled_back", previous, failed, self._rolled_back(state, previous))
            log.event("rollback", "failed")
            rollback_failed = back.failed

        # 停止（4.4）
        log.event("halt", "start")
        halt_failed = self.halt()
        log.event("halt", "failed" if halt_failed else "ok", ",".join(halt_failed))
        halted_state = replace(
            state, current=None, previous=previous, trial_since=None, reminded_at=None
        )
        return _Ending(
            "halted",
            None,
            (
                *failed,
                *(f"rollback:{name}" for name in rollback_failed),
                *(f"halt:{name}" for name in halt_failed),
            ),
            halted_state,
        )

    # --- 前の版 ---

    def _detect_running(self) -> Running:
        try:
            return detect_running(self._run)
        except CommandError:
            return Running.BOTH  # 分からない。安全の確認で中止になる

    def _linked(self, target: Target) -> bool:
        """`target` の版のディレクトリがあり、current のリンクがそこを指しているか。"""
        assert target.sha is not None
        directory = self._layout.release_dir(target.sha)
        link = self._releases.current()
        if link is None or not directory.is_dir():
            return False
        return os.path.realpath(link) == os.path.realpath(directory)

    def _previous(self, state: DeployState, running: Running) -> Target | None:
        """切り戻す先（入れる前の版）。分からなければ None（切り戻さずに停止する）。

        何が動いているかは実際のユニットで、版の名前は状態の `current` で決める。
        新しい基盤なら、状態の `current` がリンクの先と同じときだけ、その版に戻す。
        どちらも止まっていれば、状態の `current` に戻す（まだなければサンプル）。
        """
        sample = SAMPLE_TARGET if self._layout.sample_exists() else None
        if running is Running.SAMPLE:
            return SAMPLE_TARGET
        current = state.current
        if current is not None and current.kind == "release":
            return current if self._linked(current) else None
        if running is Running.CORE:
            return None  # 動いている版の名前が分からない
        return sample

    # --- 切り替え ---

    def _migrate_calibration(self, log: DeployLog, release_dir: Path) -> str | None:
        """首の調整値を移す（9.3）。どの結果でも入れ替えは続ける。"""
        log.event("calibration", "start")
        try:
            result = self._migrate(
                self._layout.sample_dir / SAMPLE_CONF,
                self._layout.core_config_dir / CORE_CONFIG_FILE_NAME,
                release_dir / EXAMPLE_CONFIG,
            )
        except OSError as e:
            log.event("calibration", "failed", exception_reason("調整値を移せない", e))
            return None  # 次の入れ替えでやり直す
        log.event("calibration", "ok", result.status)
        return result.status

    def _stop_running(self) -> None:
        """動いているユニットを止める（新しい基盤は ExecStopPost で安全停止する）。"""
        for unit in (CORE_UNIT_NAME, SAMPLE_UNIT_NAME):
            if is_active(self._run, unit):
                stop_unit(self._run, unit)

    def _bring_up(
        self,
        log: DeployLog,
        prefix: str,
        target: Target,
        release_dir: Path | None,
        everyday: Target | None,
    ) -> VerifyReport:
        """動いているものを止め、`target` に切り替えて起動し、確かめる。"""
        step = f"{prefix}switch"
        log.event(step, "start")
        try:
            self._stop_running()
            if target.kind == "release":
                assert release_dir is not None
                self._releases.switch(release_dir)
                self._releases.install_unit(release_dir)
                write_notice(
                    self._layout.runtime_dir, version_notice(target, everyday, self._now())
                )
                own, other = CORE_UNIT_NAME, SAMPLE_UNIT_NAME
            else:
                clear_notice(self._layout.runtime_dir)  # 戻し忘れの知らせも消える
                own, other = SAMPLE_UNIT_NAME, CORE_UNIT_NAME
            # 2 つが同時に動かないように、起動の前に相手が止まっていることを確かめる（9.4、9.5）
            if is_active(self._run, other):
                log.event(step, "failed", "相手のユニットが動いている")
                return VerifyReport(ok=False, failed=(FAILED_CONFLICT,))
        except _SWITCH_ERRORS as e:
            log.event(step, "failed", exception_reason("切り替えに失敗", e))
            return VerifyReport(ok=False, failed=(FAILED_SWITCH,))
        log.event(step, "ok")

        step = f"{prefix}start"
        log.event(step, "start")
        since = self._now()  # 版のログを探す起点。起動の直前の壁時計の時刻
        try:
            start_unit(self._run, own)
        except CommandError as e:
            log.event(step, "failed", exception_reason("起動に失敗", e))
            return VerifyReport(ok=False, failed=(FAILED_START,))
        log.event(step, "ok")

        step = f"{prefix}verify"
        log.event(step, "start")
        try:
            if release_dir is not None:
                report = self._verify_release(
                    release_dir, self._run, self._clock, self._layout, since_epoch=since
                )
            else:
                report = self._verify_sample(self._run, self._clock)
        except Exception as e:
            # 起動した版を確かめないまま動かしておかない。確認の失敗として切り戻す
            log.event(step, "failed", exception_reason("確認に失敗", e))
            return VerifyReport(ok=False, failed=(FAILED_VERIFY,))
        log.event(step, "ok" if report.ok else "failed", ",".join(report.failed))
        return report

    # --- 終わり方 ---

    def _enable_everyday(self, log: DeployLog, target: Target) -> bool:
        """普段の版のユニットだけを有効にする。ユニットのファイルがない側は無効にしない。"""
        if target.kind == "release":
            own, other, other_file = (
                CORE_UNIT_NAME,
                SAMPLE_UNIT_NAME,
                self._layout.sample_unit_file,
            )
        else:
            own, other, other_file = (
                SAMPLE_UNIT_NAME,
                CORE_UNIT_NAME,
                self._layout.core_unit_file,
            )
        log.event("enable", "start")
        try:
            enable_unit(self._run, own)
            if other_file.exists():
                disable_unit(self._run, other)
        except CommandError as e:
            log.event("enable", "failed", exception_reason("有効にできない", e))
            return False
        log.event("enable", "ok")
        return True

    def _prune(self, log: DeployLog, keep_targets: Collection[Target | None]) -> None:
        """古い版を消す。失敗しても入れ替えの結果は変えない。"""
        keep = {
            self._layout.release_dir(t.sha)
            for t in keep_targets
            if t is not None and t.kind == "release" and t.sha is not None
        }
        log.event("prune", "start")
        try:
            self._releases.prune(keep)
        except _SWITCH_ERRORS as e:
            log.event("prune", "failed", exception_reason("古い版を消せない", e))
            return
        log.event("prune", "ok")

    def _succeeded(
        self,
        log: DeployLog,
        state: DeployState,
        target: Target,
        previous: Target | None,
        everyday_target: Target | None,
        *,
        everyday: bool,
    ) -> _Ending:
        failed: tuple[str, ...] = ()
        if everyday and not self._enable_everyday(log, target):
            failed = (FAILED_ENABLE,)
        trial = target != everyday_target
        new_state = replace(
            state,
            current=target,
            previous=previous,
            trial_since=self._now() if trial else None,
            reminded_at=None,
        )
        self._prune(log, (target, previous, state.everyday))
        return _Ending("succeeded", target, failed, new_state)

    def _rolled_back(self, state: DeployState, previous: Target) -> DeployState:
        if previous == state.current:
            return state  # 前の版のまま。試しを始めた時刻も続く
        trial = previous != state.everyday
        return replace(
            state,
            current=previous,
            trial_since=self._now() if trial else None,
            reminded_at=None,
        )

    # --- 停止（4.4） ---

    def halt(self) -> list[str]:
        """両方のユニットを止め、安全停止を `--strict` で行う。失敗した段階の名前を返す。

        ユニットのファイルがない側は、動いていないとみなして止めない。

        安全停止は今の版（current のリンクの先）の仮想環境で行う。今の版がなければ、
        モーターが止まったと確かめられないので "safe_stop" を失敗に入れる。
        """
        failed: list[str] = []
        units = (
            (CORE_UNIT_NAME, self._layout.core_unit_file, HALT_STOP_CORE),
            (SAMPLE_UNIT_NAME, self._layout.sample_unit_file, HALT_STOP_SAMPLE),
        )
        for unit, unit_file, name in units:
            if not unit_file.exists():
                continue  # ユニットのファイルがなければ動いていない（stop は 5 で失敗する）
            try:
                stop_unit(self._run, unit)
            except CommandError:
                failed.append(name)
        if not self._strict_safe_stop():
            failed.append(HALT_SAFE_STOP)
        return failed

    def _strict_safe_stop(self) -> bool:
        current = self._releases.current()
        if current is None:
            return False
        python = current / VENV_PYTHON
        if not python.is_file():
            return False
        argv = [str(python), "-m", "osekkai.core.safe_stop", "--strict"]
        try:
            result = self._run(argv, timeout=SAFE_STOP_TIMEOUT_S, cwd=current)
        except CommandError:
            return False
        return result.returncode == 0
