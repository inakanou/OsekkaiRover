"""起動の順番（1.1〜1.7、11.2〜11.4）。

単一の実行のロック → 版のログ → 設定 → 使えないデバイスのログ → HAT のリセット →
サービスの組み立て → 中立の姿勢 → 基盤のスレッド（イベント・ボタン・同期の見張り・待機画面）→
設定の誤りの表示 → 機能の開始 → 起動の声、の順に行う。

- 設定の誤りの表示は、優先度 `CONFIG_NOTICE_PRIORITY`（待機画面より 1 つ上で、どの機能より下）の
  内部の持ち主 `core.config` が DISPLAY を借りて出す。待機画面より先に借りるので、
  待機画面は空くまで待つ。機能が DISPLAY を借りに来たら直ちに返す
  - 致命の誤り: 返さずに出し続ける（機能は 1 つも開始しない）
  - 機能の節の誤り: `config_error_seconds` 秒（実際の秒数）後に返し、待機画面に戻す
- 起動の声はメインのスレッドで話し終えてから戻る（機能の開始の後）

停止（2.1〜2.4）は `run()` が扱う。

- SIGTERM・SIGINT のハンドラは、非常停止と停止の要求の記録だけを行う。ハンドラは
  メインのスレッドの待ちの途中に割り込むので、Event のロックは取らず、旗を立てるだけにする
  （メインのスレッドが `STOP_POLL_S` ごとに見る）。ほかのスレッドからは `request_stop()` を使う
- 機能の停止は合計 `stop_budget` 秒（既定 8 秒、実際の秒数）まで待ち、応えない機能は待たずに
  片付けに進む。その名前は `abandoned` に残る。見捨てた機能のスレッドは daemon でないので、
  呼び出す側（CLI）は `abandoned` が空でなければ `os._exit(終了コード)` で強制的に終える
- 基盤の異常（`run()` の中の例外と、名前が `osekkai-` で始まる基盤のスレッドの捕まえなかった
  例外）では、先にモーターを止めてから片付け、終了コード 1 を返す。機能のスレッド
  （`osekkai-feature-`）の例外は監督が扱うので、ここでは扱わない
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import fields
from datetime import time
from enum import IntEnum
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Final

from osekkai import version
from osekkai.core.arbiter import Arbiter, Lease
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.clock import TimeSyncMonitor
from osekkai.core.config import ConfigLoader, ConfigReport, CoreConfig
from osekkai.core.display import DisplayService, LedService
from osekkai.core.events import EventBus
from osekkai.core.feature import CoreFeatureContext, FeatureContext, FeatureSpec, feature_models
from osekkai.core.idle import IdleScreen
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.motion import MotionService
from osekkai.core.ports import Devices, Unavailable
from osekkai.core.quiet import QuietMode
from osekkai.core.safety import SafetyStop
from osekkai.core.storage import StateStore
from osekkai.core.supervisor import THREAD_PREFIX as FEATURE_THREAD_PREFIX
from osekkai.core.supervisor import FeatureSupervisor
from osekkai.core.types import DeviceUnavailable, Priority, Resource, SpeechFailed
from osekkai.core.voice import VoiceService

if TYPE_CHECKING:
    from osekkai.core.clock import Clock
    from osekkai.core.paths import Paths
    from osekkai.core.registry import FeatureRegistry

logger = logging.getLogger(__name__)

ALREADY_RUNNING_MESSAGE: Final = "すでに動いています"
STARTUP_VOICE: Final = "おせっかいローバー、きどう！"
CONFIG_ERROR_TEXT: Final = "ｾｯﾃｲ ｴﾗｰ"
CONFIG_NOTICE_OWNER: Final = "core.config"
CONFIG_NOTICE_PRIORITY: Final = Priority.IDLE + 1
CONFIG_ERROR_SECONDS: Final = 5.0
DEFAULT_STOP_BUDGET_S: Final = 8.0
STOP_POLL_S: Final = 0.05
CORE_THREAD_PREFIX: Final = "osekkai-"
STOP_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)


class ExitCode(IntEnum):
    OK = 0
    FATAL = 1
    ALREADY_RUNNING = 75


def within_hours(now: time, hours: tuple[time, time]) -> bool:
    """`now` が時間帯の中か。始まりは含み、終わりは含まない。日をまたぐ帯も扱う。

    始まりと終わりが同じなら、空の帯とする。
    """
    start, end = hours
    if start < end:
        return start <= now < end
    if start > end:
        return now >= start or now < end
    return False


class Runtime:
    """基盤の起動と停止。"""

    def __init__(
        self,
        devices: Devices,
        paths: Paths,
        clock: Clock,
        registry: FeatureRegistry,
        *,
        config_override: Path | None = None,
        config_error_seconds: float = CONFIG_ERROR_SECONDS,
        stop_budget: float = DEFAULT_STOP_BUDGET_S,
    ) -> None:
        self._devices = devices
        self._paths = paths
        self._clock = clock
        self._registry = registry
        self._config_override = config_override
        self._config_error_seconds = config_error_seconds
        self._stop_budget = stop_budget
        self._lock = InstanceLock(paths.lock_file)
        self._started = False
        self.report: ConfigReport | None = None
        self.motion: MotionService | None = None
        self.voice: VoiceService | None = None
        self.arbiter: Arbiter | None = None
        self.router: ButtonRouter | None = None
        self.bus: EventBus | None = None
        self.monitor: TimeSyncMonitor | None = None
        self.idle: IdleScreen | None = None
        self.quiet: QuietMode | None = None
        self.safety: SafetyStop | None = None
        self.supervisor: FeatureSupervisor | None = None
        self.started_features: list[str] = []
        self._notice: Lease | None = None
        self._notice_lock = threading.Lock()
        self._notice_stop = threading.Event()
        self._notice_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._signaled = False  # シグナルのハンドラが立てる旗（ロックを取らない）
        self._fatal: BaseException | None = None
        self.abandoned: list[str] = []

    # --- 実行 ---

    def run(self) -> ExitCode:
        """起動し、停止の要求か基盤の異常まで待ち、片付けて終了コードを返す。

        `abandoned` が空でなければ、残ったスレッドを呼び出す側が強制的に終える。
        """
        previous_signals = self._install_signal_handlers()
        previous_hook = threading.excepthook
        threading.excepthook = self._make_excepthook(previous_hook)
        try:
            return self._run()
        finally:
            threading.excepthook = previous_hook
            for signum, handler in previous_signals.items():
                signal.signal(signum, handler)

    def request_stop(self) -> None:
        """停止を求める。モーターは直ちに止める（2.1）。どのスレッドからでも呼べる。"""
        self._emergency_stop()
        self._stop_event.set()

    def report_fatal(self, error: BaseException) -> None:
        """基盤の異常を知らせる。モーターは直ちに止め、メインのスレッドが片付ける（2.4）。"""
        if self._fatal is None:
            self._fatal = error
        self.request_stop()

    def _run(self) -> ExitCode:
        try:
            code = self.start()
            if code is not None:
                return code
            # 起動の途中の要求でも、止めてから待ちを抜ける
            while not (self._stop_event.wait(STOP_POLL_S) or self._signaled):
                pass
        except Exception as error:
            self.report_fatal(error)
        self._emergency_stop()
        fatal = self._fatal
        if fatal is not None:
            logger.error("基盤に異常が起きたので止めます: %s", type(fatal).__name__)
        else:
            logger.info("停止の要求を受けたので止めます")
        self.shutdown(self._stop_budget)
        if self._fatal is not None:  # 片付けの間の異常も含める
            return ExitCode.FATAL
        return ExitCode.OK

    def _emergency_stop(self) -> None:
        motion = self.motion
        if motion is not None:
            motion.emergency_stop()  # 例外を外に出さない

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        # 非常停止と要求の記録だけを行う（2.1）
        self._emergency_stop()
        self._signaled = True

    def _install_signal_handlers(
        self,
    ) -> dict[int, signal.Handlers | Callable[..., object] | int | None]:
        previous: dict[int, signal.Handlers | Callable[..., object] | int | None] = {}
        # シグナルのハンドラはメインのスレッドでしか置けない
        if threading.current_thread() is not threading.main_thread():
            return previous
        for signum in STOP_SIGNALS:
            previous[signum] = signal.signal(signum, self._handle_signal)
        return previous

    def _make_excepthook(
        self, previous: Callable[[threading.ExceptHookArgs], object]
    ) -> Callable[[threading.ExceptHookArgs], None]:
        def hook(args: threading.ExceptHookArgs) -> None:
            previous(args)
            name = args.thread.name if args.thread is not None else ""
            if name.startswith(CORE_THREAD_PREFIX) and not name.startswith(FEATURE_THREAD_PREFIX):
                error = args.exc_value if args.exc_value is not None else RuntimeError()
                self.report_fatal(error)

        return hook

    # --- 話して終わる ---

    def say_and_exit(self, text: str) -> ExitCode:
        """声の出力だけを作って `text` を話し、終了コードを返す（13.5）。

        単一の実行のロックを取る。機能・待機画面・動きは始めず、声のほかのデバイスに触らない。
        """
        if not self._lock.acquire():
            logger.error(ALREADY_RUNNING_MESSAGE)
            print(ALREADY_RUNNING_MESSAGE, file=sys.stderr)
            return ExitCode.ALREADY_RUNNING
        try:
            core = self._load_config().core
            cache = CacheManager.from_paths(self._paths, core.cache_max_mb, self._clock)
            voice = VoiceService(self._devices.synth, self._devices.player, cache, core.voice)
            try:
                voice.speak(text)
            except (SpeechFailed, DeviceUnavailable) as error:
                # 文章はログに出さない（6.5）
                logger.error("話せませんでした: %s", type(error).__name__)
                return ExitCode.FATAL
            return ExitCode.OK
        finally:
            self._lock.release()

    # --- 起動 ---

    def start(self) -> ExitCode | None:
        """起動の順番を行う。始められたら None、ほかが動いていれば `ALREADY_RUNNING`。"""
        if not self._lock.acquire():
            # デバイスには触らない（1.7）
            logger.error(ALREADY_RUNNING_MESSAGE)
            print(ALREADY_RUNNING_MESSAGE, file=sys.stderr)
            return ExitCode.ALREADY_RUNNING
        self._started = True
        logger.info("版: %s", version.get_version())

        report = self._load_config()
        self.report = report
        core = report.core
        self._log_unavailable()
        self._reset_hat()

        devices = self._devices
        clock = self._clock
        motion = MotionService(devices.servos, devices.motors, core.motion, clock)
        cache = CacheManager.from_paths(self._paths, core.cache_max_mb, clock)
        voice = VoiceService(devices.synth, devices.player, cache, core.voice)
        display = DisplayService(devices.display)
        led = LedService(devices.led)
        router = ButtonRouter(devices.buttons, clock)
        arbiter = Arbiter(clock, motion, voice=voice, display=display, led=led, buttons=router)
        bus = EventBus()
        monitor = TimeSyncMonitor(clock, bus)
        idle = IdleScreen(arbiter, clock)
        quiet = QuietMode(idle, bus, StateStore.for_feature(self._paths, "core", clock), clock)
        safety = SafetyStop(motion, display, led)
        self.motion, self.voice, self.arbiter, self.router = motion, voice, arbiter, router
        self.bus, self.monitor, self.idle, self.quiet = bus, monitor, idle, quiet
        self.safety = safety

        # 機能を開始する前に中立の姿勢にする（1.1）
        for step in safety.neutral():
            logger.warning("中立の姿勢の段階 %s ができませんでした", step)

        # 設定の誤りの表示は、待機画面より先に DISPLAY を借りる（11.3）
        if report.fatal_errors or report.feature_errors:
            self._show_config_error()

        bus.start()
        router.start()
        monitor.start()
        idle.start()  # 待機画面（1.2）

        def context_factory(spec: FeatureSpec) -> FeatureContext:
            return CoreFeatureContext(
                spec,
                arbiter=arbiter,
                buttons=router,
                idle=idle,
                quiet=quiet,
                bus=bus,
                clock=clock,
                paths=self._paths,
                cache=cache,
            )

        # 開始しなかった機能と理由は、登録の側がログに出す
        plans = self._registry.plan(report)
        supervisor = FeatureSupervisor(arbiter, context_factory)
        self.supervisor = supervisor
        supervisor.start(plans)
        self.started_features = [plan.feature_cls.spec.name for plan in plans]
        for name in self.started_features:
            logger.info("機能 %s を開始しました", name)
        if not self.started_features:
            logger.info("開始した機能はありません")

        if report.feature_errors and not report.fatal_errors:
            self._schedule_config_error_end()

        self._startup_voice(core)
        return None

    def _load_config(self) -> ConfigReport:
        models = feature_models(cls.spec for cls in self._registry.feature_classes())
        try:
            return ConfigLoader(self._paths, models).load(self._config_override)
        except OSError as error:
            # 見本から作れなかった。基盤は既定値で動き、機能は開始しない（11.3）
            message = f"設定ファイルを見本から作れません: {type(error).__name__}"
            logger.error("設定の誤り（基盤）: %s", message)
            return ConfigReport(core=CoreConfig(), features={}, fatal_errors=[message])

    def _log_unavailable(self) -> None:
        for field in fields(Devices):
            device = getattr(self._devices, field.name)
            if isinstance(device, Unavailable):
                logger.warning(
                    "デバイス %s が使えません: %s（使わずに続けます）", device.device, device.reason
                )

    def _reset_hat(self) -> None:
        hat = self._devices.hat
        if isinstance(hat, Unavailable):
            return
        try:
            hat.reset()
        except Exception as error:
            logger.warning("HAT をリセットできませんでした: %s", type(error).__name__)

    def _startup_voice(self, core: CoreConfig) -> None:
        # 時刻が確かでないときは夜かもしれないので話さない（1.3、1.4）
        if not self._clock.is_synchronized():
            logger.info("時刻が確かでないので、起動の声を出しません")
            return
        if not within_hours(self._clock.now().time(), core.startup_voice_hours):
            logger.info("起動の声の時間帯の外なので、声を出しません")
            return
        voice = self.voice
        if voice is None:
            return
        try:
            voice.speak(STARTUP_VOICE)
        except Exception as error:
            logger.warning("起動の声を出せませんでした: %s", type(error).__name__)

    # --- 設定の誤りの表示 ---

    def _show_config_error(self) -> None:
        arbiter = self.arbiter
        if arbiter is None:
            return
        try:
            lease = arbiter.acquire(
                CONFIG_NOTICE_OWNER,
                CONFIG_NOTICE_PRIORITY,
                [Resource.DISPLAY],
                on_preempt=self._end_config_error,
            )
        except Exception as error:
            logger.warning("設定の誤りを画面に出せませんでした: %s", type(error).__name__)
            return
        with self._notice_lock:
            self._notice = lease
        try:
            lease.display.set_backlight(True)
            lease.display.show(CONFIG_ERROR_TEXT)
        except Exception as error:
            logger.warning("設定の誤りを画面に出せませんでした: %s", type(error).__name__)

    def _schedule_config_error_end(self) -> None:
        def wait_and_end() -> None:
            self._notice_stop.wait(self._config_error_seconds)
            self._end_config_error()

        thread = threading.Thread(target=wait_and_end, name="osekkai-config-notice", daemon=True)
        self._notice_thread = thread
        thread.start()

    def _end_config_error(self) -> None:
        """設定の誤りの表示をやめて DISPLAY を返す（空けば待機画面が借り直す）。"""
        with self._notice_lock:
            lease, self._notice = self._notice, None
        if lease is not None:
            lease.release()

    # --- 停止 ---

    def shutdown(self, budget: float = DEFAULT_STOP_BUDGET_S) -> None:
        """`start()` で始めたものを止めて片付ける。各段階の失敗はログに残して次に進む。"""
        if not self._started:
            return
        self._started = False
        if self.supervisor is not None:
            self._step("features", lambda: self._stop_features(budget))
        if self.safety is not None:
            # 後のスレッドの待ちが長引いても 10 秒に収まるよう、先に中立の姿勢にする（2.2）
            for step in self.safety.neutral():
                logger.warning("停止の片付けの段階 %s ができませんでした", step)
        self._notice_stop.set()
        if self._notice_thread is not None:
            self._notice_thread.join(timeout=1.0)
        self._step("config_notice", self._end_config_error)
        if self.idle is not None:
            self._step("idle", self.idle.stop)
        if self.monitor is not None:
            self._step("timesync", self.monitor.stop)
        if self.router is not None:
            self._step("buttons", self.router.close)
        if self.bus is not None:
            self._step("events", self.bus.close)
        if self.safety is not None:
            for step in self.safety.halt():
                logger.warning("停止の片付けの段階 %s ができませんでした", step)
        self._lock.release()

    def _stop_features(self, budget: float) -> None:
        supervisor = self.supervisor
        if supervisor is not None:
            self.abandoned = list(supervisor.stop_all(budget))

    @staticmethod
    def _step(name: str, action: Callable[[], object]) -> None:
        try:
            action()
        except Exception as error:
            logger.warning("停止の段階 %s に失敗しました: %s", name, type(error).__name__)
