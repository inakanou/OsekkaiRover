"""差し替えられる時計と、本物の時計。

基盤の中で時刻が要るときは、`datetime.now()` や `time.monotonic()` を直に呼ばず、
`Clock` を受け取って使う。直に呼ぶのは `SystemClock` だけ。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Protocol

from osekkai.core.ports import TimeSyncPort


class Clock(Protocol):
    def now(self) -> datetime: ...  # 地域の時刻（タイムゾーン付き）
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...
    def is_synchronized(self) -> bool: ...  # 10.1


class SystemClock:
    """システムの時刻を使う時計。同期の状態は `TimeSyncPort` に問い合わせる。"""

    def __init__(self, timesync: TimeSyncPort) -> None:
        self._timesync = timesync
        self._synchronized = False

    def now(self) -> datetime:
        return datetime.now().astimezone()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def is_synchronized(self) -> bool:
        # 一度確かになったら保つ（後で同期が外れても、時刻は大きくずれない）
        if not self._synchronized and self._timesync.is_synchronized():
            self._synchronized = True
        return self._synchronized
