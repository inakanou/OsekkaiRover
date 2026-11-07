"""ほかの機能が読む、日課の今の状況（9.4、9.5）。

中身は時刻と真偽値だけ。タスクの名前・言葉・呼び名は入れない。
値はプロセスの中に 1 つだけで、ロックで守って置き換える。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RoutineStatus:
    """日課の今の状況。"""

    next_task_at: datetime | None  # 今日のこの後の最も早い時刻のタスク（9.4）
    in_task: bool  # タスクか予告の最中（9.5）
    asleep: bool  # おやすみモード（9.5）
    updated_at: datetime


_lock = threading.Lock()
_status: RoutineStatus | None = None


def current_status() -> RoutineStatus | None:
    """今の状況。日課が動いていなければ None。"""
    with _lock:
        return _status


def _publish(status: RoutineStatus | None) -> None:
    """状況を置き換える（日課の機能だけが呼ぶ）。None で消す。"""
    global _status
    with _lock:
        _status = status
