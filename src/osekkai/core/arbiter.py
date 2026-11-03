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

優先度による中断（4.3〜4.5）:
- 持ち主の全員が求めた機能より低い優先度なら、各リースに中断を 1 回だけ知らせる。
  走行を持つリースなら、直ちに非常停止する。知らせの受け手は短命のスレッドで呼ぶ
- 高い方は `wait` によらず、返されるか取り上げるまで待つ（持ち主の全員より上なので）
- 知らせてから `PREEMPT_GRACE_S` 秒（`Clock.monotonic()`）で返されなければ取り上げる。
  偽の時計が進まなくても止まらないよう、実際の待ちの合計が上限を超えても取り上げる
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from enum import Enum
from types import TracebackType
from typing import TYPE_CHECKING, Final

from osekkai.core.types import Resource, ResourceBusy

if TYPE_CHECKING:
    from osekkai.core.clock import Clock
    from osekkai.core.motion import MotionService

logger = logging.getLogger(__name__)

# 中断の後に待つ 1 回の長さ（秒）。偽の時計を進めたことに、この間隔で気づく
_PREEMPT_POLL_S: Final = 0.05


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
        self._preempt_deadline = 0.0  # 中断を知らせたときの取り上げの期限（Clock.monotonic()）

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
    """資源の貸し出しと返却、優先度による中断と取り上げ。"""

    PREEMPT_GRACE_S: Final = 2.0
    # 偽の時計が進まないときに備えた、中断の後の実際の待ちの上限（秒）
    _PREEMPT_REAL_LIMIT_S: float = PREEMPT_GRACE_S + 1.0

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
        """求めた資源をすべて貸す。

        持ち主の全員が `priority` より低ければ、中断を知らせて、返されるか取り上げるまで
        待つ（`wait` によらない）。そうでなければ `wait` 秒まで待ち、だめなら `ResourceBusy`。

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
        real_left = self._PREEMPT_REAL_LIMIT_S
        while True:
            to_notify: list[Lease] = []
            expired: list[Lease] = []
            with self._cond:
                while True:
                    blockers = self._blockers(wanted)
                    if not blockers:
                        return self._grant(owner, priority, wanted, on_preempt)
                    if all(b.priority < priority for b in blockers):
                        # 持ち主の全員が低い（4.3）。wait によらず、返されるか取り上げるまで待つ
                        to_notify, expired = self._preempt(blockers, real_left <= 0)
                        if to_notify or expired:
                            break
                        if not self._cond.wait(timeout=_PREEMPT_POLL_S):
                            real_left -= _PREEMPT_POLL_S
                        continue
                    # 同じか高い優先度の持ち主がいる（4.7）
                    remaining = deadline - self._clock.monotonic()
                    if timed_out or remaining <= 0:
                        raise ResourceBusy(", ".join(sorted(r.value for r in wanted)))
                    timed_out = not self._cond.wait(timeout=remaining)
            # Condition の外で、止める・知らせる・取り上げる
            if to_notify:
                # 新しく知らせたリースにも、猶予を丸ごと与える
                real_left = self._PREEMPT_REAL_LIMIT_S
            self._notify_preempt(to_notify)
            for lease in expired:
                self._finish([lease], LeaseState.REVOKED, owner=lease.owner)

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

    def _preempt(
        self, blockers: set[Lease], real_limit_hit: bool
    ) -> tuple[list[Lease], list[Lease]]:
        """中断を知らせるリースと、期限を過ぎたリースを選ぶ（Condition の中で呼ぶ）。"""
        now = self._clock.monotonic()
        to_notify: list[Lease] = []
        expired: list[Lease] = []
        for lease in blockers:
            if lease._state is LeaseState.ACTIVE:
                # 知らせるのは 1 回だけ
                lease._state = LeaseState.PREEMPT_REQUESTED
                lease._preempt_deadline = now + self.PREEMPT_GRACE_S
                lease.preempt_requested.set()
                to_notify.append(lease)
            elif lease._state is LeaseState.PREEMPT_REQUESTED and (
                real_limit_hit or now >= lease._preempt_deadline
            ):
                expired.append(lease)
        return to_notify, expired

    def _notify_preempt(self, leases: list[Lease]) -> None:
        """中断の知らせ（Condition の外で呼ぶ）。走行を持つなら先に止める（4.5）。"""
        if any(Resource.DRIVE in lease.resources for lease in leases):
            self._safely("モーターを止められませんでした", self._motion.emergency_stop)
        for lease in leases:
            logger.info("中断を知らせました: %s", lease.owner)
            callback = lease.on_preempt
            if callback is None:
                continue
            threading.Thread(
                target=self._safely,
                args=("中断の知らせの処理に失敗しました", callback),
                name="osekkai-preempt",
                daemon=True,
            ).start()

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
