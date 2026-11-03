"""起動の口（`python -m osekkai`）。

引数を解析し、ログ・パス・アダプタ・機能の発見を組み立てて Runtime に渡す。
アダプタを選ぶのはここだけにする。
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, TextIO

from osekkai.core.clock import SystemClock
from osekkai.core.config import ConfigLoader, CoreConfig
from osekkai.core.feature import feature_models
from osekkai.core.lcd_text import GLYPH_PATTERNS
from osekkai.core.logging_setup import configure_logging
from osekkai.core.paths import Paths
from osekkai.core.ports import Devices
from osekkai.core.registry import FeatureRegistry
from osekkai.core.runtime import ExitCode, Runtime

logger = logging.getLogger("osekkai.cli")

# 実機のアダプタ（7.x で足す）。関数の中で遅れて読み込む（ハードのライブラリを PC で読まない）
PI_ADAPTER_MODULE: Final = "osekkai.adapters.pi"
NO_PI_ADAPTER_MESSAGE: Final = (
    "実機のアダプタがありません。--fake-hardware を付けて動かしてください"
)

# 見捨てた機能のスレッドが残ったときに、強制的に終える（テストで差し替える）
_force_exit: Callable[[int], object] = os._exit


class HardwareUnavailable(Exception):
    """実機のアダプタを読み込めない。"""


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="osekkai", description="おせっかいローバーの基盤")
    parser.add_argument(
        "--fake-hardware",
        action="store_true",
        help="実機の代わりに端末で動かす（Enter キーが USR ボタン）",
    )
    parser.add_argument("--config", type=Path, metavar="PATH", help="設定ファイルを指定する")
    parser.add_argument("--say", metavar="TEXT", help="文章を話して終える")
    parser.add_argument("--verbose", action="store_true", help="詳しいログを出す")
    return parser.parse_args(argv)


def build_devices(args: argparse.Namespace, paths: Paths, registry: FeatureRegistry) -> Devices:
    """アダプタを選んでデバイス一式を作る。"""
    if args.fake_hardware:
        from osekkai.adapters.fake.console import build_console_devices

        return build_console_devices(time)
    return _build_pi_devices(paths, registry, args.config)


def _build_pi_devices(paths: Paths, registry: FeatureRegistry, config: Path | None) -> Devices:
    try:
        module = importlib.import_module(PI_ADAPTER_MODULE)
    except ImportError as error:
        raise HardwareUnavailable(type(error).__name__) from error
    core = _core_config(paths, registry, config)
    try:
        # 絵の文字の表は基盤のもの。アダプタは core.lcd_text を読み込まないので、ここから渡す
        devices: Devices = module.build_pi_devices(core, GLYPH_PATTERNS)
    except ImportError as error:  # ハードのライブラリがない（PC）
        raise HardwareUnavailable(type(error).__name__) from error
    return devices


def _core_config(paths: Paths, registry: FeatureRegistry, config: Path | None) -> CoreConfig:
    # 実機の配線（LCD のアドレスなど）を知るために、基盤の設定だけを先に読む
    models = feature_models(cls.spec for cls in registry.feature_classes())
    try:
        return ConfigLoader(paths, models).load(config).core
    except OSError:
        return CoreConfig()  # Runtime が改めて読み、誤りを知らせる


def _flush() -> None:
    targets: list[logging.Handler | TextIO] = [*logging.getLogger("osekkai").handlers]
    targets += [sys.stdout, sys.stderr]
    for target in targets:
        with contextlib.suppress(OSError, ValueError):
            target.flush()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    paths = Paths()
    registry = FeatureRegistry()
    try:
        devices = build_devices(args, paths, registry)
    except HardwareUnavailable as error:
        logger.error("実機のアダプタを読み込めません: %s", error)
        print(NO_PI_ADAPTER_MESSAGE, file=sys.stderr)
        return int(ExitCode.FATAL)

    clock = SystemClock(devices.timesync)
    runtime = Runtime(devices, paths, clock, registry, config_override=args.config)
    if args.say is not None:
        return int(runtime.say_and_exit(args.say))

    code = runtime.run()
    if runtime.abandoned:
        # 見捨てた機能のスレッドは daemon でないので、待たずに終える
        _flush()
        _force_exit(int(code))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
