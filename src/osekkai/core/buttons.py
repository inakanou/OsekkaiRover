"""ボタンの押下の配達（5.1〜5.4）。

ButtonPort の立ち下がりの通知を受け、チャタリングを除いてから、次の順で 1 つの受け手に届ける:
1. BUTTON のリースを持つ機能がいれば、そのリースの待ち行列に 1 つ入れる（5.1）
2. いなければ、待ち手のうち最も優先度の高い機能の受け手を呼ぶ。同じなら先に登録した方（5.2）
3. どちらもいなければ捨てる（5.3）

- チャタリングの除去は、同じボタンで前に受け付けた押下から 50 ms 以内の通知を捨てる（5.4）。
  時刻は ButtonPort が渡す単調な時刻を使う
- リースの待ち行列は、リースが終わったら捨てる（次の持ち主に持ち越さない）
- 待ち手の受け手は、ButtonPort の通知のスレッドで、ロックの外で呼ぶ。速やかに戻ること
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, Literal

from osekkai.core.ports import ButtonPort, Unavailable
from osekkai.core.types import ButtonPress, Resource

if TYPE_CHECKING:
    from osekkai.core.arbiter import Arbiter, Lease
    from osekkai.core.clock import Clock

logger = logging.getLogger(__name__)

DEBOUNCE_S: Final = 0.05
_SOURCES: Final[tuple[Literal["usr", "extra"], ...]] = ("usr", "extra")

PressCallback = Callable[[ButtonPress], None]


class _Waiter:
    def __init__(self, owner: str, priority: int, seq: int, callback: PressCallback) -> None:
        self.owner = owner
        self.priority = priority
        self.seq = seq
        self.callback = callback


class ButtonRouter:
    """押下を、正しい受け手に 1 回だけ届ける。"""

    def __init__(self, port: ButtonPort | Unavailable, clock: Clock) -> None:
        self._port = port
        self._clock = clock
        self._cond = threading.Condition()
        self._arbiter: Arbiter | None = None
        self._queues: dict[Lease, deque[ButtonPress]] = {}
        self._waiters: list[_Waiter] = []
        self._seq = itertools.count()
        self._last_at: dict[str, float] = {}
        self._started = False

    # --- 開始と終わり ---

    def start(self) -> None:
        if isinstance(self._port, Unavailable):
            logger.warning("ボタンが使えません: %s", self._port.device)
            return
        if self._started:
            return
        self._port.start(self._on_falling)
        self._started = True

    def close(self) -> None:
        if not self._started or isinstance(self._port, Unavailable):
            return
        self._started = False
        try:
            self._port.close()
        except Exception as error:
            logger.warning("ボタンを閉じられませんでした: %s", type(error).__name__)

    def bind(self, arbiter: Arbiter) -> None:
        """リースへの配達のために Arbiter とつなぐ。Arbiter の生成から呼ばれる。"""
        with self._cond:
            self._arbiter = arbiter
        arbiter.on_resource_freed(Resource.BUTTON, self._drop_finished)

    # --- 待ち手（FeatureContext.want_button_presses から使う） ---

    def add_waiter(self, owner: str, priority: int, callback: PressCallback) -> Callable[[], None]:
        """ボタンを待つ受け手を足す。戻り値を呼ぶと登録を解く（何度呼んでもよい）。"""
        with self._cond:
            waiter = _Waiter(owner, priority, next(self._seq), callback)
            self._waiters.append(waiter)

        def unregister() -> None:
            with self._cond:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

        return unregister

    # --- リースの待ち行列（ButtonHandle から使う） ---

    def clear(self, lease: Lease) -> None:
        with self._cond:
            self._queues.pop(lease, None)

    def has_press(self, lease: Lease) -> bool:
        with self._cond:
            return bool(self._queues.get(lease))

    def wait_press(self, lease: Lease, timeout: float | None) -> bool | None:
        """押下を 1 つ消費する。時間切れなら False、リースが終わったら None。"""
        with self._cond:
            deadline = None if timeout is None else self._clock.monotonic() + timeout
            timed_out = False
            while True:
                if not lease.active:
                    return None
                queue = self._queues.get(lease)
                if queue:
                    queue.popleft()
                    return True
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - self._clock.monotonic()
                if timed_out or remaining <= 0:
                    return False
                timed_out = not self._cond.wait(timeout=remaining)

    # --- 内側 ---

    def _on_falling(self, source: str, at: float) -> None:
        kind = next((s for s in _SOURCES if s == source), None)
        if kind is None:
            logger.warning("知らないボタンの通知を捨てました")
            return
        press = ButtonPress(at=at, source=kind)
        with self._cond:
            last = self._last_at.get(kind)
            if last is not None and at - last <= DEBOUNCE_S:
                return
            self._last_at[kind] = at
            if self._queue_for_holder(press):
                return
            waiter = self._best_waiter()
        if waiter is None:
            logger.debug("受け手のいない押下を捨てました")
            return
        try:
            waiter.callback(press)
        except Exception as error:
            logger.warning(
                "押下の受け手の処理に失敗しました: %s: %s", waiter.owner, type(error).__name__
            )

    def _queue_for_holder(self, press: ButtonPress) -> bool:
        """持ち主のリースの待ち行列に入れる（Condition の中で呼ぶ）。"""
        arbiter = self._arbiter
        lease = arbiter.lease_for(Resource.BUTTON) if arbiter is not None else None
        if lease is None or not lease.active:
            return False
        self._queues.setdefault(lease, deque()).append(press)
        self._cond.notify_all()
        return True

    def _best_waiter(self) -> _Waiter | None:
        if not self._waiters:
            return None
        return min(self._waiters, key=lambda w: (-w.priority, w.seq))

    def _drop_finished(self) -> None:
        """終わったリースの待ち行列を捨て、待っている口を起こす。"""
        with self._cond:
            for lease in [lease for lease in self._queues if not lease.active]:
                del self._queues[lease]
            self._cond.notify_all()
