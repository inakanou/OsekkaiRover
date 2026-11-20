"""ほかの機能が読む、日課の今の状況（9.4、9.5）。

朝と夜のお話のために、今日の起床時刻・寝るタスク・朝の最初のタスクの時刻も出す。

中身は時刻と真偽値だけ。タスクの名前・言葉・呼び名は入れない。
値はプロセスの中に 1 つだけで、ロックで守って置き換える。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TaskMoment:
    """今日の、ある時刻のタスクの時刻と予告の様子。名前と言葉は入れない。"""

    start: datetime  # タスクの時刻（今日）
    notice_at: datetime  # 予告の時刻（タスクの時刻から予告の分を引いた時刻）
    has_notice: bool  # 予告の言葉があるか
    notice_said: bool  # 今日、予告を言い終えたか
    ended_at: datetime | None  # 今日の結果が付いた時刻（なければ None）


@dataclass(frozen=True)
class RoutineStatus:
    """日課の今の状況。

    足した 3 つの項目は、時刻が確かなときだけ詰める。確かでなければ None。
    """

    next_task_at: datetime | None  # 今日のこの後の最も早い時刻のタスク（9.4）
    in_task: bool  # タスクか予告の最中（9.5）
    asleep: bool  # おやすみモード（9.5）
    updated_at: datetime
    wake_at: datetime | None = None  # 今日の起床時刻
    bedtime: TaskMoment | None = None  # 今日の寝るタスク（その種類の日になければ None）
    first_task: TaskMoment | None = None  # 今日の、寝るタスクを除く最も早い時刻のタスク


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
