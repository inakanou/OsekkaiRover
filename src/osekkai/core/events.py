"""基盤のイベントと、その配達役。

配達は専用のスレッドで行い、出した側を待たせない。受け手の例外はログに残して配り続ける。
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

E = TypeVar("E")


@dataclass(frozen=True)
class TimeSynced:
    """時刻が確かになった（10.3）。"""


@dataclass(frozen=True)
class QuietChanged:
    """静かな状態が変わった（9.4、9.5）。"""

    quiet: bool


_STOP = object()


class EventBus:
    """イベントを、登録された受け手に、型ごとに登録順で配る。

    - `publish()` は積むだけで、配るのは `start()` で始まる専用のスレッド
    - 受け手は出したイベントの型と同じ型で登録したものだけ（親の型には配らない）
    - `close()` までに積んだイベントは配ってから止まる。閉じた後のイベントは捨てる
    - スレッドは daemon にする。受け手が固まっても、プロセスの終わりを妨げないため
    - 機能の受け手は持ち主（機能の名前）付きで登録し、機能が終わったら `unsubscribe_owner` で外す
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        # 型ごとの (持ち主, 受け手)。持ち主は機能の名前、基盤の受け手は None
        self._subscribers: dict[type, list[tuple[str | None, Callable[[Any], None]]]] = {}
        self._queue: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._pending = 0
        self._closed = False
        self._thread: threading.Thread | None = None

    def subscribe(
        self, event_type: type[E], callback: Callable[[E], None], *, owner: str | None = None
    ) -> None:
        with self._lock:
            self._subscribers.setdefault(event_type, []).append((owner, callback))

    def unsubscribe_owner(self, owner: str) -> None:
        """その持ち主の受け手をすべて外す。機能が終わったときに監督から呼ばれる。"""
        with self._lock:
            for event_type, entries in list(self._subscribers.items()):
                kept = [entry for entry in entries if entry[0] != owner]
                if kept:
                    self._subscribers[event_type] = kept
                else:
                    del self._subscribers[event_type]

    def publish(self, event: object) -> None:
        with self._lock:
            if self._closed:
                logger.debug("閉じた後のイベントを捨てました: %s", type(event).__name__)
                return
            self._pending += 1
            # ロックの中で積み、close() の止める印より前に並ぶようにする
            self._queue.put(event)

    def start(self) -> None:
        with self._lock:
            if self._closed or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="osekkai-events", daemon=True)
            self._thread.start()

    def close(self, timeout: float = 2.0) -> None:
        """積んだイベントを配り終えてからスレッドを止める。`timeout` 秒より長くは待たない。"""
        with self._lock:
            if self._closed:
                thread = None
            else:
                self._closed = True
                thread = self._thread
        if thread is None:
            return
        self._queue.put(_STOP)
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("イベントの配達のスレッドが時間内に止まりませんでした")

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def wait_idle(self, timeout: float) -> bool:
        """積んだイベントをすべて配り終えるまで待つ。配り終えたら真。"""
        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout)

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            if event is _STOP:
                return
            try:
                self._deliver(event)
            finally:
                with self._idle:
                    self._pending -= 1
                    if self._pending == 0:
                        self._idle.notify_all()

    def _deliver(self, event: object) -> None:
        with self._lock:
            callbacks = [callback for _, callback in self._subscribers.get(type(event), ())]
        for callback in callbacks:
            try:
                callback(event)
            except Exception as error:
                # 中身には利用者の情報が入りうるので、型の名前だけを残す
                logger.error(
                    "イベントの受け手で例外がありました: %s（%s）",
                    type(error).__name__,
                    type(event).__name__,
                )
