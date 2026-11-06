"""起動の口（`python -m osekkai`）。

引数を解析し、ログ・パス・アダプタ・機能の発見を組み立てて Runtime に渡す。
アダプタを選ぶのはここだけにする。

単一の実行のロックは、機能の発見とデバイスを作る前に取って Runtime に渡す（1.7）。
ほかが動いていれば、機能を読み込まず、デバイスを 1 つも作らずに 75 で終える
（動いている方の LCD・サーボ・モーター・HAT に触らない）。
`--say` のときは声のデバイスだけを作る（13.5）。

`--self-check` のときは、デバイスも Runtime も作らず、確認の項目だけを動かす
（device-deployment 6.1〜6.3、6.7、6.8）。ロックを取れればハードを軽く確かめ、
取れなければ動いているサービスの状態のファイルで判定する（ハードに触らない）。
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

from osekkai.checks import Probes, base_checks
from osekkai.core.clock import SystemClock
from osekkai.core.config import ConfigLoader, ConfigReport, CoreConfig
from osekkai.core.feature import feature_models
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.lcd_text import GLYPH_PATTERNS
from osekkai.core.logging_setup import configure_logging
from osekkai.core.paths import Paths
from osekkai.core.ports import Devices
from osekkai.core.registry import FeatureRegistry
from osekkai.core.runtime import ExitCode, Runtime, report_already_running
from osekkai.core.selfcheck import (
    CheckContext,
    SelfCheckRunner,
    collect_checks,
    exit_code,
    render_lines,
    results_to_json,
)
from osekkai.core.status import STATUS_FILE_NAME, ServiceSnapshot, read_snapshot

logger = logging.getLogger("osekkai.cli")

# 実機のアダプタ（7.x で足す）。関数の中で遅れて読み込む（ハードのライブラリを PC で読まない）
PI_ADAPTER_MODULE: Final = "osekkai.adapters.pi"
NO_PI_ADAPTER_MESSAGE: Final = (
    "実機のアダプタがありません。--fake-hardware を付けて動かしてください"
)
# self-check の実機の軽い確かめ（ハードのライブラリは確かめのときに読み込む）
PI_PROBES_MODULE: Final = "osekkai.adapters.pi.probes"
CONFIG_UNREADABLE: Final = "設定ファイルを読めません"


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
    parser.add_argument("--self-check", action="store_true", help="実機の準備を確かめて終える")
    parser.add_argument("--json", action="store_true", help="self-check の結果を JSON で出す")
    parser.add_argument("--no-hardware", action="store_true", help="self-check でハードに触らない")
    return parser.parse_args(argv)


def build_devices(args: argparse.Namespace, paths: Paths, registry: FeatureRegistry) -> Devices:
    """アダプタを選んでデバイスを作る。単一の実行のロックを取ってから呼ぶ。"""
    if args.fake_hardware:
        from osekkai.adapters.fake.console import build_console_devices

        return build_console_devices(time)
    return _build_pi_devices(paths, registry, args.config, voice_only=args.say is not None)


def _build_pi_devices(
    paths: Paths, registry: FeatureRegistry, config: Path | None, *, voice_only: bool
) -> Devices:
    try:
        module = importlib.import_module(PI_ADAPTER_MODULE)
    except ImportError as error:
        raise HardwareUnavailable(type(error).__name__) from error
    core = _core_config(paths, registry, config)
    try:
        devices: Devices
        if voice_only:
            # 話して終わるには声だけが要る（LCD・サーボ・モーター・マイコンに触らない）
            devices = module.build_pi_voice_devices(core)
        else:
            # 絵の文字の表は基盤のもの。アダプタは core.lcd_text を読み込まないので、ここから渡す
            devices = module.build_pi_devices(core, GLYPH_PATTERNS)
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


class _MissingProbes:
    """実機の確かめを読み込めないときの身代わり。どの確かめも失敗にする。"""

    def i2c_responds(self, address: int) -> bool:
        raise HardwareUnavailable

    def gpio_input_free(self, pin: int) -> bool:
        raise HardwareUnavailable

    def audio_sink_exists(self, sink: str) -> bool:
        raise HardwareUnavailable

    def synthesize(self, settings: object, out: Path) -> None:
        raise HardwareUnavailable

    def time_synchronized(self) -> bool:
        raise HardwareUnavailable


def _build_probes(fake_hardware: bool) -> Probes:
    """確かめの口を選ぶ。作るだけではハードに触らない。"""
    if fake_hardware:
        from osekkai.adapters.fake.probes import FakeProbes

        return FakeProbes()
    try:
        module = importlib.import_module(PI_PROBES_MODULE)
        probes: Probes = module.PiProbes()
    except ImportError as error:
        # ハードでない項目（設定・保存先）は確かめ、残りはだめにする
        logger.error("実機の確かめを読み込めません: %s", type(error).__name__)
        return _MissingProbes()
    return probes


def _load_report(paths: Paths, registry: FeatureRegistry, config: Path | None) -> ConfigReport:
    """self-check の設定の読み込み。確かめるだけなので、ファイルがなくても作らない。"""
    models = feature_models(cls.spec for cls in registry.feature_classes())
    try:
        return ConfigLoader(paths, models).load(config, create_missing=False)
    except OSError as error:  # 見本から作れないなど。中身（パス）は出さない
        logger.error("%s: %s", CONFIG_UNREADABLE, type(error).__name__)
        return ConfigReport(core=CoreConfig(), features={}, fatal_errors=[CONFIG_UNREADABLE])


def _try_lock(lock: InstanceLock) -> bool:
    try:
        return lock.acquire()
    except OSError as error:  # ロックのファイルを作れない。ハードには触らない側に倒す
        logger.warning("単一の実行のロックを確かめられません: %s", type(error).__name__)
        return False


def self_check(args: argparse.Namespace, paths: Paths) -> int:
    """確認の項目を動かして結果を出す。だめが 1 つでもあれば 1。"""
    lock = InstanceLock(paths.lock_file)
    acquired = _try_lock(lock)
    try:
        service: ServiceSnapshot | None = None
        if not acquired:
            # 動いているサービスの報告で判定する（ハードを取り合わない。6.7）
            service = read_snapshot(paths.runtime_dir / STATUS_FILE_NAME)
        # 機能は申告（spec）を読むだけで、作らない・開始しない
        registry = FeatureRegistry()
        report = _load_report(paths, registry, args.config)
        probes = _build_probes(args.fake_hardware)
        checks = collect_checks(
            base_checks(probes), (cls.spec for cls in registry.feature_classes())
        )
        ctx = CheckContext(
            paths=paths,
            report=report,
            service=service,
            hardware_allowed=acquired and not args.no_hardware,
        )
        results = SelfCheckRunner().run(checks, ctx)
    finally:
        lock.release()
    if args.json:
        print(results_to_json(results))
    else:
        for line in render_lines(results):
            print(line)
    return exit_code(results)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    paths = Paths()
    if args.self_check:
        return self_check(args, paths)
    lock = InstanceLock(paths.lock_file)
    if not lock.acquire():
        # デバイスは 1 つも作らず、機能のモジュールも読み込まない（1.7）
        return int(report_already_running())
    try:
        registry = FeatureRegistry()
        return _run_locked(args, paths, registry, lock)
    finally:
        lock.release()  # Runtime も放す。2 回呼んでもよい


def _run_locked(
    args: argparse.Namespace, paths: Paths, registry: FeatureRegistry, lock: InstanceLock
) -> int:
    try:
        devices = build_devices(args, paths, registry)
    except HardwareUnavailable as error:
        logger.error("実機のアダプタを読み込めません: %s", error)
        print(NO_PI_ADAPTER_MESSAGE, file=sys.stderr)
        return int(ExitCode.FATAL)

    clock = SystemClock(devices.timesync)
    runtime = Runtime(devices, paths, clock, registry, config_override=args.config, lock=lock)
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
