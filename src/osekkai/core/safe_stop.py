"""ExecStopPost 用の安全停止のコマンド（`python -m osekkai.core.safe_stop`）。

設定と機能を読まない。モーターの方式によらず止まるよう、次を順に行う（2.1、2.4、8.4）。
各段階の失敗は型の名前だけを記録して次へ進む。
1. モーターに関わりうる PWM（P12〜P15）をすべて 0 にする
2. 方向ピン（D4、D5）を Low にする
3. マイコンをリセットする
実機のアダプタやハードのライブラリがなければ（PC）、何もせずに終える。
2 つ目の起動の後（`EXIT_STATUS=75`）と、ロックをほかが持っているときは、
動いている方を止めないように何もしない。

終了コード:
- 付けずに呼ぶ（ExecStopPost）: systemd の停止を乱さないよう、いつも 0。
- `--strict`（入れ替えの道具の halt）: モーターを止めたと確かめられたときだけ 0、
  それ以外は 1。段階の失敗に加えて、ハードに触らずに終えたとき（ロックをほかが
  持っている・`EXIT_STATUS=75`・実機のアダプタがない）も 1 にする。道具は両方の
  ユニットを止めてから呼ぶので、ロックが持たれていれば、まだ何かが動いている。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import logging
import os
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Final, Protocol

from osekkai.core.logging_setup import configure_logging
from osekkai.core.paths import Paths

logger = logging.getLogger("osekkai.safe_stop")

ALREADY_RUNNING_STATUS: Final = "75"


class SafeStopHardware(Protocol):
    def zero_motor_pwm(self) -> None: ...
    def direction_pins_low(self) -> None: ...
    def reset_mcu(self) -> None: ...
    def close(self) -> None: ...


HardwareOpener = Callable[[], SafeStopHardware]


def _open_pi_hardware() -> SafeStopHardware:
    from osekkai.adapters.pi.picarx import open_safe_stop_hardware  # 実機だけ（遅れて読み込む）

    return open_safe_stop_hardware()


@contextlib.contextmanager
def _hold_lock_if_free(path: Path) -> Iterator[bool]:
    """ロックのファイルがあれば取ってみる。ほかが持っていれば False。ファイルは作らない。"""
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        # ない（FileNotFoundError）・読めない（PermissionError など）は止める側に倒す
        yield True
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)  # 閉じると flock も外れる


def run_safe_stop(
    environ: Mapping[str, str],
    open_hardware: HardwareOpener | None = None,
    *,
    strict: bool = False,
) -> list[str]:
    """安全停止を行い、失敗した段階の名前を返す。

    strict のときは、ハードに触らずに終えた理由（"exit_status"・"lock"・"adapter"）も
    失敗として返す。モーターを止めたと確かめられていないため。
    """
    if environ.get("EXIT_STATUS") == ALREADY_RUNNING_STATUS:
        logger.info("すでに動いている方がいるので、安全停止を省きます")
        return ["exit_status"] if strict else []
    with _hold_lock_if_free(Paths(environ).lock_file) as free:
        if not free:
            logger.info("ほかのプロセスが動いているので、安全停止を省きます")
            return ["lock"] if strict else []
        try:
            hardware = (open_hardware or _open_pi_hardware)()
        except ImportError:
            logger.info("実機のアダプタがないので、安全停止を省きます")
            return ["adapter"] if strict else []
        except Exception as error:
            logger.warning("安全停止のハードを開けませんでした: %s", type(error).__name__)
            return ["open"]
        failed: list[str] = []
        steps: tuple[tuple[str, Callable[[], None]], ...] = (
            ("pwm", hardware.zero_motor_pwm),
            ("pins", hardware.direction_pins_low),
            ("reset", hardware.reset_mcu),
        )
        try:
            for name, step in steps:
                try:
                    step()
                except Exception as error:
                    failed.append(name)
                    logger.warning("安全停止の段階 %s に失敗: %s", name, type(error).__name__)
        finally:
            try:
                hardware.close()
            except Exception as error:
                logger.warning("安全停止のハードを閉じられませんでした: %s", type(error).__name__)
        if not failed:
            logger.info("モーターを安全に止めました")
        return failed


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m osekkai.core.safe_stop", description="モーターを安全に止める"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="モーターを止めたと確かめられなければ終了コード 1 で終える",
    )
    return parser.parse_args(list(argv))


def main(
    environ: Mapping[str, str] | None = None,
    open_hardware: HardwareOpener | None = None,
    argv: Sequence[str] = (),
) -> int:
    """コマンドの入口。--strict がなければ、systemd の停止を乱さないよう、いつも 0 で終える。"""
    strict = _parse_args(argv).strict
    env = os.environ if environ is None else environ
    try:
        failed = run_safe_stop(env, open_hardware, strict=strict)
    except Exception as error:
        logger.error("安全停止が途中で止まりました: %s", type(error).__name__)
        failed = ["unexpected"]
    if strict and failed:
        logger.warning("モーターを止めたと確かめられませんでした: %s", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    configure_logging(env=os.environ)
    raise SystemExit(main(argv=sys.argv[1:]))
