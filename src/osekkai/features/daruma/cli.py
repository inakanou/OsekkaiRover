"""遊びを始める道具と、判定を確かめる道具（6.3、8.1〜8.4、9.2）。

`python -m osekkai.features.daruma` に、次のどちらか 1 つを付けて動かす。

- `--play [--fake-hardware] [--config PATH]`: 話しかけたときと同じ遊びを 1 回して終える（8.1）。
  終わり方の名前を 1 行出す
- `--judge-test [--seconds N] [--fake-hardware] [--config PATH]`: N 秒（1〜600、既定 30）の間、
  見る長さごとの動きの量と、設定のしきい値での「うごいた」「とまってる」を出し続ける（8.2）

単一の実行のロックを取る。取れなければ、止めてから実行するよう示して 75 で終える（8.3）。
デバイスにも保存にも触らない。取ったロックはそのまま `Runtime` に渡す。
基盤は、道具用の機能 1 つ（だるまさんと同じ申告で self-check の項目はない）と、
`--fake-hardware` でなければ見つけてあいさつ（エントリーポイントから名前で選ぶ）で動かす。
`--fake-hardware` では台本の偽のカメラを使い、Enter キーがボタンの押下になる（8.4）。

終了コード: 0 はできた、1 はできなかった（カメラがないなど）、2 は指定の誤り、
75 はロボットが動いている。

動きの量は画面（標準出力）にだけ出し、ログには出さない（6.3）。ログには決まった文言・
終わり方の種類・例外の型の名前だけを出す。模擬のデバイスが端末に出す画面と声は、
開発者の画面への出力で、ログではない。

ここだけは、組み立ての口（`osekkai.__main__.build_devices`）と基盤の起動
（`Runtime`・`FeatureRegistry`・`InstanceLock`・`SystemClock`・`ConfigLoader`・`Paths`）を
読み込んでよい。
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
from enum import Enum
from pathlib import Path
from typing import ClassVar, Final

import osekkai.features.presence.api as presence_api
from osekkai.__main__ import NO_PI_ADAPTER_MESSAGE, HardwareUnavailable, build_devices
from osekkai.core.arbiter import Lease
from osekkai.core.clock import Clock, SystemClock
from osekkai.core.config import ConfigLoader, FeatureConfig
from osekkai.core.feature import Feature, FeatureContext, FeatureSpec
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.logging_setup import configure_logging
from osekkai.core.paths import Paths
from osekkai.core.registry import FeatureRegistry
from osekkai.core.runtime import ExitCode, Runtime
from osekkai.core.types import DeviceUnavailable, LeaseRevoked, Resource, ResourceBusy
from osekkai.features.daruma import texts
from osekkai.features.daruma.camera import BorrowCamera, ScriptedCamera, borrow_from_presence
from osekkai.features.daruma.config import DarumaConfig
from osekkai.features.daruma.feature import DarumaFeature, run_game
from osekkai.features.daruma.flow import GameResult
from osekkai.features.daruma.lease_io import OWNER
from osekkai.features.daruma.rules import EndReason, RoundKind, judge_watch

logger = logging.getLogger(__name__)

FEATURE_NAME: Final = DarumaFeature.spec.name
PRESENCE_NAME: Final = "presence"  # カメラを貸す機能（エントリーポイントの名前で選ぶ）

EXIT_OK: Final = int(ExitCode.OK)
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
EXIT_LOCK_BUSY: Final = int(ExitCode.ALREADY_RUNNING)  # 75

JUDGE_DEFAULT_SECONDS: Final = 30
MAX_JUDGE_SECONDS: Final = 600
ACQUIRE_WAIT_S: Final = 1.0  # 資源を待つ秒数（止める旗を見ながら繰り返す）
WAIT_POLL_S: Final = 0.1  # 終わりの旗と見つけてあいさつを見る間隔
PRESENCE_WAIT_S = 10.0  # 見つけてあいさつが動き出すのを待つ上限（秒）。テストで短くする

LOCK_BUSY_MESSAGE: Final = "ロボットが動いています。止めてから実行してください"
LOCK_UNCHECKED_MESSAGE: Final = "単一の実行のロックを確かめられません"
DISABLED_MESSAGE: Final = "だるまさんが無効になっています（daruma.enabled）"
CONFIG_ERROR_HEAD: Final = "設定の誤り"
NOT_STARTED_MESSAGE: Final = "道具の機能を始められませんでした"
SECONDS_ONLY: Final = "--seconds は --judge-test といっしょに使ってください"

LABEL_MOVED: Final = "うごいた"
LABEL_STILL: Final = "とまってる"

# 遊べなかった終わり方（終了コード 1）
FAILED_REASONS: Final = frozenset(
    {EndReason.NO_CAMERA, EndReason.CAMERA_LOST, EndReason.BUSY, EndReason.ERROR}
)


class JudgeResult(Enum):
    """判定の道具の終わり方。"""

    DONE = "done"
    CAMERA = "camera"  # カメラが使えない
    STOPPED = "stopped"  # 途中で止めた
    ERROR = "error"  # そのほかの失敗


JUDGE_MESSAGES: Final = {
    JudgeResult.DONE: "確かめ終えました",
    JudgeResult.CAMERA: "カメラが使えなかったので、確かめるのをやめました",
    JudgeResult.STOPPED: "途中で止めました",
    JudgeResult.ERROR: "確かめるのに失敗しました",
}

# 見捨てた機能のスレッドが残ったときに、強制的に終える（テストで差し替える）
_force_exit: Callable[[int], object] = os._exit

# 時計からカメラを借りる口を作る（偽のカメラは機能の時計で待つ）
BorrowFactory = Callable[[Clock], BorrowCamera]


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m osekkai.features.daruma",
        description="だるまさんの遊びを 1 回する・動いたかの判定を確かめる",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--play", action="store_true", help="遊びを 1 回して終える")
    action.add_argument(
        "--judge-test",
        action="store_true",
        help="見る長さごとの動きの量と、うごいた・とまってる を出し続ける",
    )
    parser.add_argument(
        "--seconds",
        type=_seconds,
        metavar="N",
        help=f"確かめる秒数（1〜{MAX_JUDGE_SECONDS} の整数。既定は {JUDGE_DEFAULT_SECONDS}）",
    )
    parser.add_argument(
        "--fake-hardware",
        action="store_true",
        help="実機の代わりに端末で動かす（偽のカメラ。Enter キーがボタン）",
    )
    parser.add_argument("--config", type=Path, metavar="PATH", help="設定ファイルを指定する")
    args = parser.parse_args(argv)
    if args.seconds is not None and not args.judge_test:
        parser.error(SECONDS_ONLY)
    return args


def _seconds(text: str) -> int:
    """確かめる秒数（1〜上限の整数）。誤りの文言に値は入れない。"""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= MAX_JUDGE_SECONDS:
        raise argparse.ArgumentTypeError(f"秒数は 1 から {MAX_JUDGE_SECONDS} の整数にしてください")
    return value


# --- カメラを借りる口 ---


def make_fake_camera(clock: Clock) -> ScriptedCamera:
    """PC の道具の台本の偽のカメラ（8.4）。テストで差し替える。"""
    return ScriptedCamera(clock)


def make_fake_camera_borrow(clock: Clock) -> BorrowCamera:
    """台本の偽のカメラから借りる口。"""
    return make_fake_camera(clock).borrow


def make_presence_borrow(clock: Clock) -> BorrowCamera:
    """見つけてあいさつから借りる口。動き出すまで少し待ってから借りる。

    道具の中では見つけてあいさつも同時に始まるので、置き場に見る流れが置かれるのを
    `PRESENCE_WAIT_S` 秒まで待つ。置かれなければそのまま借りに行く（使えないなら誤りが出る）。
    """

    def borrow(owner: str, alive: Callable[[], bool]) -> presence_api.CameraLoan:
        deadline = clock.monotonic() + PRESENCE_WAIT_S
        while presence_api.running_watcher() is None and alive():
            if clock.monotonic() >= deadline:
                logger.info("見つけてあいさつが動いていません")
                break
            clock.sleep(WAIT_POLL_S)
        return borrow_from_presence(owner, alive)

    return borrow


# --- 遊びの道具（8.1） ---


class PlayOutcome:
    """遊びの道具の終わりの知らせ。"""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: GameResult | None = None


def _check_config(config: FeatureConfig) -> DarumaConfig:
    if not isinstance(config, DarumaConfig):
        raise TypeError("だるまさんの設定ではありません")
    return config


def _tool_spec() -> FeatureSpec:
    """だるまさんと同じ申告で、self-check の項目はない。"""
    return dataclasses.replace(DarumaFeature.spec, checks=())


def make_play_feature(outcome: PlayOutcome, borrow_for: BorrowFactory) -> type[Feature]:
    """遊びを 1 回だけする機能のクラスを作る。言葉は登録しない。"""

    class PlayFeature:
        spec: ClassVar[FeatureSpec] = _tool_spec()

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            self._config = _check_config(config)
            self._ctx = ctx

        def run(self, stop: threading.Event) -> None:
            try:
                outcome.result = run_game(
                    self._ctx, self._config, stop, borrow=borrow_for(self._ctx.clock)
                )
            except Exception as error:
                logger.warning("遊びの道具が失敗しました: %s", type(error).__name__)
                outcome.result = GameResult(EndReason.ERROR, 0)
            finally:
                outcome.done.set()

    return PlayFeature


# --- 判定の道具（8.2） ---


class JudgeOutcome:
    """判定の道具の終わりの知らせ。"""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: JudgeResult | None = None


def judge_line(at: float, amount: float, kind: RoundKind) -> str:
    """判定の道具の 1 行。画面にだけ出す（6.3）。"""
    label = LABEL_MOVED if kind is RoundKind.MOVED else LABEL_STILL
    return f"{at:5.1f} 秒  動きの量 {amount:.3f}  {label}"


def make_judge_feature(
    seconds: int,
    outcome: JudgeOutcome,
    borrow_for: BorrowFactory,
    emit: Callable[[str], object],
) -> type[Feature]:
    """判定を確かめる機能のクラスを作る（8.2）。

    首を借りて正面に向け、カメラを借りて、`seconds` 秒の間（少なくとも 1 回）、
    設定の見る長さの動きの量を測り、遊びと同じ `judge_watch` で判定して `emit` に渡す。
    """

    class JudgeFeature:
        spec: ClassVar[FeatureSpec] = _tool_spec()

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            self._config = _check_config(config)
            self._ctx = ctx

        def run(self, stop: threading.Event) -> None:
            try:
                outcome.result = self._run(stop)
            except Exception as error:
                logger.warning("判定の道具が失敗しました: %s", type(error).__name__)
                outcome.result = JudgeResult.ERROR
            finally:
                outcome.done.set()

        def _run(self, stop: threading.Event) -> JudgeResult:
            lease = _acquire_waiting(self._ctx, frozenset({Resource.NECK}), stop)
            if lease is None:
                return JudgeResult.STOPPED
            with lease:  # 返すと基盤が首を正面に戻す
                try:
                    lease.neck.look(*texts.POSE_FACING)
                except LeaseRevoked:
                    logger.info("首を取り上げられたので、確かめるのをやめます")
                    return JudgeResult.STOPPED
                except DeviceUnavailable as error:
                    logger.warning("首を向けられませんでした: %s", type(error).__name__)
                borrow = borrow_for(self._ctx.clock)

                def alive() -> bool:
                    return not stop.is_set() and lease.active

                try:
                    loan = borrow(OWNER, alive)
                except (presence_api.CameraNotAvailable, presence_api.CameraBusy) as error:
                    logger.info("カメラを借りられませんでした: %s", type(error).__name__)
                    return JudgeResult.CAMERA
                try:
                    return self._measure(loan, stop)
                finally:
                    loan.release()

        def _measure(self, loan: presence_api.CameraLoan, stop: threading.Event) -> JudgeResult:
            config = self._config
            watch = config.watch_seconds
            rounds = max(1, int(seconds // watch))
            for index in range(rounds):
                if stop.is_set():
                    return JudgeResult.STOPPED
                try:
                    amount = loan.motion(watch)
                except Exception as error:
                    if stop.is_set():
                        return JudgeResult.STOPPED
                    logger.warning("カメラが使えなくなりました: %s", type(error).__name__)
                    return JudgeResult.CAMERA
                kind = judge_watch(amount, False, config.threshold)
                emit(judge_line((index + 1) * watch, amount, kind))  # 量はログに出さない
            return JudgeResult.DONE

    return JudgeFeature


def _acquire_waiting(
    ctx: FeatureContext, resources: frozenset[Resource], stop: threading.Event
) -> Lease | None:
    """止める旗を見ながら、借りられるまで待つ。止められたら None。"""
    while not stop.is_set():
        try:
            return ctx.acquire(resources, wait=ACQUIRE_WAIT_S)
        except ResourceBusy:
            logger.debug("資源が空いていないので、もう一度借ります")
    return None


# --- 入口 ---


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(False)
    paths = Paths()
    lock = InstanceLock(paths.lock_file)
    if not _try_lock(lock):
        # デバイスにも保存にも触らない（動いている方を乱さない。8.3）
        logger.error(LOCK_BUSY_MESSAGE)
        print(LOCK_BUSY_MESSAGE, file=sys.stderr)
        return EXIT_LOCK_BUSY
    try:
        companions = [] if args.fake_hardware else _presence_classes()
        borrow_for: BorrowFactory = (
            make_fake_camera_borrow if args.fake_hardware else make_presence_borrow
        )
        config = _load_config(args, paths, companions)
        if isinstance(config, int):
            return config
        if args.play:
            return _play(args, paths, lock, companions, borrow_for)
        return _judge(args, paths, lock, companions, borrow_for)
    finally:
        lock.release()  # Runtime も放す。2 回呼んでもよい


def _try_lock(lock: InstanceLock) -> bool:
    try:
        return lock.acquire()
    except OSError as error:  # ロックのファイルを作れない。触らない側に倒す
        logger.warning("%s: %s", LOCK_UNCHECKED_MESSAGE, type(error).__name__)
        return False


def _presence_classes() -> list[type[Feature]]:
    """エントリーポイントから見つけてあいさつを選ぶ（本体は読み込まない）。なければ空。"""
    found = [cls for cls in FeatureRegistry().feature_classes() if cls.spec.name == PRESENCE_NAME]
    if not found:
        logger.warning("見つけてあいさつが見つかりません")
    return found


def _load_config(
    args: argparse.Namespace, paths: Paths, companions: Sequence[type[Feature]]
) -> DarumaConfig | int:
    """だるまさんと、いっしょに動かす機能の設定を読む。だめなら理由を出して終了コードを返す。"""
    models: dict[str, type[FeatureConfig]] = {FEATURE_NAME: DarumaConfig}
    models.update({cls.spec.name: cls.spec.config_model for cls in companions})
    try:
        report = ConfigLoader(paths, models).load(args.config)
    except OSError as error:
        print(
            f"{CONFIG_ERROR_HEAD}: 設定ファイルを読めません（{type(error).__name__}）",
            file=sys.stderr,
        )
        return EXIT_FAILED
    # 基盤の文言は値を含まない（場所と理由だけ）
    messages = [*report.fatal_errors]
    for name in models:
        messages.extend(report.feature_errors.get(name, ()))
    config = report.features.get(FEATURE_NAME, DarumaConfig())
    if messages or not isinstance(config, DarumaConfig):
        for message in messages:
            print(f"{CONFIG_ERROR_HEAD}: {message}", file=sys.stderr)
        return EXIT_FAILED
    if not config.enabled:
        print(DISABLED_MESSAGE, file=sys.stderr)
        return EXIT_FAILED
    return config


def _play(
    args: argparse.Namespace,
    paths: Paths,
    lock: InstanceLock,
    companions: Sequence[type[Feature]],
    borrow_for: BorrowFactory,
) -> int:
    """遊びを 1 回して、終わり方の名前を出す（8.1）。"""
    outcome = PlayOutcome()
    feature = make_play_feature(outcome, borrow_for)
    ran = _run_alone(args, paths, lock, [feature, *companions], outcome.done)
    if isinstance(ran, int):
        return ran

    reason = outcome.result.reason if outcome.result is not None else EndReason.STOPPED
    logger.info("遊びの道具を終えました: %s", reason.value)
    exit_code = EXIT_FAILED if reason in FAILED_REASONS else EXIT_OK
    print(f"終わり方: {reason.value}")  # 種類の名前だけ
    return _finish(exit_code, abandoned=ran.abandoned)


def _judge(
    args: argparse.Namespace,
    paths: Paths,
    lock: InstanceLock,
    companions: Sequence[type[Feature]],
    borrow_for: BorrowFactory,
) -> int:
    """決めた秒数の間、動きの量と判定を出す（8.2）。"""
    outcome = JudgeOutcome()
    seconds = args.seconds or JUDGE_DEFAULT_SECONDS
    feature = make_judge_feature(seconds, outcome, borrow_for, _emit)
    ran = _run_alone(args, paths, lock, [feature, *companions], outcome.done)
    if isinstance(ran, int):
        return ran

    result = outcome.result or JudgeResult.STOPPED
    logger.info("判定の道具を終えました: %s", result.value)
    ok = result in (JudgeResult.DONE, JudgeResult.STOPPED)
    exit_code = EXIT_OK if ok else EXIT_FAILED
    print(JUDGE_MESSAGES[result], file=sys.stdout if ok else sys.stderr)
    return _finish(exit_code, abandoned=ran.abandoned)


def _emit(line: str) -> None:
    print(line, flush=True)


@dataclasses.dataclass(frozen=True)
class _Ran:
    abandoned: bool  # 基盤が機能のスレッドを見捨てたか


def _run_alone(
    args: argparse.Namespace,
    paths: Paths,
    lock: InstanceLock,
    features: Sequence[type[Feature]],
    done: threading.Event,
) -> _Ran | int:
    """基盤を `features` の登録で動かし、`done` が立つか Ctrl+C まで待つ。

    取ったロックはそのまま `Runtime` に渡す。始める前に終えたときは終了コードを返す。
    """
    registry = FeatureRegistry(features)
    device_args = argparse.Namespace(fake_hardware=args.fake_hardware, config=args.config, say=None)
    try:
        devices = build_devices(device_args, paths, registry)
    except HardwareUnavailable as error:
        logger.error("実機のアダプタを読み込めません: %s", error)
        print(NO_PI_ADAPTER_MESSAGE, file=sys.stderr)
        return EXIT_FAILED

    clock = SystemClock(devices.timesync)
    runtime = Runtime(devices, paths, clock, registry, config_override=args.config, lock=lock)
    try:
        code = runtime.start()
        if code is not None:
            return int(code)
        if FEATURE_NAME not in runtime.started_features:
            print(NOT_STARTED_MESSAGE, file=sys.stderr)
            return EXIT_FAILED
        while not done.wait(WAIT_POLL_S):
            pass
    except KeyboardInterrupt:
        logger.info("Ctrl+C を受けたので止めます")
    finally:
        runtime.shutdown()  # 首を正面に戻し、カメラとリースを返す
    return _Ran(abandoned=bool(runtime.abandoned))


def _finish(exit_code: int, *, abandoned: bool) -> int:
    if abandoned:
        # 見捨てた機能のスレッドは daemon でないので、待たずに終える
        _flush()
        _force_exit(exit_code)
    return exit_code


def _flush() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(OSError, ValueError):
            stream.flush()
