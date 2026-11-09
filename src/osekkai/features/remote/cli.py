"""センサーを確かめる道具（14.1〜14.3）。

`python -m osekkai.features.remote --sensor-test [--seconds N] [--fake-hardware] [--config PATH]`

N 秒（1〜600、既定 30）の間、0.5 秒ごとに、前の物の距離・床の 3 つの値・段差の判定・
設定（`stop_cm`・`cliff_stop`・`cliff_reference`）での危険の判定を 1 行ずつ出す（14.1）。
判定は走行と同じ `drive_rules.judge_front` で行う。

- 単一の実行のロックを取る。取れなければセンサーに触らず、止めてから実行するよう示して
  75 で終える（14.2）
- 基盤を起動しない。モーターとサーボに触らない（14.3）。センサーだけを開く
- `--fake-hardware` は、いつも安全な偽のセンサー（`ClearSensors`）を使う（PC で確かめる）
- `remote.enabled` が偽でも動かす。保護者が、ラジコンを使えるようにする前に
  部屋に合った止まる距離を確かめるための道具なので

終了コード: 0 は確かめ終えた（Ctrl+C で途中で止めたときも）、1 はセンサーを使えない・
設定の誤り、2 は指定の誤り、75 はロボットが動いている。

距離と床の値は画面（標準出力）にだけ出す。ログには決まった文言と例外の型の名前だけを出し、
設定の値は出さない。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from osekkai.core.clock import Clock, SystemClock
from osekkai.core.config import ConfigLoader
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.logging_setup import configure_logging
from osekkai.core.paths import Paths

from .adapters.fake import ClearSensors
from .adapters.sensors import HatSensors, sensors_available
from .config import RemoteConfig
from .drive_rules import Danger, judge_front
from .ports import SensorPort, SensorReading

logger = logging.getLogger(__name__)

FEATURE_NAME: Final = "remote"

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
EXIT_LOCK_BUSY: Final = 75  # 基盤の ExitCode.ALREADY_RUNNING と同じ値

DEFAULT_SECONDS: Final = 30
MAX_SECONDS: Final = 600
INTERVAL_S: Final = 0.5

LOCK_BUSY_MESSAGE: Final = "ロボットが動いています。止めてから実行してください"
LOCK_UNCHECKED_MESSAGE: Final = "単一の実行のロックを確かめられません"
CONFIG_ERROR_HEAD: Final = "設定の誤り"
NO_SENSORS_MESSAGE: Final = (
    "センサーのライブラリがないので確かめられません（PC では --fake-hardware を付けてください）"
)
DONE_MESSAGE: Final = "確かめ終えました"
STOPPED_MESSAGE: Final = "途中で止めました"

UNREADABLE: Final = "読めない"
FAR: Final = "遠い"
CLIFF_YES: Final = "段差あり"
CLIFF_NO: Final = "段差なし"
CLIFF_OFF: Final = "判定しない"
VERDICTS: Final = {
    Danger.NONE: "ふつう",
    Danger.OBSTACLE: "まえに なにか",
    Danger.EDGE: "だんさ",
    Danger.UNKNOWN: "読めない(危険)",
}

_UNREADABLE_READING: Final = SensorReading(distance_cm=None, floor=(None, None, None))


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m osekkai.features.remote",
        description="ラジコンの前の距離と床の段差のセンサーを確かめる",
    )
    parser.add_argument(
        "--sensor-test",
        action="store_true",
        required=True,
        help="距離・床の値・段差・危険の判定を 0.5 秒ごとに出す",
    )
    parser.add_argument(
        "--seconds",
        type=_seconds,
        default=DEFAULT_SECONDS,
        metavar="N",
        help=f"確かめる秒数（1〜{MAX_SECONDS} の整数。既定は {DEFAULT_SECONDS}）",
    )
    parser.add_argument(
        "--fake-hardware",
        action="store_true",
        help="実機の代わりに、いつも安全な偽のセンサーを使う",
    )
    parser.add_argument("--config", type=Path, metavar="PATH", help="設定ファイルを指定する")
    return parser.parse_args(argv)


def _seconds(text: str) -> int:
    """確かめる秒数（1〜上限の整数）。誤りの文言に値は入れない。"""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= MAX_SECONDS:
        raise argparse.ArgumentTypeError(f"秒数は 1 から {MAX_SECONDS} の整数にしてください")
    return value


# --- 差し替えられる口（テストで替える） ---


class _NotSynced:
    """時刻の同期を問わない（道具は経った時間だけを使う）。"""

    def is_synchronized(self) -> bool:
        return False


def make_clock() -> Clock:
    return SystemClock(_NotSynced())


def make_fake_sensors() -> SensorPort:
    return ClearSensors()


def make_hat_sensors(clock: Clock) -> SensorPort:
    return HatSensors(clock=clock)


# --- 1 行の作り方 ---


def _number(value: int | None) -> str:
    return UNREADABLE if value is None or value < 0 else str(value)


def _distance_text(reading: SensorReading) -> str:
    if reading.far:
        return FAR
    if reading.distance_cm is None:
        return UNREADABLE
    return f"{reading.distance_cm:.1f} cm"


def _cliff_text(reading: SensorReading, config: RemoteConfig) -> str:
    if not config.cliff_stop:
        return CLIFF_OFF
    floor = reading.floor
    if any(value is None or value < 0 for value in floor):
        return UNREADABLE
    if any(value is not None and value <= config.cliff_reference for value in floor):
        return CLIFF_YES
    return CLIFF_NO


def sensor_line(reading: SensorReading, config: RemoteConfig) -> str:
    """道具の 1 行。画面にだけ出す。"""
    danger = judge_front(
        reading,
        stop_cm=config.stop_cm,
        cliff_stop=config.cliff_stop,
        cliff_reference=config.cliff_reference,
    )
    floor = " ".join(_number(value) for value in reading.floor)
    return (
        f"距離 {_distance_text(reading)}  床 {floor}  "
        f"{_cliff_text(reading, config)}  {VERDICTS[danger]}"
    )


# --- 入口 ---


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(False)
    paths = Paths()
    lock = InstanceLock(paths.lock_file)
    if not _try_lock(lock):
        # センサーに触らない（動いている方を乱さない。14.2）
        logger.error(LOCK_BUSY_MESSAGE)
        print(LOCK_BUSY_MESSAGE, file=sys.stderr)
        return EXIT_LOCK_BUSY
    try:
        config = _load_config(args, paths)
        if isinstance(config, int):
            return config
        return _sensor_test(args, config)
    finally:
        lock.release()


def _try_lock(lock: InstanceLock) -> bool:
    try:
        return lock.acquire()
    except OSError as error:  # ロックのファイルを作れない。触らない側に倒す
        logger.warning("%s: %s", LOCK_UNCHECKED_MESSAGE, type(error).__name__)
        return False


def _load_config(args: argparse.Namespace, paths: Paths) -> RemoteConfig | int:
    """ラジコンの設定だけを読む。だめなら理由を出して終了コードを返す。"""
    try:
        report = ConfigLoader(paths, {FEATURE_NAME: RemoteConfig}).load(args.config)
    except OSError as error:
        print(
            f"{CONFIG_ERROR_HEAD}: 設定ファイルを読めません（{type(error).__name__}）",
            file=sys.stderr,
        )
        return EXIT_FAILED
    # 基盤の文言は値を含まない（場所と理由だけ）
    messages = [*report.fatal_errors, *report.feature_errors.get(FEATURE_NAME, ())]
    config = report.features.get(FEATURE_NAME)
    if messages or not isinstance(config, RemoteConfig):
        for message in messages:
            print(f"{CONFIG_ERROR_HEAD}: {message}", file=sys.stderr)
        return EXIT_FAILED
    return config


def _sensor_test(args: argparse.Namespace, config: RemoteConfig) -> int:
    """決めた秒数の間、0.5 秒ごとに 1 行を出す（14.1）。タイヤには触らない（14.3）。"""
    clock = make_clock()
    if args.fake_hardware:
        sensors = make_fake_sensors()
    elif sensors_available():
        sensors = make_hat_sensors(clock)
    else:
        print(NO_SENSORS_MESSAGE, file=sys.stderr)
        return EXIT_FAILED
    rounds = int(args.seconds / INTERVAL_S)
    try:
        start = clock.monotonic()
        for index in range(rounds):
            print(sensor_line(_read(sensors), config), flush=True)
            wait = start + (index + 1) * INTERVAL_S - clock.monotonic()
            if wait > 0:
                clock.sleep(wait)
    except KeyboardInterrupt:
        logger.info("Ctrl+C を受けたので止めます")
        print(STOPPED_MESSAGE)
        return EXIT_OK
    finally:
        _close(sensors)
    print(DONE_MESSAGE)
    return EXIT_OK


def _read(sensors: SensorPort) -> SensorReading:
    try:
        return sensors.read()
    except Exception as error:  # 読めないものとして出す（危険の側）
        logger.warning("センサーを読めませんでした: %s", type(error).__name__)
        return _UNREADABLE_READING


def _close(sensors: SensorPort) -> None:
    try:
        sensors.close()
    except Exception as error:
        logger.warning("センサーを閉じられませんでした: %s", type(error).__name__)
