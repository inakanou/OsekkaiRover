"""試しのお話のコマンド（11.3〜11.5）。

`python -m osekkai.features.talk --try night|morning [--fake-hardware] [--config PATH]`

夜か朝のお話を、時刻と日課の状況を見ずに、その場で用意して 1 回話す（11.3）。記録は試し用
（`NullTalkRecord`）で、その日に話したことの記録と直前の好きなものを変えない（11.4）。
静かな状態なら話さず、話さなかったことを表示する（11.5）。

流れ（日課の試しと同じ）:
1. 単一の実行のロックを取る。取れなければ理由を出して 75 で終える（デバイスは作らない）
2. 設定を読む。誤りか無効なら理由を出して 1 で終える（日課の試しと同じく、無効なら試さない）
3. デバイスと、試しの機能 1 つだけを登録した基盤を作って起動する。取ったロックはそのまま渡す
   （同じプロセスで取り直すと断られるため）
4. 話し終えるか Ctrl+C で基盤を止め、結果を 1 行出して 0 で終える

静かな状態は、今の静かな状態（`ctx.is_quiet`）に加えて、止める前のロボットが静かな状態だった
こと（`ctx.was_quiet_before_restart`）も見る。試しは別のプロセスで、起動した直後は静かでない
ため、おやすみモードのまま止めたロボットで話さないようにする（11.5）。

用意には前もっての時間がないので、問い合わせの時間切れ（`fetch_timeout_seconds`）まで待つ。
偽のハードでは、外へ問い合わせない偽の天気・注意報・豆知識を使う（12.2）。

ログと出力には、呼び名・好きなもの・豆知識・天気の値を出さない（種類と結果だけ）。
模擬のデバイスが端末に出す画面と声は、開発者の画面への出力で、ログではない。

ここと `__main__.py` だけは、組み立ての口（`osekkai.__main__.build_devices`）と基盤の起動
（`Runtime`・`FeatureRegistry`・`InstanceLock`・`SystemClock`）を読み込んでよい。
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
from datetime import date, timedelta
from pathlib import Path
from random import Random
from typing import ClassVar, Final

from osekkai.__main__ import NO_PI_ADAPTER_MESSAGE, HardwareUnavailable, build_devices
from osekkai.core.clock import SystemClock
from osekkai.core.config import ConfigLoader, FeatureConfig
from osekkai.core.feature import Feature, FeatureContext, FeatureSpec
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.logging_setup import configure_logging
from osekkai.core.paths import Paths
from osekkai.core.registry import FeatureRegistry
from osekkai.core.runtime import ExitCode, Runtime
from osekkai.features.talk.adapters.canned import CannedAlerts, CannedFacts, CannedWeather
from osekkai.features.talk.config import TalkConfig
from osekkai.features.talk.feature import TICK_S, Sources, TalkFeature, choose_sources
from osekkai.features.talk.flow import FlowOutcome, TalkFlow
from osekkai.features.talk.model import TalkKind
from osekkai.features.talk.prepare import Preparer
from osekkai.features.talk.record import NullTalkRecord

logger = logging.getLogger(__name__)

EXIT_LOCK_BUSY: Final = int(ExitCode.ALREADY_RUNNING)  # 75
EXIT_CONFIG: Final = 1

LOCK_BUSY_MESSAGE: Final = "ロボットが動いています。止めてから試してください"
LOCK_UNCHECKED_MESSAGE: Final = "単一の実行のロックを確かめられません"
DISABLED_MESSAGE: Final = "お話が無効になっています（talk.enabled）"
NOT_STARTED_MESSAGE: Final = "試しの機能を始められませんでした"
CONFIG_ERROR_HEAD: Final = "設定の誤り"

QUIET_MESSAGE: Final = "おやすみモードなので はなしませんでした"  # 11.5
SPOKEN_LABEL: Final = "はなしました"
INTERRUPTED_LABEL: Final = "中断"
FAILED_LABEL: Final = "はなせませんでした"
STOPPED_LABEL: Final = "中断"  # 途中で止めた（Ctrl+C）

KINDS: Final = {"night": TalkKind.NIGHT, "morning": TalkKind.MORNING}
RESULT_LABELS: Final = {
    FlowOutcome.SPOKEN: SPOKEN_LABEL,
    FlowOutcome.QUIET: QUIET_MESSAGE,
    FlowOutcome.INTERRUPTED: INTERRUPTED_LABEL,
    FlowOutcome.FAILED: FAILED_LABEL,
}

WAIT_POLL_S: Final = 0.1  # 終わりの旗を見る間隔

# 見捨てた機能のスレッドが残ったときに、強制的に終える（テストで差し替える）
_force_exit: Callable[[int], object] = os._exit


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m osekkai.features.talk",
        description="夜か朝のお話を 1 回だけ試す（その日の記録と直前の好きなものは変わりません）",
    )
    parser.add_argument(
        "--try", dest="kind", required=True, choices=sorted(KINDS), help="夜か朝のお話"
    )
    parser.add_argument(
        "--fake-hardware",
        action="store_true",
        help="実機の代わりに端末で動かす（偽の天気と偽の豆知識を使う）",
    )
    parser.add_argument("--config", type=Path, metavar="PATH", help="設定ファイルを指定する")
    return parser.parse_args(argv)


def weather_day(kind: TalkKind, today: date) -> date:
    """話す天気の日。夜はあした、朝は今日（5.1）。"""
    return today + timedelta(days=1) if kind is TalkKind.NIGHT else today


def fake_sources(config: TalkConfig) -> Sources:
    """偽のハードの出どころ。外へ問い合わせない（12.2）。設定のないものは使わない（1.5、1.6）。"""
    location = config.location
    return Sources(
        weather=CannedWeather() if location is not None else None,
        alerts=CannedAlerts() if location is not None and config.warning_area else None,
        facts=CannedFacts() if config.topics else None,
    )


class TrialOutcome:
    """試しの機能の終わりの知らせ。`result` が None なら途中で止めた。"""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: FlowOutcome | None = None


def make_trial_feature(
    kind: TalkKind, outcome: TrialOutcome, *, fake_hardware: bool, paths: Paths
) -> type[Feature]:
    """その種類のお話を 1 回だけ話す試しの機能のクラスを作る。

    申告はお話と同じ名前・優先度・資源・設定のモデルで、self-check の項目はない。
    """

    class TrialFeature:
        spec: ClassVar[FeatureSpec] = dataclasses.replace(TalkFeature.spec, checks=())

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            if not isinstance(config, TalkConfig):
                raise TypeError("お話の設定ではありません")
            self._ctx = ctx
            self._config = config

        def run(self, stop: threading.Event) -> None:
            try:
                outcome.result = self._run(stop)
            except Exception as exc:
                self._ctx.log.warning("試しのお話に失敗しました: %s", type(exc).__name__)
                outcome.result = FlowOutcome.FAILED
            finally:
                outcome.done.set()

        def _quiet(self) -> bool:
            return self._ctx.is_quiet or self._ctx.was_quiet_before_restart

        def _run(self, stop: threading.Event) -> FlowOutcome | None:
            if self._quiet():  # 用意（外への問い合わせ）もしない（11.5）
                self._ctx.log.info("静かな状態なので試しのお話をしませんでした: %s", kind.name)
                return FlowOutcome.QUIET
            config = self._config
            clock = self._ctx.clock
            today = clock.now().date()  # 時刻が確かかは見ない（11.3）
            sources = (
                fake_sources(config) if fake_hardware else choose_sources(config, clock.now, paths)
            )
            record = NullTalkRecord()  # 記録と直前の好きなものを変えない（11.4）
            preparer = Preparer(
                weather=sources.weather,
                alerts=sources.alerts,
                facts=sources.facts,
                record=record,
                config=config,
                rng=Random(),
                monotonic=clock.monotonic,
            )
            self._ctx.log.info("試しのお話を用意します: %s", kind.name)
            prepared = preparer.take(
                kind, weather_day(kind, today), today, config.fetch_timeout_seconds
            )
            flow = TalkFlow(self._ctx, config)
            while not stop.is_set():
                if self._quiet():
                    return FlowOutcome.QUIET
                result = flow.speak(prepared, on_started=lambda: None)
                if result is not FlowOutcome.BUSY:
                    return result
                stop.wait(TICK_S)  # 借りられなければ少し待って借り直す
            return None

    return TrialFeature


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(False)
    paths = Paths()
    lock = InstanceLock(paths.lock_file)
    if not _try_lock(lock):
        # デバイスは 1 つも作らない（動いている方のハードに触らない）
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


def _check_config(args: argparse.Namespace, paths: Paths) -> int | None:
    """設定を読み、試せなければ終了コードを返す（日課の試しと同じ）。"""
    name = TalkFeature.spec.name
    try:
        report = ConfigLoader(paths, {name: TalkConfig}).load(args.config)
    except OSError as error:
        print(
            f"{CONFIG_ERROR_HEAD}: 設定ファイルを読めません（{type(error).__name__}）",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    # 基盤の文言は値を含まない（場所と理由だけ）
    messages = [*report.fatal_errors, *report.feature_errors.get(name, ())]
    config = report.features.get(name)
    if messages or not isinstance(config, TalkConfig):
        for message in messages:
            print(f"{CONFIG_ERROR_HEAD}: {message}", file=sys.stderr)
        return EXIT_CONFIG
    if not config.enabled:
        print(DISABLED_MESSAGE, file=sys.stderr)
        return EXIT_CONFIG
    return None


def _run_locked(args: argparse.Namespace, paths: Paths, lock: InstanceLock) -> int:
    code = _check_config(args, paths)
    if code is not None:
        return code
    kind = KINDS[args.kind]
    outcome = TrialOutcome()
    feature = make_trial_feature(kind, outcome, fake_hardware=args.fake_hardware, paths=paths)
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
        started = runtime.start()
        if started is not None:
            return int(started)
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
    label = STOPPED_LABEL if result is None else RESULT_LABELS.get(result, FAILED_LABEL)
    logger.info(
        "試しのお話を終えました: %s %s", kind.name, "stopped" if result is None else result.value
    )
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
