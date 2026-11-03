"""差し替えられる時計と、本物の時計。

基盤の中で時刻が要るときは、`datetime.now()` や `time.monotonic()` を直に呼ばず、
`Clock` を受け取って使う。直に呼ぶのは `SystemClock` だけ。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Protocol

from osekkai.core.events import EventBus, TimeSynced
from osekkai.core.ports import TimeSyncPort

logger = logging.getLogger(__name__)


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


class TimeSyncMonitor:
    """同期するまで `interval` 秒ごとに確かめ、同期したら `TimeSynced` を 1 回だけ出す（10.3）。

    待つのは止める合図の `threading.Event` で、実際の秒数を待つ（偽の時計の時刻は進めない）。
    テストでは `check_once()` で 1 回ずつ進められる。
    """

    def __init__(self, clock: Clock, bus: EventBus, interval: float = 30.0) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self._clock = clock
        self._bus = bus
        self.interval = interval
        self._lock = threading.Lock()
        self._synced = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def synced(self) -> bool:
        with self._lock:
            return self._synced

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def check_once(self) -> bool:
        """1 回確かめる。同期していれば真。初めて同期を見たときだけ `TimeSynced` を出す。"""
        with self._lock:
            if self._synced:
                return True
            if not self._clock.is_synchronized():
                return False
            self._synced = True
        self._bus.publish(TimeSynced())
        return True

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="osekkai-timesync", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self.join(timeout)

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.check_once():
                    return
            except Exception as error:
                # 確かめるのに失敗しても、次の回にまた確かめる
                logger.warning("時刻の同期を確かめられませんでした: %s", type(error).__name__)
            self._stop.wait(self.interval)
