"""試しに 1 つのタスクを動かす（10.1〜10.7）。

`python -m osekkai.features.routine --try <タスクの id> [--fast] [--fake-hardware] [--config PATH]`

設定のタスクを 1 つだけ、時刻を待たずに、本番と同じ進み方で動かす。記録は試し用
（`NullRecordBook`）で、星も結果も予告も変えない。割り込みと予告はない。
寝るタスクなら、おやすみの表示を見せて終える。静かな状態は求めない。

流れ:
1. 単一の実行のロックを取る。取れなければ理由を出して 75 で終える（デバイスは作らない）
2. 設定を読む。誤りなら理由を出して 1、ない id ならタスクの id の一覧を出して 2 で終える
3. デバイスと、試しの機能 1 つだけを登録した基盤を作って起動する。取ったロックはそのまま渡す
   （同じプロセスで取り直すと断られるため）
4. タスクが終わるか Ctrl+C で基盤を止め、結果の種類だけを 1 行出して 0 で終える

ログと出力には、呼び名・言葉・LCD の文字・設定の時刻を出さない（タスクの id と結果の種類だけ）。
模擬のデバイスが端末に出す画面と声は、開発者の画面への出力で、ログではない。

ここと `__main__.py` だけは、組み立ての口（`osekkai.__main__.build_devices`）と基盤の起動
（`Runtime`・`FeatureRegistry`・`InstanceLock`・`SystemClock`）を読み込んでよい。
アダプタは直接読み込まない。
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import os
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import ClassVar, Final

from osekkai.__main__ import NO_PI_ADAPTER_MESSAGE, HardwareUnavailable, build_devices
from osekkai.core.arbiter import Lease
from osekkai.core.clock import SystemClock
from osekkai.core.config import ConfigLoader, FeatureConfig
from osekkai.core.feature import Feature, FeatureContext, FeatureSpec
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.logging_setup import configure_logging
from osekkai.core.paths import Paths
from osekkai.core.registry import FeatureRegistry
from osekkai.core.runtime import ExitCode, Runtime
from osekkai.core.types import Face, LeaseRevoked, ResourceBusy
from osekkai.features.routine.config import RoutineConfig, TaskConfig
from osekkai.features.routine.feature import ACQUIRE_WAIT_S, ALL_RESOURCES, RoutineFeature
from osekkai.features.routine.records import NullRecordBook, TaskResult
from osekkai.features.routine.runner import LeaseIO, TaskRunner, TaskStopped, Texts, Timings
from osekkai.features.routine.schedule import DueNotice, DueTask, Phase
from osekkai.features.routine.texts import LCD_SLEEP

logger = logging.getLogger(__name__)

EXIT_LOCK_BUSY: Final = int(ExitCode.ALREADY_RUNNING)  # 75
EXIT_CONFIG: Final = 1
EXIT_UNKNOWN_TASK: Final = 2

LOCK_BUSY_MESSAGE: Final = "ロボットが動いています。止めてから試してください"
LOCK_UNCHECKED_MESSAGE: Final = "単一の実行のロックを確かめられません"
DISABLED_MESSAGE: Final = "日課が無効になっています（routine.enabled）"
NOT_STARTED_MESSAGE: Final = "試しの機能を始められませんでした"
CONFIG_ERROR_HEAD: Final = "設定の誤り"
UNKNOWN_TASK_MESSAGE: Final = "そのタスクは設定にありません。設定のタスクの id:"
NO_TASKS_MESSAGE: Final = "設定にタスクがありません"

RESULT_LABELS: Final = {
    TaskResult.DONE: "できた",
    TaskResult.SKIPPED: "スキップ",
    TaskResult.INTERRUPTED: "中断",
}
STOPPED_LABEL: Final = "中断"  # 途中で止めた（Ctrl+C）

# 短い時間の指定（10.4）。テストでは更に短くする
FAST_TIMINGS = Timings(remind_s=lambda task: 10.0, later_s=30.0)
# 寝るタスクのおやすみの表示を見せる秒数（6.3、10.3）
SLEEP_SHOW_S = 3.0
WAIT_POLL_S: Final = 0.1  # 終わりの旗を見る間隔

# 見捨てた機能のスレッドが残ったときに、強制的に終える（テストで差し替える）
_force_exit: Callable[[int], object] = os._exit


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m osekkai.features.routine",
        description="日課のタスクを 1 つだけ試す（記録と星の数は変わりません）",
    )
    parser.add_argument("--try", dest="task_id", required=True, metavar="ID", help="タスクの id")
    parser.add_argument(
        "--fast", action="store_true", help="催促の間隔を 10 秒、あとでの受付を 30 秒にする"
    )
    parser.add_argument(
        "--fake-hardware",
        action="store_true",
        help="実機の代わりに端末で動かす（Enter キーが USR ボタン）",
    )
    parser.add_argument("--config", type=Path, metavar="PATH", help="設定ファイルを指定する")
    return parser.parse_args(argv)


def timings_for(*, fast: bool) -> Timings:
    """催促とあとでの長さ。短い時間の指定なら `FAST_TIMINGS`（10.4）。"""
    return FAST_TIMINGS if fast else Timings()


class TrialOutcome:
    """試しの機能の終わりの知らせ。`result` が None なら途中で止めた。"""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: TaskResult | None = None


class _TrialHooks:
    """試しの `TaskHooks`。割り込みも予告もない。止める旗だけを見る。"""

    def __init__(self, stop: threading.Event, book: NullRecordBook) -> None:
        self._stop = stop
        self._book = book

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def cut_in(self, phase: Phase) -> DueTask | None:
        return None

    def due_notice(self) -> DueNotice | None:
        return None

    def notice_said(self, notice: DueNotice) -> None:
        """予告はないので、何もしない。"""

    def star_count_after_done(self) -> int:
        return self._book.stars  # 試しでは増やさない（10.2）


def make_trial_feature(
    task: TaskConfig, outcome: TrialOutcome, *, timings: Timings, sleep_show_s: float
) -> type[Feature]:
    """そのタスクだけを動かす試しの機能のクラスを作る。

    申告は日課と同じ名前・優先度・資源・設定のモデルで、self-check の項目はない。
    """

    class TrialFeature:
        spec: ClassVar[FeatureSpec] = dataclasses.replace(RoutineFeature.spec, checks=())

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            if not isinstance(config, RoutineConfig):
                raise TypeError("日課の設定ではありません")
            self._ctx = ctx
            self._texts = Texts(config.call_name)
            self._book = NullRecordBook.from_store(ctx.store)  # 読むだけ（10.2）

        def run(self, stop: threading.Event) -> None:
            try:
                outcome.result = self._run(stop)
            except Exception as exc:
                self._ctx.log.warning("試しに失敗しました: %s", type(exc).__name__)
                outcome.result = TaskResult.INTERRUPTED
            finally:
                outcome.done.set()

        def _run(self, stop: threading.Event) -> TaskResult | None:
            lease = self._acquire(stop)
            if lease is None:
                return None
            with lease:
                io = LeaseIO(lease)
                runner = TaskRunner(io, self._ctx.clock, self._texts, timings)
                hooks = _TrialHooks(stop, self._book)
                self._ctx.log.info("試しのタスクを始めます: %s", task.id)
                try:
                    run = runner.run(task, hooks, on_done=lambda: None)  # 記録しない（10.2）
                except TaskStopped:
                    self._ctx.log.info("止めるので、試しのタスクを途中でやめました: %s", task.id)
                    return None
                except LeaseRevoked:
                    self._ctx.log.info("資源を取り上げられたので、試しを中断にします: %s", task.id)
                    return TaskResult.INTERRUPTED
                if task.sleep:
                    self._show_sleep(io)
                return run.result

        def _acquire(self, stop: threading.Event) -> Lease | None:
            while not stop.is_set():
                try:
                    return self._ctx.acquire(ALL_RESOURCES, wait=ACQUIRE_WAIT_S)
                except ResourceBusy:
                    self._ctx.log.debug("資源が空いていないので、もう一度借ります")
            return None

        def _show_sleep(self, io: LeaseIO) -> None:
            """おやすみの表示を見せる（6.3）。静かな状態は求めない（10.3）。"""
            try:
                io.sleepy()
                io.backlight(True)
                io.show(LCD_SLEEP, Face.SLEEP.text())
                self._ctx.clock.sleep(sleep_show_s)
            except Exception as exc:
                self._ctx.log.warning("おやすみの表示ができませんでした: %s", type(exc).__name__)
            self._ctx.log.info("おやすみの表示を見せました（静かな状態にはしません）")

    return TrialFeature


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(False)
    paths = Paths()
    lock = InstanceLock(paths.lock_file)
    if not _try_lock(lock):
        # デバイスは 1 つも作らない（動いている方のハードに触らない。10.6）
        logger.error(LOCK_BUSY_MESSAGE)
        print(LOCK_BUSY_MESSAGE, file=sys.stderr)
        return EXIT_LOCK_BUSY
    try:
        return _run_locked(args, paths, lock)
    finally:
        lock.release()  # Runtime も放す。2 回呼んでもよい


def _try_lock(lock: InstanceLock) -> bool:
    try:
        return lock.acquire()
    except OSError as error:  # ロックのファイルを作れない。ハードには触らない側に倒す
        logger.warning("%s: %s", LOCK_UNCHECKED_MESSAGE, type(error).__name__)
        return False


def _load_task(args: argparse.Namespace, paths: Paths) -> TaskConfig | int:
    """設定を読み、試すタスクを返す。だめなら終了コードを返す（10.5）。"""
    name = RoutineFeature.spec.name
    try:
        report = ConfigLoader(paths, {name: RoutineConfig}).load(args.config)
    except OSError as error:
        print(
            f"{CONFIG_ERROR_HEAD}: 設定ファイルを読めません（{type(error).__name__}）",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    # 基盤の文言は値を含まない（場所と理由だけ）
    messages = [*report.fatal_errors, *report.feature_errors.get(name, ())]
    config = report.features.get(name)
    if messages or not isinstance(config, RoutineConfig):
        for message in messages:
            print(f"{CONFIG_ERROR_HEAD}: {message}", file=sys.stderr)
        return EXIT_CONFIG
    if not config.enabled:
        print(DISABLED_MESSAGE, file=sys.stderr)
        return EXIT_CONFIG
    for task in config.tasks:
        if task.id == args.task_id:
            return task
    logger.error("試すタスクが設定にありません: %s", args.task_id)
    if config.tasks:
        print(UNKNOWN_TASK_MESSAGE, file=sys.stderr)
        for task in config.tasks:
            print(f"  {task.id}", file=sys.stderr)
    else:
        print(NO_TASKS_MESSAGE, file=sys.stderr)
    return EXIT_UNKNOWN_TASK


def _run_locked(args: argparse.Namespace, paths: Paths, lock: InstanceLock) -> int:
    task = _load_task(args, paths)
    if isinstance(task, int):
        return task
    outcome = TrialOutcome()
    feature = make_trial_feature(
        task, outcome, timings=timings_for(fast=args.fast), sleep_show_s=SLEEP_SHOW_S
    )
    registry = FeatureRegistry([feature])
    device_args = argparse.Namespace(fake_hardware=args.fake_hardware, config=args.config, say=None)
    try:
        devices = build_devices(device_args, paths, registry)
    except HardwareUnavailable as error:
        logger.error("実機のアダプタを読み込めません: %s", error)
        print(NO_PI_ADAPTER_MESSAGE, file=sys.stderr)
        return EXIT_CONFIG

    clock = SystemClock(devices.timesync)
    runtime = Runtime(devices, paths, clock, registry, config_override=args.config, lock=lock)
    try:
        code = runtime.start()
        if code is not None:
            return int(code)
        if feature.spec.name not in runtime.started_features:
            print(NOT_STARTED_MESSAGE, file=sys.stderr)
            return EXIT_CONFIG
        while not outcome.done.wait(WAIT_POLL_S):
            pass
    except KeyboardInterrupt:
        logger.info("Ctrl+C を受けたので、試しを止めます")
    finally:
        runtime.shutdown()

    result = outcome.result
    label = STOPPED_LABEL if result is None else RESULT_LABELS.get(result, result.value)
    logger.info("試しを終えました: %s %s", task.id, "stopped" if result is None else result.value)
    print(label)
    if runtime.abandoned:
        # 見捨てた機能のスレッドは daemon でないので、待たずに終える
        _flush()
        _force_exit(int(ExitCode.OK))
    return int(ExitCode.OK)


def _flush() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(OSError, ValueError):
            stream.flush()
