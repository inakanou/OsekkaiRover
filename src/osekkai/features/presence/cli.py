"""顔を覚える・消す・一覧と、確認のコマンド（5.1〜5.9、6.3、6.5、9.1〜9.3）。

`python -m osekkai.features.presence` に、次のどれか 1 つを付けて動かす。

- `--list`: 覚えた人の名前（識別子）と見本の数を出す。ロックはいらない。呼び名は出さない
- `--learn-face 名前 --call-name 呼び名 [--fake-hardware] [--config PATH]`: 顔を覚える
- `--forget-face 名前`: 1 人の特徴とあいさつの記録を消す
- `--forget-all`: すべての人の特徴とあいさつの記録を消す
- `--watch-test [--seconds N] [--fake-hardware] [--config PATH]`: 追従の確認。顔の位置・
  首の角度・目の向き・認識の結果（覚えた人かどうかだけ）を N 秒（既定 30）の間、出し続ける（9.1）
- `--motion-test [--seconds N] [--fake-hardware] [--config PATH]`: 動きの量の確認。
  N 秒（既定 20）の間、2 秒ごとに動きの量を出す（9.2）

覚える・消す・確認は単一の実行のロックを取る。取れなければ理由を出して 75 で終える（5.8、9.3）。
覚える・確認は、基盤（`Runtime`）をその用の機能 1 つだけの登録で動かす（日課の試しと同じ形）。
取ったロックはそのまま `Runtime` に渡す（同じプロセスで取り直すと断られるため）。
覚えた顔が読めないときは始めない（ほかの人の特徴を黙って上書きしない）。
追従の確認は覚えた顔を読むだけで、読めなければ全員を知らない人として出す（書かない）。

終了コード: 0 はできた、1 はできなかった・使えない、2 は指定の誤り、75 はロボットが動いている。

ログと出力には、呼び名・顔の特徴・言葉・LCD の文字を出さない（6.3）。名前（識別子）は
一覧の出力にだけ出す。追従の確認の認識の結果は、覚えた人かどうかだけを出す。模擬のデバイスが端末に出す画面と声は、開発者の画面への出力で、ログではない。

ここと `__main__.py` だけは、組み立ての口（`osekkai.__main__.build_devices`）と基盤の起動
（`Runtime`・`FeatureRegistry`・`InstanceLock`・`SystemClock`・`ConfigLoader`）を読み込んでよい。
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import math
import os
import sys
import threading
from collections.abc import Callable, Sequence
from enum import Enum
from pathlib import Path
from typing import ClassVar, Final

from osekkai.__main__ import NO_PI_ADAPTER_MESSAGE, HardwareUnavailable, build_devices
from osekkai.core.arbiter import Lease
from osekkai.core.clock import SystemClock
from osekkai.core.config import ConfigLoader, FeatureConfig
from osekkai.core.feature import Feature, FeatureContext, FeatureSpec
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.lcd_text import center
from osekkai.core.logging_setup import configure_logging
from osekkai.core.paths import Paths
from osekkai.core.registry import FeatureRegistry
from osekkai.core.runtime import ExitCode, Runtime
from osekkai.core.storage import StateStore
from osekkai.core.types import (
    DeviceUnavailable,
    Face,
    LeaseRevoked,
    Resource,
    ResourceBusy,
    SpeechFailed,
)
from osekkai.features.presence import texts
from osekkai.features.presence.config import PresenceConfig
from osekkai.features.presence.faces import MSG_PERSON_ID, PERSON_ID, Embedding, FaceBook, Person
from osekkai.features.presence.faces import STORE_KEY as FACES_KEY
from osekkai.features.presence.feature import RESOURCES, PresenceFeature, choose_devices
from osekkai.features.presence.greeting import STORE_KEY as GREETINGS_KEY
from osekkai.features.presence.greeting import GreetingRecords
from osekkai.features.presence.ports import CameraPort, CameraUnavailable, VisionPort
from osekkai.features.presence.tracking import FaceBox, Tracker, TrackOutput, largest
from osekkai.features.presence.watcher import MOTION_HZ, RECOGNIZE_INTERVAL, _peak_motion

logger = logging.getLogger(__name__)

FEATURE_NAME: Final = PresenceFeature.spec.name

EXIT_OK: Final = int(ExitCode.OK)
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
EXIT_LOCK_BUSY: Final = int(ExitCode.ALREADY_RUNNING)  # 75

MIN_SAMPLES: Final = 10  # これより少なければ保存しない（5.5）
LEARN_INTERVAL_S = 0.5  # 特徴を集める間（秒。5.1）。テストで短くする
LEARN_DURATION_S = 40.0  # 特徴を集める長さの上限（秒。5.1）。テストで短くする
ACQUIRE_WAIT_S: Final = 1.0  # 資源を待つ秒数（止める旗を見ながら繰り返す）
WAIT_POLL_S: Final = 0.1  # 終わりの旗を見る間隔

LOCK_BUSY_MESSAGE: Final = "ロボットが動いています。止めてから実行してください"
LOCK_UNCHECKED_MESSAGE: Final = "単一の実行のロックを確かめられません"
UNREADABLE_MESSAGE: Final = (
    "覚えた顔を読めません。--forget-all ですべて消すか、バックアップから戻してください"
)
NO_CAMERA_MESSAGE: Final = "カメラか顔のモデルが使えないので、顔を覚えられません"
DISABLED_MESSAGE: Final = "見つけてあいさつが無効になっています（presence.enabled）"
CONFIG_ERROR_HEAD: Final = "設定の誤り"
NOT_STARTED_MESSAGE: Final = "顔を覚える機能を始められませんでした"
CALL_NAME_MISSING: Final = "--learn-face には --call-name で呼び名を付けてください"
CALL_NAME_ONLY: Final = "--call-name は --learn-face といっしょに使ってください"
CALL_NAME_EMPTY: Final = "呼び名が空です"
UNKNOWN_PERSON_MESSAGE: Final = "その名前の顔は覚えていません（--list で名前を確かめてください）"
NO_PEOPLE_MESSAGE: Final = "覚えた顔はありません"
SAVE_FAILED_MESSAGE: Final = "保存できませんでした"
NO_CAMERA_CHECK_MESSAGE: Final = "カメラか顔のモデルが使えないので、確かめられません"
UNREADABLE_WATCH_MESSAGE: Final = "覚えた顔を読めないので、みんな知らない人として出します"
CHECK_NOT_STARTED_MESSAGE: Final = "確認の機能を始められませんでした"
SECONDS_ONLY: Final = "--seconds は --watch-test か --motion-test といっしょに使ってください"

# 確認（9.1、9.2）
WATCH_DEFAULT_SECONDS: Final = 30
MOTION_DEFAULT_SECONDS: Final = 20
MAX_CHECK_SECONDS: Final = 3600
CHECK_SECOND_S = 1.0  # 確認の 1 秒の実際の長さ。テストで短くする
WATCH_EVERY: Final = 0.2  # 追従の確認の 1 行の間（確認の秒）
MOTION_EVERY: Final = 2  # 動きの量を出す間（確認の秒。9.2）
LABEL_KNOWN: Final = "覚えた人"
LABEL_UNKNOWN: Final = "知らない人"
LABEL_WAITING: Final = "まだ"  # 顔は見えているが、まだ認識していない
LABEL_NONE: Final = "-"  # 顔が見えていない


class LearnResult(Enum):
    """覚える機能の終わり方。"""

    LEARNED = "learned"
    TOO_FEW = "too_few"  # 見本が足りない（5.5）
    CAMERA = "camera"  # カメラが使えない
    UNREADABLE = "unreadable"  # 覚えた顔を読めない
    STOPPED = "stopped"  # 途中で止めた
    ERROR = "error"  # そのほかの失敗


RESULT_MESSAGES: Final = {
    LearnResult.LEARNED: "顔を覚えました（見本 {count} 個）",
    LearnResult.TOO_FEW: "見本が足りないので、覚えませんでした（見本 {count} 個）",
    LearnResult.CAMERA: "カメラが使えなかったので、覚えませんでした",
    LearnResult.UNREADABLE: UNREADABLE_MESSAGE,
    LearnResult.STOPPED: "途中で止めたので、覚えませんでした",
    LearnResult.ERROR: "顔を覚えられませんでした",
}


class CheckResult(Enum):
    """確認の機能の終わり方。"""

    DONE = "done"
    CAMERA = "camera"  # カメラが使えない
    STOPPED = "stopped"  # 途中で止めた
    ERROR = "error"  # そのほかの失敗


CHECK_MESSAGES: Final = {
    CheckResult.DONE: "確認を終えました",
    CheckResult.CAMERA: "カメラが使えなかったので、確認をやめました",
    CheckResult.STOPPED: "途中で止めました",
    CheckResult.ERROR: "確認に失敗しました",
}

# 見捨てた機能のスレッドが残ったときに、強制的に終える（テストで差し替える）
_force_exit: Callable[[int], object] = os._exit


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m osekkai.features.presence",
        description="見つけてあいさつの顔を覚える・消す・一覧を出す・追従と動きの量を確かめる",
    )
    # どれか 1 つを指定する
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true", help="覚えた人の名前と見本の数を出す")
    action.add_argument("--learn-face", metavar="ID", help="この名前で顔を覚える（a-z・0-9・_）")
    action.add_argument("--forget-face", metavar="ID", help="この名前の顔とあいさつの記録を消す")
    action.add_argument(
        "--forget-all", action="store_true", help="すべての顔とあいさつの記録を消す"
    )
    action.add_argument(
        "--watch-test",
        action="store_true",
        help="顔の位置・首の角度・目の向き・覚えた人かどうかを出し続ける（追従の確認）",
    )
    action.add_argument(
        "--motion-test", action="store_true", help="2 秒ごとに動きの量を出す（動きの量の確認）"
    )
    parser.add_argument(
        "--seconds",
        type=_seconds,
        metavar="N",
        help=(
            f"確認する秒数（1〜{MAX_CHECK_SECONDS} の整数。既定は追従が "
            f"{WATCH_DEFAULT_SECONDS} 秒、動きの量が {MOTION_DEFAULT_SECONDS} 秒）"
        ),
    )
    parser.add_argument("--call-name", metavar="NAME", help="覚える人の呼び名（話すときに使う）")
    parser.add_argument(
        "--fake-hardware",
        action="store_true",
        help="実機の代わりに端末で動かす（覚える・確認のとき）",
    )
    parser.add_argument("--config", type=Path, metavar="PATH", help="設定ファイルを指定する")
    args = parser.parse_args(argv)
    if args.learn_face is not None and args.call_name is None:
        parser.error(CALL_NAME_MISSING)
    if args.learn_face is None and args.call_name is not None:
        parser.error(CALL_NAME_ONLY)
    if args.seconds is not None and not (args.watch_test or args.motion_test):
        parser.error(SECONDS_ONLY)
    return args


def _seconds(text: str) -> int:
    """確認の秒数（1〜上限の整数）。誤りの文言に値は入れない。"""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= MAX_CHECK_SECONDS:
        raise argparse.ArgumentTypeError(f"秒数は 1 から {MAX_CHECK_SECONDS} の整数にしてください")
    return value


# --- 覚える機能 ---


class LearnOutcome:
    """覚える機能の終わりの知らせ。`count` は集めた見本の数。"""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: LearnResult | None = None
        self.count = 0


def make_learn_feature(
    person_id: str,
    call_name: str,
    outcome: LearnOutcome,
    *,
    camera: CameraPort,
    vision: VisionPort,
) -> type[Feature]:
    """その人の顔だけを覚える機能のクラスを作る。

    申告は見つけてあいさつと同じ名前（保存先が同じになる）・優先度・資源・設定のモデルで、
    self-check の項目はない。登録は `__init__(ctx, config)` を呼ぶので、カメラと映像の処理は
    ここで閉じ込めて渡す。
    """

    class LearnFeature:
        spec: ClassVar[FeatureSpec] = dataclasses.replace(PresenceFeature.spec, checks=())

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            if not isinstance(config, PresenceConfig):
                raise TypeError("見つけてあいさつの設定ではありません")
            self._ctx = ctx
            self._log = ctx.log

        def run(self, stop: threading.Event) -> None:
            try:
                outcome.result = self._run(stop)
            except Exception as error:
                self._log.warning("顔を覚えるのに失敗しました: %s", type(error).__name__)
                outcome.result = LearnResult.ERROR
            finally:
                outcome.done.set()

        def _run(self, stop: threading.Event) -> LearnResult:
            book = FaceBook.load(self._ctx.store)
            if not book.readable:
                return LearnResult.UNREADABLE
            lease = self._acquire(stop)
            if lease is None:
                return LearnResult.STOPPED
            with lease:
                try:
                    camera.start()
                except CameraUnavailable:
                    self._log.warning("カメラが使えないので、顔を覚えるのをやめます")
                    self._say(lease, texts.LEARN_FAILED)
                    return LearnResult.CAMERA
                try:
                    samples = self._collect(lease, stop)
                except CameraUnavailable:
                    self._log.warning("撮れなくなったので、顔を覚えるのをやめます")
                    self._say(lease, texts.LEARN_FAILED)
                    return LearnResult.CAMERA
                finally:
                    camera.stop()
                outcome.count = len(samples)
                self._log.info("顔の見本を集めました: %d 個", len(samples))
                if stop.is_set():
                    return LearnResult.STOPPED
                if len(samples) < MIN_SAMPLES:
                    self._say(lease, texts.LEARN_FAILED)  # 何も保存しない（5.5）
                    return LearnResult.TOO_FEW
                book.put(Person(person_id, call_name, tuple(samples)))  # 前の特徴は置き換える
                book.save()
                self._show(lease, texts.LCD_LEARNED, Face.HAPPY)
                self._say(lease, texts.fill(texts.LEARN_DONE, call_name))  # 5.4
                return LearnResult.LEARNED

        def _acquire(self, stop: threading.Event) -> Lease | None:
            return _acquire_waiting(self._ctx, RESOURCES, stop)

        def _collect(self, lease: Lease, stop: threading.Event) -> list[Embedding]:
            """0.5 秒ごとに、最も大きい顔の特徴を集める（最大 40 秒・40 個。5.1〜5.3）。"""
            clock = self._ctx.clock
            total = texts.LEARN_SAMPLES
            half = total // 2
            samples: list[Embedding] = []
            self._show(lease, texts.lcd_learning(0), Face.CALL)
            self._say(lease, texts.fill(texts.LEARN_START, call_name))
            deadline = clock.monotonic() + LEARN_DURATION_S
            while len(samples) < total and not stop.is_set():
                started = clock.monotonic()
                if started >= deadline:
                    break
                emb = self._sample()
                if emb is not None:
                    samples.append(emb)
                    self._show(lease, texts.lcd_learning(len(samples)), Face.CALL)  # 5.2
                    if len(samples) == half:
                        self._say(lease, texts.LEARN_TURN)  # 半分で 1 回だけ（5.3）
                if len(samples) >= total:
                    break
                elapsed = clock.monotonic() - started
                stop.wait(max(0.0, LEARN_INTERVAL_S - elapsed))
            return samples

        def _sample(self) -> Embedding | None:
            """撮って、最も大きい顔の特徴を返す。顔がない・特徴にできなければ None。"""
            frame = camera.capture()  # CameraUnavailable はそのまま出す
            try:
                face = largest(vision.detect(frame))
                if face is None:
                    return None
                return vision.embed(frame, face)
            except CameraUnavailable:
                raise
            except Exception as error:
                self._log.warning("顔の特徴にできませんでした: %s", type(error).__name__)
                return None

        def _show(self, lease: Lease, line1: str, face: Face) -> None:
            try:
                lease.display.show(center(line1), center(face.text()))
            except DeviceUnavailable as error:
                self._log.warning("LCD に出せませんでした: %s", type(error).__name__)

        def _say(self, lease: Lease, speech: str) -> None:
            try:
                lease.voice.say(speech)
            except (SpeechFailed, DeviceUnavailable) as error:
                self._log.warning("話せませんでした: %s", type(error).__name__)

    return LearnFeature


def _acquire_waiting(
    ctx: FeatureContext, resources: frozenset[Resource], stop: threading.Event
) -> Lease | None:
    """止める旗を見ながら、借りられるまで待つ。止められたら None。"""
    while not stop.is_set():
        try:
            return ctx.acquire(resources, wait=ACQUIRE_WAIT_S)
        except ResourceBusy:
            ctx.log.debug("資源が空いていないので、もう一度借ります")
    return None


# --- 確認の機能（9.1、9.2） ---


class CheckOutcome:
    """確認の機能の終わりの知らせ。"""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: CheckResult | None = None


def _run_check(log: logging.Logger, outcome: CheckOutcome, body: Callable[[], CheckResult]) -> None:
    """確認の本体を動かし、終わり方を知らせる。例外は型の名前だけを記録する。"""
    try:
        outcome.result = body()
    except CameraUnavailable as error:
        log.warning("カメラが使えないので、確認をやめます: %s", type(error).__name__)
        outcome.result = CheckResult.CAMERA
    except Exception as error:
        log.warning("確認に失敗しました: %s", type(error).__name__)
        outcome.result = CheckResult.ERROR
    finally:
        outcome.done.set()


def _check_config(config: FeatureConfig) -> PresenceConfig:
    if not isinstance(config, PresenceConfig):
        raise TypeError("見つけてあいさつの設定ではありません")
    return config


def watch_line(elapsed: float, face: FaceBox | None, out: TrackOutput, label: str) -> str:
    """追従の確認の 1 行。顔の位置・首の角度・目の向き・認識の結果（覚えた人かどうか）。"""
    if face is None:
        where = "顔 なし"
    else:
        where = f"顔 x={face.x:+.2f} y={face.y:+.2f} 大きさ={face.size:.2f}"
    return (
        f"{elapsed:5.1f} 秒  {where}  首 左右={out.pan:+.1f} 上下={out.tilt:+.1f}"
        f"  目={out.gaze:+.2f}  認識={label}"
    )


def make_watch_feature(
    seconds: int,
    outcome: CheckOutcome,
    *,
    camera: CameraPort,
    vision: VisionPort,
    emit: Callable[[str], object],
) -> type[Feature]:
    """追従の確認の機能のクラスを作る（9.1）。

    首を借り、`seconds` 秒の間、撮って顔を見つけ、追従の部品で首と目を決めて首を動かし、
    1 行ずつ `emit` に渡す。認識は 1 秒に 1 回で、覚えた人かどうかだけを出す。
    覚えた顔は読むだけで、書かない（読めなければ全員を知らない人として出す）。
    """

    class WatchFeature:
        spec: ClassVar[FeatureSpec] = dataclasses.replace(PresenceFeature.spec, checks=())

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            self._config = _check_config(config)
            self._ctx = ctx
            self._log = ctx.log

        def run(self, stop: threading.Event) -> None:
            _run_check(self._log, outcome, lambda: self._run(stop))

        def _run(self, stop: threading.Event) -> CheckResult:
            book = FaceBook.load(self._ctx.store)  # 読むだけ。保存しない
            if not book.readable:
                emit(UNREADABLE_WATCH_MESSAGE)
            lease = _acquire_waiting(self._ctx, frozenset({Resource.NECK}), stop)
            if lease is None:
                return CheckResult.STOPPED
            with lease:
                camera.start()
                try:
                    finished = self._watch(lease, stop, book)
                finally:
                    camera.stop()
                    self._ctx.set_idle_face(Face.NORMAL, 0.0)
            return CheckResult.DONE if finished else CheckResult.STOPPED

        def _watch(self, lease: Lease, stop: threading.Event, book: FaceBook) -> bool:
            """決めた秒数の間、見て出す。終わりまで見たら True、止められたら False。"""
            config = self._config
            clock = self._ctx.clock
            tracker = Tracker(
                pan_reverse=config.pan_reverse,
                tilt_reverse=config.tilt_reverse,
                gaze_reverse=config.gaze_reverse,
            )
            known = bool(book.people())
            started = clock.monotonic()
            sent_look: tuple[float, float] | None = None
            sent_idle: tuple[Face, float] | None = None
            recognized_at: float | None = None
            label = LABEL_NONE
            while not stop.is_set():
                began = clock.monotonic()
                elapsed = (began - started) / CHECK_SECOND_S  # 確認の秒
                if elapsed >= seconds:
                    return True
                frame = camera.capture()
                face = largest(vision.detect(frame))
                out = tracker.step(face, elapsed)
                if face is None:
                    label, recognized_at = LABEL_NONE, None
                elif not out.seen:
                    label = LABEL_WAITING
                elif recognized_at is None or elapsed - recognized_at >= RECOGNIZE_INTERVAL:
                    recognized_at = elapsed
                    emb = vision.embed(frame, face)
                    matched = (
                        known
                        and emb is not None
                        and book.best_match(emb, config.match_threshold) is not None
                    )
                    label = LABEL_KNOWN if matched else LABEL_UNKNOWN  # 誰かは出さない
                look = (out.pan, out.tilt)
                if look != sent_look:
                    try:
                        lease.neck.look(*look)
                    except LeaseRevoked:
                        self._log.info("首を取り上げられたので、確認をやめます")
                        return False
                    sent_look = look
                idle = (Face.HAPPY if out.seen else Face.NORMAL, out.gaze)
                if idle != sent_idle:
                    self._ctx.set_idle_face(*idle)
                    sent_idle = idle
                emit(watch_line(elapsed, face, out, label))
                spent = clock.monotonic() - began
                stop.wait(max(0.0, WATCH_EVERY * CHECK_SECOND_S - spent))
            return False

    return WatchFeature


def make_motion_feature(
    seconds: int,
    outcome: CheckOutcome,
    *,
    camera: CameraPort,
    vision: VisionPort,
    emit: Callable[[str], object],
) -> type[Feature]:
    """動きの量の確認の機能のクラスを作る（9.2）。

    `seconds` 秒の間、2 秒ごとに、カメラを借りる口と同じ測り方（1 秒に 12 回撮って比べ、
    1 回だけの大きな値は数えない）の動きの量を `emit` に渡す。終わりの端は残りの秒数で測る。
    """

    class MotionFeature:
        spec: ClassVar[FeatureSpec] = dataclasses.replace(PresenceFeature.spec, checks=())

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            _check_config(config)
            self._ctx = ctx
            self._log = ctx.log

        def run(self, stop: threading.Event) -> None:
            _run_check(self._log, outcome, lambda: self._run(stop))

        def _run(self, stop: threading.Event) -> CheckResult:
            camera.start()
            try:
                finished = self._measure(stop)
            finally:
                camera.stop()
            return CheckResult.DONE if finished else CheckResult.STOPPED

        def _measure(self, stop: threading.Event) -> bool:
            clock = self._ctx.clock
            before = camera.capture()
            for index in range(math.ceil(seconds / MOTION_EVERY)):
                at = min((index + 1) * MOTION_EVERY, seconds)  # この回の終わり（確認の秒）
                span = at - index * MOTION_EVERY
                deadline = clock.monotonic() + span * CHECK_SECOND_S
                values: list[float] = []
                while True:
                    if stop.wait(CHECK_SECOND_S / MOTION_HZ):
                        return False
                    after = camera.capture()
                    values.append(vision.motion(before, after))
                    before = after
                    if clock.monotonic() >= deadline:
                        break
                emit(f"{at:4d} 秒  動きの量 {_peak_motion(values):.3f}")
            return True

    return MotionFeature


# --- 入口 ---


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(False)
    paths = Paths()
    if args.list:
        return _list(paths)
    if args.learn_face is not None:
        problem = _check_learn_args(args.learn_face, args.call_name)
        if problem is not None:
            print(problem, file=sys.stderr)
            return EXIT_USAGE
    lock = InstanceLock(paths.lock_file)
    if not _try_lock(lock):
        # デバイスにも保存にも触らない（動いている方を乱さない。5.8）
        logger.error(LOCK_BUSY_MESSAGE)
        print(LOCK_BUSY_MESSAGE, file=sys.stderr)
        return EXIT_LOCK_BUSY
    try:
        if args.learn_face is not None:
            return _learn(args, paths, lock)
        if args.watch_test or args.motion_test:
            return _check(args, paths, lock)
        if args.forget_face is not None:
            return _forget_one(paths, args.forget_face)
        return _forget_all(paths)
    finally:
        lock.release()  # Runtime も放す。2 回呼んでもよい


def _check_learn_args(person_id: str, call_name: str) -> str | None:
    """覚える名前と呼び名を確かめ、誤りの文言を返す。文言に値は入れない。"""
    if not PERSON_ID.fullmatch(person_id):
        return MSG_PERSON_ID
    if not call_name.strip():
        return CALL_NAME_EMPTY
    return None


def _try_lock(lock: InstanceLock) -> bool:
    try:
        return lock.acquire()
    except OSError as error:  # ロックのファイルを作れない。触らない側に倒す
        logger.warning("%s: %s", LOCK_UNCHECKED_MESSAGE, type(error).__name__)
        return False


class _NotSynced:
    """基盤を動かさないときの時計の同期の口（壊れた保存を退避するときの名前にだけ使う）。"""

    def is_synchronized(self) -> bool:
        return False


def _store(paths: Paths) -> StateStore:
    return StateStore.for_feature(paths, FEATURE_NAME, SystemClock(_NotSynced()))


def _list(paths: Paths) -> int:
    """覚えた人の名前と見本の数を出す（5.9）。呼び名は出さない。"""
    book = FaceBook.load(_store(paths))
    if not book.readable:
        print(UNREADABLE_MESSAGE, file=sys.stderr)
        return EXIT_FAILED
    people = book.people()
    if not people:
        print(NO_PEOPLE_MESSAGE)
        return EXIT_OK
    for person in people:
        print(f"{person.person_id}\t見本 {len(person.samples)} 個")
    return EXIT_OK


def _forget_one(paths: Paths, person_id: str) -> int:
    """1 人の特徴とあいさつの記録を消す（5.6）。"""
    store = _store(paths)
    book = FaceBook.load(store)
    if not book.readable:
        print(UNREADABLE_MESSAGE, file=sys.stderr)  # 読めないまま上書きしない
        return EXIT_FAILED
    if not book.remove(person_id):
        print(UNKNOWN_PERSON_MESSAGE, file=sys.stderr)
        return EXIT_USAGE
    try:
        book.save()
        GreetingRecords.load(store).forget(person_id)
    except OSError as error:
        logger.error("%s: %s", SAVE_FAILED_MESSAGE, type(error).__name__)
        print(SAVE_FAILED_MESSAGE, file=sys.stderr)
        return EXIT_FAILED
    left = len(book.people())
    logger.info("1 人の顔とあいさつの記録を消しました（残り %d 人）", left)
    print(f"1 人の顔とあいさつの記録を消しました（残り {left} 人）")
    return EXIT_OK


def _forget_all(paths: Paths) -> int:
    """すべての特徴とあいさつの記録を消す（5.7）。退避した読めない保存も消す。"""
    store = _store(paths)
    book = FaceBook.load(store)
    count = len(book.people())
    book.clear()
    try:
        book.save()
        GreetingRecords.load(store).forget(None)
        for key in (FACES_KEY, GREETINGS_KEY):
            for path in store.directory.glob(f"{key}.corrupt-*.json"):
                path.unlink(missing_ok=True)
    except OSError as error:
        logger.error("%s: %s", SAVE_FAILED_MESSAGE, type(error).__name__)
        print(SAVE_FAILED_MESSAGE, file=sys.stderr)
        return EXIT_FAILED
    logger.info("すべての顔とあいさつの記録を消しました（%d 人）", count)
    print(f"すべての顔とあいさつの記録を消しました（{count} 人）")
    return EXIT_OK


def _load_config(args: argparse.Namespace, paths: Paths) -> PresenceConfig | int:
    """設定を読む。だめなら理由を出して終了コードを返す。"""
    try:
        report = ConfigLoader(paths, {FEATURE_NAME: PresenceConfig}).load(args.config)
    except OSError as error:
        print(
            f"{CONFIG_ERROR_HEAD}: 設定ファイルを読めません（{type(error).__name__}）",
            file=sys.stderr,
        )
        return EXIT_FAILED
    # 基盤の文言は値を含まない（場所と理由だけ）
    messages = [*report.fatal_errors, *report.feature_errors.get(FEATURE_NAME, ())]
    config = report.features.get(FEATURE_NAME, PresenceConfig())
    if messages or not isinstance(config, PresenceConfig):
        for message in messages:
            print(f"{CONFIG_ERROR_HEAD}: {message}", file=sys.stderr)
        return EXIT_FAILED
    if not config.enabled:
        print(DISABLED_MESSAGE, file=sys.stderr)
        return EXIT_FAILED
    return config


def _learn(args: argparse.Namespace, paths: Paths, lock: InstanceLock) -> int:
    """顔を覚える（5.1〜5.5）。覚えた顔が読めなければ始めない。"""
    config = _load_config(args, paths)
    if isinstance(config, int):
        return config
    if not FaceBook.load(_store(paths)).readable:
        print(UNREADABLE_MESSAGE, file=sys.stderr)  # ほかの人の特徴を黙って消さない
        return EXIT_FAILED
    camera, vision = choose_devices(config)
    if camera is None or vision is None:
        print(NO_CAMERA_MESSAGE, file=sys.stderr)
        return EXIT_FAILED

    outcome = LearnOutcome()
    feature = make_learn_feature(
        args.learn_face, args.call_name, outcome, camera=camera, vision=vision
    )
    ran = _run_alone(args, paths, lock, feature, outcome.done, NOT_STARTED_MESSAGE)
    if isinstance(ran, int):
        return ran

    result = outcome.result or LearnResult.STOPPED
    logger.info("顔を覚えるのを終えました: %s", result.value)
    message = RESULT_MESSAGES[result].format(count=outcome.count)
    exit_code = EXIT_OK if result is LearnResult.LEARNED else EXIT_FAILED
    print(message, file=sys.stdout if exit_code == EXIT_OK else sys.stderr)
    return _finish(exit_code, abandoned=ran.abandoned)


def _check(args: argparse.Namespace, paths: Paths, lock: InstanceLock) -> int:
    """追従の確認か動きの量の確認を、決めた秒数の間行う（9.1、9.2）。"""
    config = _load_config(args, paths)
    if isinstance(config, int):
        return config
    camera, vision = choose_devices(config)
    if camera is None or vision is None:
        print(NO_CAMERA_CHECK_MESSAGE, file=sys.stderr)
        return EXIT_FAILED

    outcome = CheckOutcome()
    if args.watch_test:
        seconds = args.seconds or WATCH_DEFAULT_SECONDS
        feature = make_watch_feature(seconds, outcome, camera=camera, vision=vision, emit=_emit)
    else:
        seconds = args.seconds or MOTION_DEFAULT_SECONDS
        feature = make_motion_feature(seconds, outcome, camera=camera, vision=vision, emit=_emit)
    ran = _run_alone(args, paths, lock, feature, outcome.done, CHECK_NOT_STARTED_MESSAGE)
    if isinstance(ran, int):
        return ran

    result = outcome.result or CheckResult.STOPPED
    logger.info("確認を終えました: %s", result.value)
    ok = result in (CheckResult.DONE, CheckResult.STOPPED)
    exit_code = EXIT_OK if ok else EXIT_FAILED
    print(CHECK_MESSAGES[result], file=sys.stdout if ok else sys.stderr)
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
    feature: type[Feature],
    done: threading.Event,
    not_started: str,
) -> _Ran | int:
    """基盤を `feature` 1 つだけの登録で動かし、`done` が立つか Ctrl+C まで待つ。

    取ったロックはそのまま `Runtime` に渡す。始める前に終えたときは終了コードを返す。
    """
    registry = FeatureRegistry([feature])
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
        if feature.spec.name not in runtime.started_features:
            print(not_started, file=sys.stderr)
            return EXIT_FAILED
        while not done.wait(WAIT_POLL_S):
            pass
    except KeyboardInterrupt:
        logger.info("Ctrl+C を受けたので止めます")
    finally:
        runtime.shutdown()
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
