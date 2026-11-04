"""テストから進められる時計。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9), "JST")
DEFAULT_START = datetime(2026, 1, 1, 9, 0, tzinfo=JST)


class FakeClock:
    """`Clock` の偽物。

    - `now()` と `monotonic()` は `advance()` と `sleep()` でだけ進む
    - `sleep()` は時刻を進めて直ちに戻る（実際には待たない）。待った秒数は `sleeps` に残る
    - 複数のスレッドから呼んでよい。別のスレッドの `sleep()` も同じ時刻を進める
    """

    def __init__(
        self,
        start: datetime = DEFAULT_START,
        *,
        monotonic: float = 1000.0,
        synchronized: bool = True,
    ) -> None:
        _require_aware(start)
        self._lock = threading.Lock()
        self._now = start
        self._monotonic = monotonic
        self._synchronized = synchronized
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def monotonic(self) -> float:
        with self._lock:
            return self._monotonic

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._advance(seconds)
            self.sleeps.append(seconds)

    def is_synchronized(self) -> bool:
        with self._lock:
            return self._synchronized

    # --- テストから操作する口 ---

    def advance(self, seconds: float) -> None:
        """現在時刻と単調な時刻を、どちらも `seconds` 秒進める。"""
        with self._lock:
            self._advance(seconds)

    def set_now(self, now: datetime) -> None:
        """現在時刻だけを変える（時刻の同期で時計が飛ぶ場合など）。単調な時刻は変えない。"""
        _require_aware(now)
        with self._lock:
            self._now = now

    def set_synchronized(self, synchronized: bool) -> None:
        with self._lock:
            self._synchronized = synchronized

    def _advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("seconds must be non-negative")
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
