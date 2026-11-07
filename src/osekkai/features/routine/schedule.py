"""タスクと予告を始める判定（3.1〜3.6、3.10、5.1〜5.4、6.1、6.4〜6.11、7.1〜7.4、9.4）。

今の時刻（タイムゾーン付き）と記録の読み取りだけから、何をするかを決める。
資源・時計・保存には触れない。日の区切りは、今の時刻のタイムゾーンの日付で決める。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from enum import Enum

from osekkai.features.routine.calendar import DayKind, DayKinds
from osekkai.features.routine.config import RoutineConfig, TaskConfig, WakeKind
from osekkai.features.routine.records import DayRecordView, TaskResult

START_WINDOW = timedelta(minutes=60)  # 時刻のタスクを始められる長さ（3.2）。寝るタスクも同じ（6.8）
FOLLOW_WINDOW = timedelta(minutes=60)  # 続きのタスクを始められる長さ（3.3）
LATER_WINDOW = timedelta(minutes=30)  # あとでの受付の長さ

MSG_NAIVE_NOW = "今の時刻にタイムゾーンがありません"


class Phase(Enum):
    """タスクの段階。割り込みの判定に使う。"""

    READY = "ready"  # 準備の後のボタン待ち
    CALLING = "calling"  # 声かけ・催促の後のボタン待ち
    LATER = "later"
    PRAISING = "praising"


@dataclass(frozen=True)
class DueTask:
    """始めるタスク。"""

    task: TaskConfig
    day: date  # 記録を付ける日


@dataclass(frozen=True)
class DueNotice:
    """言う予告。"""

    task: TaskConfig
    day: date  # 予告するタスクの日（予告を言う日と違うことがある）


class _State(Enum):
    """今日のタスクの今の状態（判定の中だけで使う）。"""

    WAITING = "waiting"  # まだ始める時刻でない、または前のタスクが終わっていない
    DUE = "due"  # 今始められる
    DROPPED = "dropped"  # 今日は始めないと決まった（3.2、3.4、6.5）
    ENDED_OK = "ended_ok"  # できた・スキップで終わった
    ENDED_CUT = "ended_cut"  # 中断で終わった


def _wake_kind(kind: DayKind) -> WakeKind:
    return "weekday" if kind is DayKind.WEEKDAY else "holiday"


def _zone(now: datetime) -> tzinfo:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(MSG_NAIVE_NOW)
    return now.tzinfo


def _moment(day: date, at: time, tz: tzinfo) -> datetime:
    return datetime.combine(day, at, tzinfo=tz)


class Scheduler:
    """何をいつするかの判定。

    `now` はタイムゾーン付きで渡す（ないと ValueError）。返す時刻は `now` と同じタイムゾーン。
    `wake_time` は今の時刻を受け取らないので、タイムゾーンを引数で受け取る。
    """

    def __init__(self, config: RoutineConfig, kinds: DayKinds) -> None:
        self._config = config
        self._kinds = kinds
        self._by_id = {task.id: task for task in config.tasks}

    def due_task(self, now: datetime, records: DayRecordView) -> DueTask | None:
        """今始めるタスク。設定の順で最初に始められるもの（3.1〜3.4、3.10、6.5）。

        おやすみの時間なら None（6.4）。
        """
        if self.in_sleep_period(now, records):
            return None
        due = self._due_tasks(now, records)
        # 寝るタスクは、待つ間はほかのタスクの後に回す（6.1）
        due.sort(key=lambda d: d.task.sleep and self._sleep_waiting(d, now))
        return due[0] if due else None

    def cut_in(
        self, now: datetime, records: DayRecordView, running: DueTask, phase: Phase
    ) -> DueTask | None:
        """今のタスクに割り込むタスク（5.1〜5.4、6.1）。設定の順で最初のもの。

        候補は、今のタスクのほかの、今始められる時刻のタスク。続きのタスクは割り込まない。
        ほめる間は割り込まない。寝るタスクは、時刻から待つ長さ（`sleep_wait_min`）の間は
        割り込まず（6.1）、過ぎたらどの段階にも割り込む（6.11）。準備の待ちには時刻のタスク、
        声かけ・催促・あとでには割り込めるタスクだけが割り込む。割り込めないタスクは、
        今のタスクが終わった後に、窓の中なら `due_task` で始まる（5.4）。
        おやすみの時間なら None。ふつうは寝るタスクの後にタスクは動かないが、
        `due_task` と同じく、おやすみの間には何も始めない（6.4）。
        """
        if phase is Phase.PRAISING or self.in_sleep_period(now, records):
            return None
        for due in self._due_tasks(now, records):
            task = due.task
            if task.id == running.task.id or task.at is None:
                continue
            if task.sleep:
                if not self._sleep_waiting(due, now):
                    return due
                continue
            if phase is Phase.READY or task.firm:
                return due
        return None

    def in_sleep_period(self, now: datetime, records: DayRecordView) -> bool:
        """おやすみの時間かどうか（6.4、6.8、6.10、7.1、7.2）。

        寝るタスクが設定に 1 つもなければ、いつも偽。そうでなければ、今日の起床時刻より前か、
        今日の寝るタスクの時刻の後で、結果があるか時刻から 60 分を過ぎていれば真。
        寝るタスクの窓の中で結果がなければ、まだおやすみでない（寝るタスクを始める。7.4）。
        """
        tz = _zone(now)
        if not any(task.sleep for task in self._config.tasks):
            return False
        today = now.date()
        wake = self.wake_time(today, tz)
        if wake is not None and now < wake:
            return True
        sleep = self.sleep_task(today)
        if sleep is None or sleep.at is None:
            return False
        start = _moment(today, sleep.at, tz)
        if now < start:
            return False
        return records.outcome(today, sleep.id) is not None or now >= start + START_WINDOW

    def _sleep_waiting(self, due: DueTask, now: datetime) -> bool:
        """寝るタスクが、ほかのタスクの流れを待つ間か（6.1、6.11）。"""
        at = due.task.at
        if at is None:
            return False
        start = _moment(due.day, at, _zone(now))
        return now < start + timedelta(minutes=self._config.sleep_wait_min)

    def due_notice(self, now: datetime, records: DayRecordView) -> DueNotice | None:
        """今言う予告。今日と明日の、予告を持つ時刻のタスクから選ぶ（3.5、3.6）。

        予告の時刻からタスクの時刻までの間で、タスクの日に結果がなく、
        予告をまだ言っていないもの。起床時刻より前のタスクは始めないので、予告もしない。
        おやすみの時間なら None（6.4）。
        """
        if self.in_sleep_period(now, records):
            return None
        tz = _zone(now)
        today = now.date()
        for day in (today, today + timedelta(days=1)):
            for task in self._config.tasks:
                if task.at is None or task.notice is None:
                    continue
                if not self._kinds.applies(task, day) or self._before_wake(task, day, tz):
                    continue
                start = _moment(day, task.at, tz)
                if not start - timedelta(minutes=task.notice_min) <= now < start:
                    continue
                if records.outcome(day, task.id) is None and not records.notice_said(day, task.id):
                    return DueNotice(task, day)
        return None

    def sleep_task(self, day: date) -> TaskConfig | None:
        """その日の寝るタスク。設定の検証で、1 つの種類の日に多くても 1 つ。"""
        for task in self._config.tasks:
            if task.sleep and self._kinds.applies(task, day):
                return task
        return None

    def wake_time(self, day: date, tz: tzinfo) -> datetime | None:
        """その日の起床時刻（6.7）。設定の値、なければその日の最初の時刻のタスクの時刻。

        どちらもなければ None。寝るタスクがあれば、設定の検証で必ず決まる。
        """
        at = self._config.wake_time(_wake_kind(self._kinds.kind_of(day)))
        return _moment(day, at, tz) if at is not None else None

    def next_task_at(self, now: datetime, records: DayRecordView) -> datetime | None:
        """今日の、まだ結果がなく、この後に時刻が来る時刻のタスクのうち最も早い時刻（9.4）。

        おやすみの時間なら None。
        """
        if self.in_sleep_period(now, records):
            return None
        tz = _zone(now)
        today = now.date()
        times = [
            _moment(today, task.at, tz)
            for task in self._config.tasks
            if task.at is not None
            and self._kinds.applies(task, today)
            and not self._before_wake(task, today, tz)
            and records.outcome(today, task.id) is None
        ]
        return min((moment for moment in times if moment > now), default=None)

    def _due_tasks(self, now: datetime, records: DayRecordView) -> list[DueTask]:
        """今日の、今始められるタスクの全部（設定の順）。"""
        tz = _zone(now)
        today = now.date()
        states: dict[str, _State] = {}
        return [
            DueTask(task, today)
            for task in self._config.tasks
            if self._state(task, today, now, tz, records, states) is _State.DUE
        ]

    def _state(
        self,
        task: TaskConfig,
        day: date,
        now: datetime,
        tz: tzinfo,
        records: DayRecordView,
        states: dict[str, _State],
    ) -> _State:
        """そのタスクの今の状態。続きのタスクは前のタスクをたどって決める。"""
        known = states.get(task.id)
        if known is not None:
            return known
        state = self._decide(task, day, now, tz, records, states)
        states[task.id] = state
        return state

    def _decide(
        self,
        task: TaskConfig,
        day: date,
        now: datetime,
        tz: tzinfo,
        records: DayRecordView,
        states: dict[str, _State],
    ) -> _State:
        result = records.outcome(day, task.id)
        if result is not None:
            return _State.ENDED_CUT if result is TaskResult.INTERRUPTED else _State.ENDED_OK
        # 今日に当てはまらないタスクは始めない。その続きも、前が終わらないので始めない
        if not self._kinds.applies(task, day):
            return _State.DROPPED
        if task.at is not None:
            if self._before_wake(task, day, tz):
                return _State.DROPPED  # おやすみの間に時刻が来たタスク（6.5）
            start = _moment(day, task.at, tz)
            return self._in_window(now, start, START_WINDOW)
        before = self._by_id.get(task.after) if task.after is not None else None
        if before is None:
            return _State.DROPPED  # 設定の検証で起きないが、念のため始めない
        before_state = self._state(before, day, now, tz, records, states)
        if before_state is not _State.ENDED_OK:
            # 前が中断か始めないと決まったなら、後ろも全部始めない（3.4）
            if before_state in (_State.ENDED_CUT, _State.DROPPED):
                return _State.DROPPED
            return _State.WAITING
        ended = records.ended_at(day, before.id)
        if ended is None:
            return _State.DROPPED  # 窓を決められないので始めない
        return self._in_window(now, ended, FOLLOW_WINDOW)

    @staticmethod
    def _in_window(now: datetime, start: datetime, window: timedelta) -> _State:
        if now < start:
            return _State.WAITING
        if now < start + window:
            return _State.DUE
        return _State.DROPPED

    def _before_wake(self, task: TaskConfig, day: date, tz: tzinfo) -> bool:
        """時刻のタスクの時刻が、その日の起床時刻より前かどうか（6.5）。起床時刻がなければ偽。"""
        if task.at is None:
            return False
        wake = self.wake_time(day, tz)
        return wake is not None and _moment(day, task.at, tz) < wake
