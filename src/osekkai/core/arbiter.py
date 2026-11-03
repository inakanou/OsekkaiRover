"""資源の調停（4.1〜4.8、2.5、3.5）。

資源ごとに持ち主（リース）を最大 1 つにし、求めた資源の集合を「全部か無しか」で貸す。
状態は 1 つの `threading.Condition` で守る。モーターや首の操作と、空いたときの合図は、
必ず Condition の外で呼ぶ（行き詰まりを防ぐ）。

リースが終わったときの順番:
1. Condition の中でリースを終わった状態にする（以後、有効にならない）
2. Condition の外で、走行ならモーターを止め、首なら正面に戻す（2.5、4.8）
3. Condition の中で資源を空け、待っている機能を起こす
4. Condition の外で、空いた資源の合図を呼ぶ

2 と 3 の順にするのは、次の持ち主が動かし始めたモーターを、前の持ち主の後始末で
止めないため。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from enum import Enum
from types import TracebackType
from typing import TYPE_CHECKING

from osekkai.core.types import Resource, ResourceBusy

if TYPE_CHECKING:
    from osekkai.core.clock import Clock
    from osekkai.core.motion import MotionService

logger = logging.getLogger(__name__)


class LeaseState(Enum):
    ACTIVE = "active"
    PREEMPT_REQUESTED = "preempt_requested"
    RELEASED = "released"
    REVOKED = "revoked"


_FINISHED = frozenset({LeaseState.RELEASED, LeaseState.REVOKED})


class Lease:
    """借りた資源の集合。`Arbiter.acquire()` だけが作る。

    状態は Arbiter の Condition の中で変わる。`active` はロックを取らずに読める
    （動きの段階ごとの確認で使う）。
    """

    def __init__(
        self,
        arbiter: Arbiter,
        owner: str,
        priority: int,
        resources: frozenset[Resource],
        on_preempt: Callable[[], None] | None,
    ) -> None:
        self._arbiter = arbiter
        self.owner = owner
        self.priority = priority
        self.resources = resources
        self.on_preempt = on_preempt
        self.preempt_requested = threading.Event()  # 中断の通知（4.3）
        self._state = LeaseState.ACTIVE

    @property
    def state(self) -> LeaseState:
        return self._state

    @property
    def active(self) -> bool:
        return self._state not in _FINISHED

    def release(self) -> None:
        """返す。何度呼んでもよい。"""
        self._arbiter._finish([self], LeaseState.RELEASED, owner=None)

    def __enter__(self) -> Lease:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def __repr__(self) -> str:
        names = ",".join(sorted(r.value for r in self.resources))
        return f"Lease(owner={self.owner!r}, resources={names}, state={self._state.value})"


class Arbiter:
    """資源の貸し出しと返却。"""

    def __init__(self, clock: Clock, motion: MotionService) -> None:
        self._clock = clock
        self._motion = motion
        self._cond = threading.Condition()
        self._holders: dict[Resource, Lease] = {}
        self._leases: set[Lease] = set()  # 終わっていないリース
        self._freed_callbacks: dict[Resource, list[Callable[[], None]]] = {}

    def acquire(
        self,
        owner: str,
        priority: int,
        resources: Iterable[Resource],
        *,
        wait: float = 0.0,
        on_preempt: Callable[[], None] | None = None,
    ) -> Lease:
        """求めた資源をすべて貸す。貸せなければ `wait` 秒まで待ち、だめなら `ResourceBusy`。

        期限は `Clock.monotonic()` で決める。実際の待ちも残りの秒数で区切るので、
        偽の時計が進まなくても、待ちが時間切れになれば諦める。
        """
        wanted = frozenset(resources)
        if not wanted:
            raise ValueError("resources must not be empty")
        if wait < 0:
            raise ValueError("wait must not be negative")
        with self._cond:
            deadline = self._clock.monotonic() + wait
            timed_out = False
            while True:
                blockers = self._blockers(wanted)
                if not blockers:
                    return self._grant(owner, priority, wanted, on_preempt)
                # 優先度の低い持ち主への中断（4.3）は、ここで blockers に対して行う。
                # 今は持ち主の優先度によらず、空くのを待つ（4.7）
                remaining = deadline - self._clock.monotonic()
                if timed_out or remaining <= 0:
                    raise ResourceBusy(", ".join(sorted(r.value for r in wanted)))
                timed_out = not self._cond.wait(timeout=remaining)

    def revoke_all(self, owner: str) -> None:
        """その持ち主のすべてのリースを、直ちに取り上げる（3.5）。"""
        with self._cond:
            leases = [lease for lease in self._leases if lease.owner == owner]
        self._finish(leases, LeaseState.REVOKED, owner=owner)

    def on_resource_freed(self, resource: Resource, callback: Callable[[], None]) -> None:
        """資源が空いたときに呼ぶ受け手を足す。受け手は Condition の外で呼ばれる。"""
        with self._cond:
            self._freed_callbacks.setdefault(resource, []).append(callback)

    def holder(self, resource: Resource) -> str | None:
        with self._cond:
            lease = self._holders.get(resource)
            return lease.owner if lease is not None else None

    # --- 内側 ---

    def _blockers(self, wanted: frozenset[Resource]) -> set[Lease]:
        """求めた資源を持っているリース（Condition の中で呼ぶ）。"""
        return {self._holders[r] for r in wanted if r in self._holders}

    def _grant(
        self,
        owner: str,
        priority: int,
        wanted: frozenset[Resource],
        on_preempt: Callable[[], None] | None,
    ) -> Lease:
        lease = Lease(self, owner, priority, wanted, on_preempt)
        for resource in wanted:
            self._holders[resource] = lease
        self._leases.add(lease)
        return lease

    def _finish(self, leases: Iterable[Lease], state: LeaseState, owner: str | None) -> None:
        # 1. 終わった状態にする。すでに終わったリースは飛ばす（返却は何度でもよい）
        with self._cond:
            finished = [lease for lease in leases if lease.active]
            for lease in finished:
                lease._state = state
                self._leases.discard(lease)
        if not finished:
            return
        if owner is not None:
            logger.info("資源を取り上げました: %s", owner)
        freed = frozenset().union(*(lease.resources for lease in finished))

        # 2. 後始末（Condition の外）
        if Resource.DRIVE in freed:
            self._safely("モーターを止められませんでした", self._motion.stop)
        if Resource.NECK in freed:
            self._safely("首を正面に戻せませんでした", self._motion.home_neck)

        # 3. 資源を空けて、待っている機能を起こす
        with self._cond:
            for lease in finished:
                for resource in lease.resources:
                    if self._holders.get(resource) is lease:
                        del self._holders[resource]
            self._cond.notify_all()
            callbacks = [
                callback
                for resource in Resource
                if resource in freed
                for callback in self._freed_callbacks.get(resource, [])
            ]

        # 4. 空いた資源の合図（Condition の外）
        for callback in callbacks:
            self._safely("資源が空いたときの処理に失敗しました", callback)

    @staticmethod
    def _safely(message: str, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as error:
            # メッセージに利用者の入力が混ざりうるので、例外の型だけを残す
            logger.warning("%s: %s", message, type(error).__name__)
