"""だるまさんの機能（DarumaFeature）と 1 回の遊び（run_game）。

要件 1.1、1.6、3.2、3.5、4.4、4.5、4.7、4.9、5.1、5.3、6.1、7.2。

起動の始めに始める語句を登録し、止まるときに外す。言葉を受けたらすぐ戻り、
遊びは機能のスレッドで 1 つずつ行う。遊んでいる間に届いた言葉は捨てる（4.9）。
遊びの間は声・LCD・ボタン・首を借り続ける。走行（DRIVE）は申告しない（5.1、6.1）。
保存先は使わず、星も増やさない（3.2、3.5）。
ログには、決まった文言・終わり方の種類・回の数・例外の型の名前だけを出す。
"""

from __future__ import annotations

import logging
import queue
import random
import threading
from collections.abc import Callable
from typing import ClassVar, Final

import osekkai.features.routine.api as routine_api
from osekkai.core.config import FeatureConfig
from osekkai.core.feature import FeatureContext, FeatureSpec
from osekkai.core.types import Priority, Resource, ResourceBusy
from osekkai.features.daruma.camera import BorrowCamera, borrow_from_presence
from osekkai.features.daruma.checks import DARUMA_CHECKS
from osekkai.features.daruma.config import DarumaConfig
from osekkai.features.daruma.flow import GameFlow, GameResult
from osekkai.features.daruma.lease_io import OWNER, LeaseGameIO
from osekkai.features.daruma.rules import EndReason, Rules
from osekkai.features.daruma.texts import START_PHRASES
from osekkai.voice.api import Heard, register_words

__all__ = ["ACQUIRE_WAIT_S", "POLL_S", "PRIORITY", "RESOURCES", "DarumaFeature", "run_game"]

logger = logging.getLogger(__name__)

# 入口（55）より上、日課（80）より下（6.1）
PRIORITY: Final = Priority.INTERACTIVE + 6
# 遊びの始めから終わりまで借りる資源。走行（DRIVE）は入れない（5.1）
RESOURCES: Final = frozenset({Resource.VOICE, Resource.DISPLAY, Resource.BUTTON, Resource.NECK})
ACQUIRE_WAIT_S: Final = 2.0  # 資源を待つ上限（秒）
POLL_S: Final = 1.0  # 待ち行列を見る間隔（実際の秒）
WORDS_PRIORITY: Final = 0  # 語句の登録の優先度

IoFactory = Callable[..., LeaseGameIO]


def run_game(
    ctx: FeatureContext,
    config: DarumaConfig,
    stop: threading.Event,
    *,
    borrow: BorrowCamera = borrow_from_presence,
    rng: random.Random | None = None,
    status: Callable[[], routine_api.RoutineStatus | None] = routine_api.current_status,
    io_factory: IoFactory = LeaseGameIO,
) -> GameResult:
    """資源を借り、1 回の遊びを終わりまで進めて、借りたものを返す。

    どの終わり方でも、カメラを返し（4.7）、リースを返す（基盤が首を正面に戻す）。
    思いがけない例外は ERROR にする（5.3）。
    """
    if stop.is_set():
        return GameResult(EndReason.STOPPED, 0)
    if ctx.is_quiet:  # 4.5（資源も借りない）
        return GameResult(EndReason.QUIET, 0)
    preempted = threading.Event()
    try:
        lease = ctx.acquire(RESOURCES, wait=ACQUIRE_WAIT_S, on_preempt=preempted.set)
    except ResourceBusy:
        logger.info("資源を借りられないので遊びません")  # 1.6（話さない）
        return GameResult(EndReason.BUSY, 0)

    def interrupted() -> EndReason | None:
        """止める旗 → 中断の知らせ → 静かな状態、の順に見る（4.4、4.5）。"""
        if stop.is_set():
            return EndReason.STOPPED
        if preempted.is_set() or lease.preempt_requested.is_set() or not lease.active:
            return EndReason.PREEMPTED
        if ctx.is_quiet:
            return EndReason.QUIET
        return None

    logger.info("遊びを始めました")
    try:
        with lease:
            io = io_factory(
                lease, borrow=borrow, clock=ctx.clock, interrupted=interrupted, stop=stop
            )
            try:
                flow = GameFlow(
                    Rules.from_config(config),
                    config.call_name,
                    rng if rng is not None else random.Random(),
                    status,
                    ctx.clock.now,
                    interrupted,
                    logger,
                )
                result = flow.play(io)
            finally:
                io.close()  # カメラを返す（4.7）
    except Exception as exc:  # 5.3。その遊びだけを終える
        logger.warning("遊びで思いがけない誤りが起きました: %s", type(exc).__name__)
        result = GameResult(EndReason.ERROR, 0)
    logger.info("遊びを終えました: %s（%d 回）", result.reason.value, result.rounds)
    return result


class DarumaFeature:
    """だるまさんの機能。基盤は `DarumaFeature(ctx, config)` だけで作る。

    `borrow` はカメラを借りる口、`io_factory` は借りたリースから遊びの口を作る口、
    `poll_s` は待ち行列を見る間隔（テストで短くする）。
    """

    spec: ClassVar[FeatureSpec] = FeatureSpec(
        name=OWNER,
        priority=PRIORITY,
        resources=RESOURCES,
        config_model=DarumaConfig,
        checks=DARUMA_CHECKS,
    )

    def __init__(
        self,
        ctx: FeatureContext,
        config: FeatureConfig,
        *,
        borrow: BorrowCamera = borrow_from_presence,
        io_factory: IoFactory = LeaseGameIO,
        poll_s: float = POLL_S,
    ) -> None:
        if not isinstance(config, DarumaConfig):
            raise TypeError("だるまさんの設定ではありません")
        self._ctx = ctx
        self._config = config
        self._borrow = borrow
        self._io_factory = io_factory
        self._poll_s = poll_s
        self._heard: queue.Queue[Heard] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._playing = False  # 言葉を受けてから遊びを終えるまで立てる（4.9）

    # --- 基盤から呼ぶ口 ---

    def run(self, stop: threading.Event) -> None:
        """始める語句を登録し、止める旗が立つまで言葉で遊ぶ。終わりに登録を外す（1.1）。"""
        registration = register_words(
            OWNER, WORDS_PRIORITY, START_PHRASES, self._on_heard, alive=lambda: not stop.is_set()
        )
        try:
            while not stop.is_set():
                self._step(stop)
        finally:
            registration.remove()

    # --- 中の流れ ---

    def _on_heard(self, heard: Heard) -> None:
        """入口のスレッドから呼ばれる。遊んでいる間なら捨て、そうでなければ渡してすぐ戻る。"""
        with self._lock:
            if self._playing:
                logger.info("遊んでいる間なので、言葉を捨てました")  # 4.9
                return
            try:
                self._heard.put_nowait(heard)
            except queue.Full:
                logger.info("遊びを始める前なので、言葉を捨てました")
                return
            self._playing = True

    def _step(self, stop: threading.Event) -> None:
        """待ち行列を見て、言葉があれば 1 回遊ぶ。"""
        try:
            self._heard.get(timeout=self._poll_s)
        except queue.Empty:
            return
        try:
            run_game(
                self._ctx,
                self._config,
                stop,
                borrow=self._borrow,
                io_factory=self._io_factory,
            )
        except Exception as exc:  # 5.3。次の言葉を待つ
            logger.warning("遊びで思いがけない誤りが起きました: %s", type(exc).__name__)
        finally:
            with self._lock:
                self._playing = False
