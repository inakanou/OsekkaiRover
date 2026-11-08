"""3 段のリース（操作・見るだけ・なし）を持つ（要件 8.4、12.2〜12.5）。

- 操作は DRIVE・NECK・VOICE・DISPLAY、見るだけは NECK、なしはリースを持たない
- 段が変わるときは、今のリースを返してから、新しい資源を待たずに借りる。
  返すと、基盤がモーターを止め、首を正面に戻す（12.4）。ここでは止めない
- 借りられなければなしにし、lost を立てる。操作は呼ばれるたびに試す（12.3）。
  見るだけは、前の失敗（借りられない・中断・取り上げ）から 5 秒たつまで試さない
- 首を持つ間は、優先度の低い見つけてあいさつが首を動かせない（12.5）

ロックは 2 つ。
- `_want_lock`: want と release を 1 つずつにする。借りる・返すの間も持つ
- `_lock`: 状態を入れ替える間だけ持つ。借りる・返す・on_lost を呼ぶ間は持たない
中断の受け手（基盤の短いスレッド）は `_lock` だけを短く取る。`_want_lock` は取らないので、
want の途中に中断が来ても詰まらない。on_lost の中から want と release を呼ばない。
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from enum import Enum
from typing import Final

from osekkai.core.arbiter import Lease
from osekkai.core.feature import FeatureContext
from osekkai.core.types import Resource, ResourceBusy


class Level(Enum):
    NONE = 0
    VIEW = 1  # {NECK}
    OPERATE = 2  # {DRIVE, NECK, VOICE, DISPLAY}


RESOURCES: Final[dict[Level, frozenset[Resource]]] = {
    Level.VIEW: frozenset({Resource.NECK}),
    Level.OPERATE: frozenset({Resource.DRIVE, Resource.NECK, Resource.VOICE, Resource.DISPLAY}),
}


class LeaseKeeper:
    """ラジコンのリースを 3 段で持つ。"""

    RETRY_SECONDS: Final = 5.0

    def __init__(self, ctx: FeatureContext, on_lost: Callable[[], None]) -> None:
        self._ctx = ctx
        self._on_lost = on_lost
        self._want_lock = threading.Lock()
        self._lock = threading.Lock()
        self._lease: Lease | None = None
        self._level = Level.NONE
        self._lost = False
        self._failed_at: float | None = None  # 前の失敗の単調な時刻
        self._gen = 0  # 借りるたびに増やす。遅れて届いた前のリースの知らせを見分ける
        self._preempted_gen = -1

    @property
    def lease(self) -> Lease | None:
        """今持っている使えるリース。持たない・取り上げられたなら None。"""
        with self._lock:
            lease = self._lease
        return lease if lease is not None and lease.active else None

    @property
    def lost(self) -> bool:
        """中断の知らせ（または借りられない）の後で、まだ借り直していない。"""
        with self._lock:
            return self._lost

    def want(self, level: Level) -> Level:
        """その段のリースを持つようにする。実際に持っている段を返す。"""
        with self._want_lock:
            now = self._ctx.clock.monotonic()
            with self._lock:
                old = self._lease
                if old is not None and not old.active:
                    # 取り上げられた（猶予の後）。中断と同じに扱う
                    self._lease = None
                    self._level = Level.NONE
                    self._lost = True
                    self._failed_at = now
                    old = None
                if old is not None and self._level is level:
                    return level
                self._lease = None
                self._level = Level.NONE
                if level is Level.NONE:
                    self._lost = False
                skip_view = (
                    level is Level.VIEW
                    and self._failed_at is not None
                    and now - self._failed_at < self.RETRY_SECONDS
                )
                self._gen += 1
                gen = self._gen
            if old is not None:
                old.release()
            if level is Level.NONE or skip_view:
                return Level.NONE
            return self._acquire(level, gen)

    def release(self) -> None:
        """リースを返す（段をなしにする）。何度呼んでもよい。"""
        self.want(Level.NONE)

    # --- 内側 ---

    def _acquire(self, level: Level, gen: int) -> Level:
        try:
            lease = self._ctx.acquire(
                RESOURCES[level], wait=0, on_preempt=functools.partial(self._preempted, gen)
            )
        except ResourceBusy:
            with self._lock:
                self._lost = True
                self._failed_at = self._ctx.clock.monotonic()
            return Level.NONE
        with self._lock:
            preempted = self._preempted_gen == gen
            if not preempted:
                self._lease = lease
                self._level = level
                self._lost = False
                self._failed_at = None
        if preempted:
            # 借りた直後に中断が来た。受け手は on_lost を呼び、lost を立ててある
            lease.release()
            return Level.NONE
        return level

    def _preempted(self, gen: int) -> None:
        """中断の知らせ（基盤の短いスレッド）。lost を立て、on_lost を呼び、返す。

        話さない・待たない（12.2）。`_lock` は状態を入れ替える間だけ持つ。
        """
        with self._lock:
            if gen != self._gen:
                return  # すでに返した前のリースの知らせ
            self._preempted_gen = gen
            lease = self._lease
            self._lease = None
            self._level = Level.NONE
            self._lost = True
            self._failed_at = self._ctx.clock.monotonic()
        try:
            self._on_lost()
        except Exception as e:
            self._ctx.log.warning("中断の後片付けに失敗しました: %s", type(e).__name__)
        if lease is not None:
            lease.release()
