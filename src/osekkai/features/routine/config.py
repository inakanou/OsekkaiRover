"""日課の設定（`routine:` の節）の形と検証（1.1〜1.4、1.7、1.8、8.6）。

誤りの文言には、場所（キーのパス）と理由だけを入れる。
設定の値（タスクの id・時刻・言葉・呼び名）は入れない（1.9）。
"""

from __future__ import annotations

from datetime import date, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from osekkai.core.config import FeatureConfig

DayFilter = Literal["weekday", "holiday", "everyday"]
WakeKind = Literal["weekday", "holiday"]

_MINUTES_PER_DAY = 24 * 60
# YAML の 60 進の数の H:MM は、先頭が 1〜9 なので 60（1:00）より小さくならない
_MIN_BASE60 = 60
_KIND_NAMES: dict[WakeKind, str] = {"weekday": "平日", "holiday": "休日"}

MSG_TIME_FORMAT = '時刻は "HH:MM" のように書いてください'
MSG_TIME_ZONE = "時刻にタイムゾーンは書けません"
MSG_TIME_SECONDS = "時刻に秒は書けません"


def _time_before(value: object) -> object:
    """YAML が 60 進の数として読んだ時刻（例: 21:00 → 1260）を、時と分に戻す。

    60 より小さい数（例: 7）は、時刻の書き方の誤りにする。
    """
    if isinstance(value, bool):
        raise ValueError(MSG_TIME_FORMAT)
    if isinstance(value, int):
        if _MIN_BASE60 <= value < _MINUTES_PER_DAY:
            return time(value // 60, value % 60)
        raise ValueError(MSG_TIME_FORMAT)
    if isinstance(value, float):
        raise ValueError(MSG_TIME_FORMAT)
    return value


def _time_after(value: time | None) -> time | None:
    if value is not None and value.tzinfo is not None:
        raise ValueError(MSG_TIME_ZONE)
    if value is not None and (value.second or value.microsecond):
        raise ValueError(MSG_TIME_SECONDS)
    return value


class TaskConfig(BaseModel):
    """タスク 1 つの設定。キーの名前はサンプルと同じ。"""

    model_config = ConfigDict(extra="forbid")  # タスクの中の知らない項目は誤り（書き間違いの検出）

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=32)
    at: time | None = None  # "HH:MM"。at と after のどちらか 1 つ
    after: str | None = None  # 前のタスクの id
    days: DayFilter = "everyday"
    call: str = Field(min_length=1)
    praise: str = Field(min_length=1)
    lcd: str = Field(
        min_length=1, max_length=16
    )  # LCD の 1 行目。全角のかなも可（基盤が半角に変える）
    notice: str | None = None  # at のタスクだけ
    notice_min: int = Field(default=10, ge=1, le=120)
    ready: str | None = None
    remind_min: int = Field(default=5, ge=1, le=60)
    max_reminders: int = Field(default=2, ge=0, le=10)
    firm: bool = False
    sleep: bool = False

    _check_at_before = field_validator("at", mode="before")(_time_before)
    _check_at_after = field_validator("at")(_time_after)

    def applies_to(self, kind: WakeKind) -> bool:
        """その種類の日のタスクかどうか（`everyday` はどちらにも入る）。"""
        return self.days in ("everyday", kind)


class WakeTimes(BaseModel):
    """起床時刻。書かなければ、その種類の日の最初の時刻のタスクの時刻を使う（6.7）。"""

    weekday: time | None = None
    holiday: time | None = None

    _check_before = field_validator("weekday", "holiday", mode="before")(_time_before)
    _check_after = field_validator("weekday", "holiday")(_time_after)


class RoutineConfig(FeatureConfig):
    """日課の設定（`routine:` の節）。"""

    call_name: str = ""  # 空なら「きみ」（1.6）
    extra_holidays: list[date] = Field(default_factory=list)
    wake: WakeTimes = Field(default_factory=WakeTimes)
    keep_days: int = Field(default=90, ge=7, le=3650)  # 8.6
    tasks: list[TaskConfig] = Field(default_factory=list)

    def wake_time(self, kind: WakeKind) -> time | None:
        """その種類の日の起床時刻。設定がなければ、その種類の日の最初の時刻のタスクの時刻。

        寝るタスクは数えない。正しい設定では寝るタスクは起床時刻より後なので、
        数えても数えなくても同じ時刻になる。数えないほうが、誤りの理由が分かりやすい。
        """
        configured = self.wake.weekday if kind == "weekday" else self.wake.holiday
        if configured is not None:
            return configured
        times = [
            t.at for t in self.tasks if t.at is not None and not t.sleep and t.applies_to(kind)
        ]
        return min(times, default=None)

    @model_validator(mode="after")
    def _check_tasks(self) -> RoutineConfig:
        problems = [
            *self._problems_of_tasks(),
            *self._problems_of_after(),
            *self._problems_of_sleep(),
        ]
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _problems_of_tasks(self) -> list[str]:
        problems: list[str] = []
        seen: set[str] = set()
        for i, task in enumerate(self.tasks):
            if task.id in seen:
                problems.append(f"tasks.{i}.id: 同じ id のタスクがほかにあります")
            seen.add(task.id)
            if (task.at is None) == (task.after is None):
                problems.append(f"tasks.{i}: at と after のどちらか 1 つを書いてください")
            if task.after is not None and task.notice is not None:
                problems.append(f"tasks.{i}.notice: 予告は at のタスクにだけ書けます")
        return problems

    def _problems_of_after(self) -> list[str]:
        problems: list[str] = []
        by_id = {task.id: task for task in self.tasks}
        for i, task in enumerate(self.tasks):
            if task.after is None:
                continue
            if task.after not in by_id:
                problems.append(f"tasks.{i}.after: その id のタスクがありません")
            elif _in_loop(task, by_id):
                problems.append(f"tasks.{i}.after: after のつながりが輪になっています")
        return problems

    def _problems_of_sleep(self) -> list[str]:
        problems: list[str] = []
        sleeps = [(i, task) for i, task in enumerate(self.tasks) if task.sleep]
        if not sleeps:
            return problems
        counted: dict[WakeKind, int] = {"weekday": 0, "holiday": 0}
        for i, task in sleeps:
            if task.at is None and task.after is not None:
                problems.append(f"tasks.{i}.sleep: 寝るタスクは at で始めてください")
            twice = False
            for kind in counted:
                if task.applies_to(kind):
                    counted[kind] += 1
                    twice = twice or counted[kind] > 1
            if twice:
                problems.append(f"tasks.{i}.sleep: 同じ種類の日に寝るタスクが 2 つ以上あります")
        for kind, name in _KIND_NAMES.items():
            if self.wake_time(kind) is None:
                problems.append(
                    f"wake.{kind}: 寝るタスクがあるので、{name}の起床時刻が要ります"
                    f"（wake.{kind} か、{name}の時刻のタスクを書いてください）"
                )
        for i, task in sleeps:
            if task.at is None:
                continue
            for kind, name in _KIND_NAMES.items():
                wake = self.wake_time(kind)
                if task.applies_to(kind) and wake is not None and task.at <= wake:
                    problems.append(
                        f"tasks.{i}.at: 寝るタスクの時刻が{name}の起床時刻より後になっていません"
                    )
        return problems


def _in_loop(task: TaskConfig, by_id: dict[str, TaskConfig]) -> bool:
    """after をたどって、自分に戻ってくるかどうか。"""
    visited: set[str] = set()
    current = task.after
    while current is not None and current in by_id and current not in visited:
        if current == task.id:
            return True
        visited.add(current)
        current = by_id[current].after
    return False
