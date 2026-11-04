"""機能の監督。機能ごとのスレッド、異常の隔離、停止の待ち。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from enum import Enum

from osekkai.core.arbiter import Arbiter
from osekkai.core.feature import FeatureContext, FeatureSpec
from osekkai.core.registry import FeaturePlan

logger = logging.getLogger(__name__)

THREAD_PREFIX = "osekkai-feature-"


class FeatureState(Enum):
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPED = "stopped"


class FeatureSupervisor:
    """機能ごとにデーモンでないスレッドを立て、`Feature(ctx, config)` を作って `run(stop)` を呼ぶ。

    - 生成（道具の生成を含む）や `run` で例外が出たら、機能名と例外の型だけをログに残し
      （traceback は DEBUG のときだけ）、`revoke_all` で資源を取り上げて FAILED にする。再起動しない
    - 例外なしで戻ったときも、返し忘れのリースを `revoke_all` で返して STOPPED にする
    - 終わった機能（失敗・正常の終わり・見捨て）の名前を `on_finished` に渡す。Runtime はそこで
      その機能のボタンの待ち手と知らせの受け手を外す（終わった機能が押下を飲み込まないため）
    - `stop_all(timeout)` は全機能の `stop` を立て、合計 `timeout` 秒（実際の秒数）まで待つ。
      応えなかった機能は資源を取り上げて見捨て、その名前を返す
    """

    def __init__(
        self,
        arbiter: Arbiter,
        context_factory: Callable[[FeatureSpec], FeatureContext],
        *,
        on_finished: Callable[[str], None] | None = None,
    ) -> None:
        self._arbiter = arbiter
        self._context_factory = context_factory
        self._on_finished = on_finished
        self._cond = threading.Condition()
        self._states: dict[str, FeatureState] = {}
        self._stops: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._pending: set[str] = set()  # スレッドがまだ終わっていない機能

    def start(self, plans: Sequence[FeaturePlan]) -> None:
        names = [plan.feature_cls.spec.name for plan in plans]
        with self._cond:
            known = [name for name in names if name in self._states]
        if known or len(set(names)) != len(names):
            raise ValueError("同じ名前の機能は 2 度開始できません")
        for plan in plans:
            self._start_one(plan)

    def states(self) -> Mapping[str, FeatureState]:
        with self._cond:
            return dict(self._states)

    def stop_all(self, timeout: float) -> Sequence[str]:
        """全機能に停止を知らせ、合計 `timeout` 秒まで待つ。応えなかった機能の名前を返す。"""
        with self._cond:
            for stop in self._stops.values():
                stop.set()
            # Condition の待ちは実際の秒数で測られるので、合計の上限になる
            self._cond.wait_for(lambda: not self._pending, max(0.0, timeout))
            left = [name for name in self._threads if name in self._pending]
            finished = [t for name, t in self._threads.items() if name not in self._pending]
        for thread in finished:
            if thread.ident is not None:  # 立てられなかったスレッドは join できない
                thread.join()  # 終わりの知らせの後は戻るだけ
        for name in left:
            logger.warning("機能 %s が停止に応えませんでした", name)
            self._clean_up(name)
        return left

    # --- 内側 ---

    def _start_one(self, plan: FeaturePlan) -> None:
        name = plan.feature_cls.spec.name
        stop = threading.Event()
        thread = threading.Thread(
            target=self._run, args=(plan, stop), name=f"{THREAD_PREFIX}{name}", daemon=False
        )
        with self._cond:
            self._states[name] = FeatureState.STARTING
            self._stops[name] = stop
            self._threads[name] = thread
            self._pending.add(name)
        try:
            thread.start()
        except Exception as exc:
            self._log_failure("機能 %s を開始できませんでした: %s", name, exc)
            self._finish(name, FeatureState.FAILED)

    def _run(self, plan: FeaturePlan, stop: threading.Event) -> None:
        spec = plan.feature_cls.spec
        name = spec.name
        outcome = FeatureState.FAILED
        try:
            try:
                ctx = self._context_factory(spec)
                feature = plan.feature_cls(ctx, plan.config)
            except Exception as exc:
                self._log_failure("機能 %s を開始できませんでした: %s", name, exc)
                return
            with self._cond:
                self._states[name] = FeatureState.RUNNING
            try:
                feature.run(stop)
            except Exception as exc:
                self._log_failure("機能 %s が止まりました: %s", name, exc)
                return
            outcome = FeatureState.STOPPED
        finally:
            self._finish(name, outcome)

    def _finish(self, name: str, state: FeatureState) -> None:
        # 失敗でも正常の終わりでも、残ったリースを取り上げる（DRIVE ならモーターが止まる）
        self._clean_up(name)
        with self._cond:
            self._states[name] = state
            self._pending.discard(name)
            self._cond.notify_all()

    def _clean_up(self, name: str) -> None:
        try:
            self._arbiter.revoke_all(name)
        except Exception as exc:
            logger.error("機能 %s の資源を取り上げられませんでした: %s", name, type(exc).__name__)
        # 待ち手と受け手も外す。見捨てた機能は、待つのをやめた時点と、スレッドの終わりの 2 回
        on_finished = self._on_finished
        if on_finished is None:
            return
        try:
            on_finished(name)
        except Exception as exc:
            logger.error("機能 %s の後片付けに失敗しました: %s", name, type(exc).__name__)

    @staticmethod
    def _log_failure(message: str, name: str, exc: BaseException) -> None:
        # 例外のメッセージには個人情報が混ざりうるので、型だけを出す
        logger.error(message, name, type(exc).__name__)
        logger.debug("機能 %s の例外の詳細", name, exc_info=exc)
