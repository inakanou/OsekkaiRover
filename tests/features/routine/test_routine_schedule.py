"""タスクと予告を始める判定（3.1〜3.6、3.10、6.5、6.7、9.4）のテスト。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from routine_fixtures import TOKYO, build_config, task_dict

from osekkai.features.routine.calendar import DayKinds
from osekkai.features.routine.records import TaskResult
from osekkai.features.routine.schedule import (
    FOLLOW_WINDOW,
    LATER_WINDOW,
    START_WINDOW,
    DueNotice,
    DueTask,
    Phase,
    Scheduler,
)

MONDAY = date(2026, 4, 6)
TUESDAY = date(2026, 4, 7)
SATURDAY = date(2026, 4, 11)


class FakeHolidays:
    """祝日のない、テスト用の祝日の判定。"""

    @property
    def available(self) -> bool:
        return True

    def is_holiday(self, day: date) -> bool:
        return False


class FakeRecords:
    """テスト用の記録の読み取り（DayRecordView を形で満たす）。"""

    def __init__(self) -> None:
        self.results: dict[tuple[date, str], tuple[TaskResult, datetime | None]] = {}
        self.notices: set[tuple[date, str]] = set()

    def put(
        self, day: date, task_id: str, result: TaskResult, ended_at: datetime | None = None
    ) -> None:
        self.results[(day, task_id)] = (result, ended_at)

    def outcome(self, day: date, task_id: str) -> TaskResult | None:
        entry = self.results.get((day, task_id))
        return entry[0] if entry is not None else None

    def ended_at(self, day: date, task_id: str) -> datetime | None:
        entry = self.results.get((day, task_id))
        return entry[1] if entry is not None else None

    def notice_said(self, day: date, task_id: str) -> bool:
        return (day, task_id) in self.notices


def at(day: date, hh: int, mm: int) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=TOKYO)


def scheduler(tasks: list[dict[str, Any]], **fields: Any) -> Scheduler:
    config = build_config(tasks, **fields)
    return Scheduler(config, DayKinds(config.extra_holidays, FakeHolidays()))


def running(tasks: list[dict[str, Any]], task_id: str, day: date, **fields: Any) -> DueTask:
    """動いているタスクとして渡す DueTask。"""
    config = build_config(tasks, **fields)
    return DueTask(next(t for t in config.tasks if t.id == task_id), day)


def due_id(s: Scheduler, now: datetime, records: FakeRecords) -> str | None:
    due = s.due_task(now, records)
    return due.task.id if due is not None else None


def test_定数の窓の長さ() -> None:
    assert timedelta(minutes=60) == START_WINDOW
    assert timedelta(minutes=60) == FOLLOW_WINDOW
    assert timedelta(minutes=30) == LATER_WINDOW


def test_段階の値() -> None:
    assert [p.value for p in Phase] == ["ready", "calling", "later", "praising"]


# 時刻のタスク（3.1、3.2）


def test_時刻のタスクは時刻になったら始まる() -> None:
    s = scheduler([task_dict("wash", at="07:00")])
    records = FakeRecords()
    assert s.due_task(at(MONDAY, 6, 59), records) is None
    due = s.due_task(at(MONDAY, 7, 0), records)
    assert isinstance(due, DueTask)
    assert due.task.id == "wash"
    assert due.day == MONDAY


def test_時刻のタスクは60分の間だけ始まる() -> None:
    s = scheduler([task_dict("wash", at="07:00")])
    records = FakeRecords()
    assert due_id(s, at(MONDAY, 7, 59) + timedelta(seconds=59), records) == "wash"
    assert s.due_task(at(MONDAY, 8, 0), records) is None


def test_今日の結果があれば始めない() -> None:
    s = scheduler([task_dict("wash", at="07:00")])
    records = FakeRecords()
    records.put(MONDAY, "wash", TaskResult.SKIPPED, at(MONDAY, 7, 5))
    assert s.due_task(at(MONDAY, 7, 10), records) is None


def test_前の日の結果は今日のタスクに数えない() -> None:
    s = scheduler([task_dict("wash", at="07:00")])
    records = FakeRecords()
    records.put(MONDAY, "wash", TaskResult.DONE, at(MONDAY, 7, 5))
    due = s.due_task(at(TUESDAY, 7, 0), records)
    assert due is not None and due.day == TUESDAY


def test_今日に当てはまらないタスクは始めない() -> None:
    s = scheduler([task_dict("school", at="07:00", days="weekday")])
    records = FakeRecords()
    assert s.due_task(at(SATURDAY, 7, 0), records) is None
    assert due_id(s, at(MONDAY, 7, 0), records) == "school"


# 設定の順（3.10）


def test_同時に始められるときは設定の順で最初のもの() -> None:
    s = scheduler([task_dict("b", at="07:10"), task_dict("a", at="07:00")])
    records = FakeRecords()
    assert due_id(s, at(MONDAY, 7, 20), records) == "b"
    records.put(MONDAY, "b", TaskResult.DONE, at(MONDAY, 7, 25))
    assert due_id(s, at(MONDAY, 7, 25), records) == "a"


# 続きのタスク（3.3、3.4）


def _chain() -> Scheduler:
    return scheduler(
        [
            task_dict("wash", at="07:00"),
            task_dict("teeth", at=None, after="wash"),
            task_dict("dress", at=None, after="teeth"),
        ]
    )


def test_続きのタスクは前ができたで終わったら始まる() -> None:
    s = _chain()
    records = FakeRecords()
    assert due_id(s, at(MONDAY, 7, 0), records) == "wash"
    assert due_id(s, at(MONDAY, 7, 3), records) == "wash"
    records.put(MONDAY, "wash", TaskResult.DONE, at(MONDAY, 7, 5))
    assert due_id(s, at(MONDAY, 7, 5), records) == "teeth"


def test_続きのタスクは前がスキップでも始まる() -> None:
    s = _chain()
    records = FakeRecords()
    records.put(MONDAY, "wash", TaskResult.SKIPPED, at(MONDAY, 7, 40))
    assert due_id(s, at(MONDAY, 7, 41), records) == "teeth"


def test_続きのタスクの窓は前の終わりから60分() -> None:
    s = _chain()
    records = FakeRecords()
    records.put(MONDAY, "wash", TaskResult.DONE, at(MONDAY, 7, 5))
    assert due_id(s, at(MONDAY, 8, 4) + timedelta(seconds=59), records) == "teeth"
    assert s.due_task(at(MONDAY, 8, 5), records) is None


def test_続きのタスクの窓を過ぎたら後ろも全部始めない() -> None:
    s = _chain()
    records = FakeRecords()
    records.put(MONDAY, "wash", TaskResult.DONE, at(MONDAY, 7, 5))
    # teeth は始まらないまま窓を過ぎた。dress も始めない
    assert s.due_task(at(MONDAY, 8, 30), records) is None


def test_前が中断なら続きと後ろを全部始めない() -> None:
    s = _chain()
    records = FakeRecords()
    records.put(MONDAY, "wash", TaskResult.INTERRUPTED, at(MONDAY, 7, 5))
    assert s.due_task(at(MONDAY, 7, 5), records) is None
    assert s.due_task(at(MONDAY, 7, 30), records) is None


def test_途中が中断ならその後ろを始めない() -> None:
    s = _chain()
    records = FakeRecords()
    records.put(MONDAY, "wash", TaskResult.DONE, at(MONDAY, 7, 5))
    records.put(MONDAY, "teeth", TaskResult.INTERRUPTED, at(MONDAY, 7, 10))
    assert s.due_task(at(MONDAY, 7, 10), records) is None


def test_時刻のタスクが窓を過ぎたら続きも始めない() -> None:
    s = _chain()
    records = FakeRecords()
    assert s.due_task(at(MONDAY, 8, 0), records) is None
    assert s.due_task(at(MONDAY, 9, 0), records) is None


def test_前が今日に当てはまらなければ続きも始めない() -> None:
    s = scheduler(
        [
            task_dict("school", at="07:00", days="weekday"),
            task_dict("bag", at=None, after="school"),
        ]
    )
    records = FakeRecords()
    assert s.due_task(at(SATURDAY, 7, 0), records) is None
    assert s.due_task(at(SATURDAY, 7, 30), records) is None


def test_前が終わっていない間は続きを始めない() -> None:
    s = _chain()
    records = FakeRecords()
    # wash の最中（結果はまだない）。teeth と dress は待つ
    assert due_id(s, at(MONDAY, 7, 30), records) == "wash"


def test_終わった時刻のない前の結果からは続きを始めない() -> None:
    s = _chain()
    records = FakeRecords()
    records.put(MONDAY, "wash", TaskResult.DONE, None)
    assert s.due_task(at(MONDAY, 7, 10), records) is None


# 起床時刻（6.5、6.7）


def test_起床時刻は設定の値() -> None:
    s = scheduler([task_dict("wash", at="07:00")], wake={"weekday": "06:30", "holiday": "08:00"})
    assert s.wake_time(MONDAY, TOKYO) == at(MONDAY, 6, 30)
    assert s.wake_time(SATURDAY, TOKYO) == at(SATURDAY, 8, 0)


def test_起床時刻の既定はその日の最初の時刻のタスク() -> None:
    s = scheduler(
        [
            task_dict("late", at="07:30"),
            task_dict("school", at="06:45", days="weekday"),
            task_dict("follow", at=None, after="late"),
            task_dict("sleep", at="21:00", sleep=True),
        ]
    )
    assert s.wake_time(MONDAY, TOKYO) == at(MONDAY, 6, 45)
    assert s.wake_time(SATURDAY, TOKYO) == at(SATURDAY, 7, 30)


def test_起床時刻がなければNone() -> None:
    s = scheduler([task_dict("school", at="07:00", days="weekday")])
    assert s.wake_time(SATURDAY, TOKYO) is None


def test_起床時刻はタイムゾーン付き() -> None:
    s = scheduler([task_dict("wash", at="07:00")])
    wake = s.wake_time(MONDAY, TOKYO)
    assert wake is not None and wake.tzinfo is TOKYO


def test_起床時刻より前の時刻のタスクは始めない() -> None:
    s = scheduler(
        [
            task_dict("early", at="05:30"),
            task_dict("after_early", at=None, after="early"),
            task_dict("wash", at="07:00"),
        ],
        wake={"weekday": "06:00", "holiday": "06:00"},
    )
    records = FakeRecords()
    assert s.due_task(at(MONDAY, 5, 30), records) is None
    assert s.due_task(at(MONDAY, 6, 10), records) is None
    assert due_id(s, at(MONDAY, 7, 0), records) == "wash"


def test_起床時刻ちょうどのタスクは始まる() -> None:
    s = scheduler([task_dict("wash", at="06:00")], wake={"weekday": "06:00"})
    assert due_id(s, at(MONDAY, 6, 0), FakeRecords()) == "wash"


# 寝るタスク


def test_寝るタスクはその日の種類のもの() -> None:
    s = scheduler(
        [
            task_dict("wash", at="07:00"),
            task_dict("bed_w", at="21:00", days="weekday", sleep=True),
            task_dict("bed_h", at="22:00", days="holiday", sleep=True),
        ]
    )
    weekday = s.sleep_task(MONDAY)
    holiday = s.sleep_task(SATURDAY)
    assert weekday is not None and weekday.id == "bed_w"
    assert holiday is not None and holiday.id == "bed_h"


def test_寝るタスクがなければNone() -> None:
    s = scheduler([task_dict("wash", at="07:00")])
    assert s.sleep_task(MONDAY) is None


# 予告（3.5、3.6）


def test_予告は予告の時刻からタスクの時刻までの間に出る() -> None:
    s = scheduler([task_dict("wash", at="07:00", notice="もうすぐ", notice_min=10)])
    records = FakeRecords()
    assert s.due_notice(at(MONDAY, 6, 49), records) is None
    notice = s.due_notice(at(MONDAY, 6, 50), records)
    assert isinstance(notice, DueNotice)
    assert notice.task.id == "wash" and notice.day == MONDAY
    assert s.due_notice(at(MONDAY, 6, 59), records) is not None
    assert s.due_notice(at(MONDAY, 7, 0), records) is None


def test_予告を言ったら出ない() -> None:
    s = scheduler([task_dict("wash", at="07:00", notice="もうすぐ")])
    records = FakeRecords()
    records.notices.add((MONDAY, "wash"))
    assert s.due_notice(at(MONDAY, 6, 55), records) is None


def test_タスクの日に結果があれば予告は出ない() -> None:
    s = scheduler([task_dict("wash", at="07:00", notice="もうすぐ")])
    records = FakeRecords()
    records.put(MONDAY, "wash", TaskResult.SKIPPED, at(MONDAY, 6, 30))
    assert s.due_notice(at(MONDAY, 6, 55), records) is None


def test_予告を持たないタスクは予告しない() -> None:
    s = scheduler([task_dict("wash", at="07:00")])
    assert s.due_notice(at(MONDAY, 6, 55), FakeRecords()) is None


def test_0時5分のタスクの予告は前の日の23時55分に出る() -> None:
    s = scheduler([task_dict("night", at="00:05", notice="もうすぐ", notice_min=10)])
    records = FakeRecords()
    assert s.due_notice(at(MONDAY, 23, 54), records) is None
    notice = s.due_notice(at(MONDAY, 23, 55), records)
    assert notice is not None
    assert notice.task.id == "night"
    assert notice.day == TUESDAY
    assert s.due_notice(at(TUESDAY, 0, 0), records) is not None
    assert s.due_notice(at(TUESDAY, 0, 5), records) is None


def test_日付をまたぐ予告は言った記録をタスクの日で見る() -> None:
    s = scheduler([task_dict("night", at="00:05", notice="もうすぐ")])
    records = FakeRecords()
    records.notices.add((TUESDAY, "night"))
    assert s.due_notice(at(MONDAY, 23, 58), records) is None
    records.notices = {(MONDAY, "night")}
    assert s.due_notice(at(MONDAY, 23, 58), records) is not None


def test_予告はタスクの日の種類で決める() -> None:
    # 金曜の夜の、土曜（休日）の平日だけのタスクの予告は出ない
    friday = date(2026, 4, 10)
    s = scheduler([task_dict("school", at="00:05", days="weekday", notice="もうすぐ")])
    assert s.due_notice(at(friday, 23, 58), FakeRecords()) is None
    sunday = date(2026, 4, 12)
    notice = s.due_notice(at(sunday, 23, 58), FakeRecords())
    assert notice is not None and notice.day == date(2026, 4, 13)


def test_予告は設定の順で最初のもの() -> None:
    s = scheduler(
        [
            task_dict("b", at="07:05", notice="b"),
            task_dict("a", at="07:00", notice="a"),
        ]
    )
    notice = s.due_notice(at(MONDAY, 6, 56), FakeRecords())
    assert notice is not None and notice.task.id == "b"


def test_起床時刻より前のタスクの予告は出ない() -> None:
    s = scheduler(
        [task_dict("early", at="05:30", notice="もうすぐ"), task_dict("wash", at="07:00")],
        wake={"weekday": "06:00", "holiday": "06:00"},
    )
    assert s.due_notice(at(MONDAY, 5, 25), FakeRecords()) is None


# 次の予定（9.4）


def test_次の予定はこの後の最も早い時刻のタスク() -> None:
    s = scheduler(
        [
            task_dict("late", at="08:00"),
            task_dict("wash", at="07:00"),
            task_dict("follow", at=None, after="wash"),
        ]
    )
    records = FakeRecords()
    assert s.next_task_at(at(MONDAY, 6, 0), records) == at(MONDAY, 7, 0)
    assert s.next_task_at(at(MONDAY, 7, 0), records) == at(MONDAY, 8, 0)
    assert s.next_task_at(at(MONDAY, 8, 0), records) is None


def test_次の予定は結果のあるタスクを数えない() -> None:
    s = scheduler([task_dict("wash", at="07:00"), task_dict("late", at="08:00")])
    records = FakeRecords()
    records.put(MONDAY, "wash", TaskResult.SKIPPED, at(MONDAY, 6, 0))
    assert s.next_task_at(at(MONDAY, 6, 30), records) == at(MONDAY, 8, 0)


def test_次の予定は今日に当てはまるタスクだけ() -> None:
    s = scheduler([task_dict("school", at="07:00", days="weekday"), task_dict("play", at="09:00")])
    assert s.next_task_at(at(SATURDAY, 6, 0), FakeRecords()) == at(SATURDAY, 9, 0)


def test_次の予定は起床時刻より前のタスクを数えない() -> None:
    s = scheduler(
        [task_dict("early", at="05:30"), task_dict("wash", at="07:00")],
        wake={"weekday": "06:00", "holiday": "06:00"},
    )
    assert s.next_task_at(at(MONDAY, 5, 0), FakeRecords()) == at(MONDAY, 7, 0)


def test_タイムゾーンのない時刻は受け付けない() -> None:
    s = scheduler([task_dict("wash", at="07:00")])
    with pytest.raises(ValueError):
        s.due_task(datetime(2026, 4, 6, 7, 0), FakeRecords())
    with pytest.raises(ValueError):
        s.in_sleep_period(datetime(2026, 4, 6, 7, 0), FakeRecords())


# 続きのタスクのつながり（3.3、3.4）の追加


def test_前のタスクが設定の後ろにあっても続きは始まる() -> None:
    s = scheduler(
        [
            task_dict("teeth", at=None, after="wash"),
            task_dict("wash", at="07:00"),
        ]
    )
    records = FakeRecords()
    assert due_id(s, at(MONDAY, 7, 0), records) == "wash"
    records.put(MONDAY, "wash", TaskResult.DONE, at(MONDAY, 7, 5))
    assert due_id(s, at(MONDAY, 7, 5), records) == "teeth"


def test_途中のタスクが今日に当てはまらなければ後ろも始めない() -> None:
    s = scheduler(
        [
            task_dict("wash", at="07:00"),
            task_dict("bag", at=None, after="wash", days="weekday"),
            task_dict("shoes", at=None, after="bag"),
        ]
    )
    records = FakeRecords()
    records.put(SATURDAY, "wash", TaskResult.DONE, at(SATURDAY, 7, 5))
    assert s.due_task(at(SATURDAY, 7, 5), records) is None
    assert s.due_task(at(SATURDAY, 7, 30), records) is None


def test_続きの窓は遅く終わった時刻から数える() -> None:
    s = _chain()
    records = FakeRecords()
    # wash は窓の終わりの近くでスキップされた。teeth の窓は 07:55 から 08:55 まで
    records.put(MONDAY, "wash", TaskResult.SKIPPED, at(MONDAY, 7, 55))
    assert due_id(s, at(MONDAY, 8, 30), records) == "teeth"
    assert due_id(s, at(MONDAY, 8, 54) + timedelta(seconds=59), records) == "teeth"
    assert s.due_task(at(MONDAY, 8, 55), records) is None


# 割り込み（5.1〜5.4、6.1）

WAKE_6 = {"weekday": "06:00", "holiday": "06:00"}


def _cut_in_scheduler(kind: str) -> Scheduler:
    """07:00 の wash が動いていて、07:10 に kind の時刻のタスク new が来る設定。"""
    fields: dict[str, Any] = {}
    if kind == "firm":
        fields["firm"] = True
    elif kind == "sleep":
        fields["sleep"] = True
    return scheduler(
        [task_dict("wash", at="07:00"), task_dict("new", at="07:10", **fields)],
        wake=WAKE_6,
    )


@pytest.mark.parametrize(
    ("kind", "phase", "expected"),
    [
        ("plain", Phase.READY, "new"),
        ("plain", Phase.CALLING, None),
        ("plain", Phase.LATER, None),
        ("plain", Phase.PRAISING, None),
        ("firm", Phase.READY, "new"),
        ("firm", Phase.CALLING, "new"),
        ("firm", Phase.LATER, "new"),
        ("firm", Phase.PRAISING, None),
        # 寝るタスクは、時刻から待つ長さ（既定 30 分）の間は割り込まない（6.1）
        ("sleep", Phase.READY, None),
        ("sleep", Phase.CALLING, None),
        ("sleep", Phase.LATER, None),
        ("sleep", Phase.PRAISING, None),
    ],
)
def test_段階ごとの割り込みの表(kind: str, phase: Phase, expected: str | None) -> None:
    s = _cut_in_scheduler(kind)
    records = FakeRecords()
    current = s.due_task(at(MONDAY, 7, 0), records)
    assert current is not None and current.task.id == "wash"
    cut = s.cut_in(at(MONDAY, 7, 10), records, current, phase)
    assert (cut.task.id if cut is not None else None) == expected
    if cut is not None:
        assert cut.day == MONDAY


def test_時刻の前は割り込まない() -> None:
    s = _cut_in_scheduler("firm")
    records = FakeRecords()
    current = s.due_task(at(MONDAY, 7, 0), records)
    assert current is not None
    assert s.cut_in(at(MONDAY, 7, 9), records, current, Phase.READY) is None


def test_今のタスクには割り込まない() -> None:
    s = scheduler([task_dict("wash", at="07:00", firm=True)])
    records = FakeRecords()
    current = s.due_task(at(MONDAY, 7, 0), records)
    assert current is not None
    assert s.cut_in(at(MONDAY, 7, 5), records, current, Phase.CALLING) is None
    assert s.cut_in(at(MONDAY, 7, 5), records, current, Phase.READY) is None


def test_続きのタスクは割り込まない() -> None:
    tasks = [
        task_dict("wash", at="07:00"),
        task_dict("teeth", at=None, after="wash", firm=True),
        task_dict("bag", at="07:05"),
    ]
    s = scheduler(tasks)
    records = FakeRecords()
    records.put(MONDAY, "wash", TaskResult.DONE, at(MONDAY, 7, 1))
    assert due_id(s, at(MONDAY, 7, 6), records) == "teeth"
    # bag を動かしている間に、続きの teeth は始められるが割り込まない
    bag = running(tasks, "bag", MONDAY)
    assert s.cut_in(at(MONDAY, 7, 6), records, bag, Phase.READY) is None
    assert s.cut_in(at(MONDAY, 7, 6), records, bag, Phase.CALLING) is None


def test_割り込みは設定の順で最初のもの() -> None:
    s = scheduler(
        [
            task_dict("wash", at="07:00"),
            task_dict("b", at="07:10", firm=True),
            task_dict("a", at="07:05", firm=True),
        ]
    )
    records = FakeRecords()
    current = s.due_task(at(MONDAY, 7, 0), records)
    assert current is not None
    cut = s.cut_in(at(MONDAY, 7, 10), records, current, Phase.CALLING)
    assert cut is not None and cut.task.id == "b"


def test_割り込めないタスクは今のタスクの後に窓の中なら始まる() -> None:
    s = _cut_in_scheduler("plain")
    records = FakeRecords()
    current = s.due_task(at(MONDAY, 7, 0), records)
    assert current is not None
    # 催促の間は割り込まない
    assert s.cut_in(at(MONDAY, 7, 10), records, current, Phase.CALLING) is None
    assert s.cut_in(at(MONDAY, 7, 40), records, current, Phase.LATER) is None
    # wash が終わった後、new の窓（07:10〜08:10）の中なら始まる
    records.put(MONDAY, "wash", TaskResult.DONE, at(MONDAY, 7, 45))
    assert due_id(s, at(MONDAY, 7, 45), records) == "new"


def test_割り込めないタスクは今のタスクが窓の後に終われば始まらない() -> None:
    s = _cut_in_scheduler("plain")
    records = FakeRecords()
    records.put(MONDAY, "wash", TaskResult.SKIPPED, at(MONDAY, 8, 10))
    assert s.due_task(at(MONDAY, 8, 10), records) is None


# おやすみの時間（6.4、6.5、6.7、6.8、6.10、7.1、7.2、7.4）


SLEEPY_TASKS = [
    task_dict("wash", at="07:00", notice="もうすぐ"),
    task_dict("teeth", at=None, after="bed"),
    task_dict("bed", at="21:00", sleep=True, notice="もうすぐ"),
    task_dict("late", at="23:00"),
]
SLEEPY_WAKE = {"weekday": "06:30", "holiday": "08:00"}


def _sleepy() -> Scheduler:
    return scheduler(SLEEPY_TASKS, wake=SLEEPY_WAKE)


def test_起床時刻より前はおやすみ() -> None:
    s = _sleepy()
    records = FakeRecords()
    assert s.in_sleep_period(at(MONDAY, 0, 0), records)
    assert s.in_sleep_period(at(MONDAY, 6, 29) + timedelta(seconds=59), records)
    assert not s.in_sleep_period(at(MONDAY, 6, 30), records)
    # 休日は起床時刻が違う
    assert s.in_sleep_period(at(SATURDAY, 7, 59), records)
    assert not s.in_sleep_period(at(SATURDAY, 8, 0), records)


def test_寝るタスクが終わったらおやすみ() -> None:
    s = _sleepy()
    for result in TaskResult:
        records = FakeRecords()
        records.put(MONDAY, "bed", result, at(MONDAY, 21, 10))
        assert s.in_sleep_period(at(MONDAY, 21, 10), records)
        assert s.in_sleep_period(at(MONDAY, 23, 59), records)


def test_寝るタスクの前と窓の中で結果がなければおやすみでない() -> None:
    s = _sleepy()
    records = FakeRecords()
    assert not s.in_sleep_period(at(MONDAY, 20, 59), records)
    assert not s.in_sleep_period(at(MONDAY, 21, 59) + timedelta(seconds=59), records)


def test_寝るタスクの窓を過ぎたらおやすみ() -> None:
    s = _sleepy()
    records = FakeRecords()
    assert s.in_sleep_period(at(MONDAY, 22, 0), records)


def test_時刻の前に寝るタスクの結果があってもまだおやすみでない() -> None:
    # 前の夜に付いた結果ではない、今日の時刻の前の結果（ふつうは起きない）
    s = _sleepy()
    records = FakeRecords()
    records.put(MONDAY, "bed", TaskResult.SKIPPED, at(MONDAY, 20, 0))
    assert not s.in_sleep_period(at(MONDAY, 20, 30), records)


def test_前の日の寝るタスクの結果は今日の昼をおやすみにしない() -> None:
    s = _sleepy()
    records = FakeRecords()
    records.put(MONDAY, "bed", TaskResult.DONE, at(MONDAY, 21, 5))
    assert not s.in_sleep_period(at(TUESDAY, 12, 0), records)
    assert s.in_sleep_period(at(TUESDAY, 6, 0), records)


def test_寝るタスクがなければおやすみの時間はない() -> None:
    s = scheduler([task_dict("wash", at="07:00")], wake=WAKE_6)
    records = FakeRecords()
    assert not s.in_sleep_period(at(MONDAY, 0, 0), records)
    assert not s.in_sleep_period(at(MONDAY, 5, 0), records)
    assert not s.in_sleep_period(at(MONDAY, 23, 59), records)


def test_今日の寝るタスクがない日は起床時刻の前だけおやすみ() -> None:
    s = scheduler(
        [task_dict("wash", at="07:00"), task_dict("bed", at="21:00", days="weekday", sleep=True)],
        wake=WAKE_6,
    )
    records = FakeRecords()
    assert s.in_sleep_period(at(SATURDAY, 5, 0), records)
    assert not s.in_sleep_period(at(SATURDAY, 23, 0), records)


def test_おやすみの時間は始めるタスクも予告も次の予定もない() -> None:
    s = _sleepy()
    records = FakeRecords()
    records.put(MONDAY, "bed", TaskResult.DONE, at(MONDAY, 21, 5))
    # 寝た後の 23:00 の late と、寝るタスクの続きの teeth は始めない（6.4、6.5）
    assert s.due_task(at(MONDAY, 21, 5), records) is None
    assert s.due_task(at(MONDAY, 23, 0), records) is None
    assert s.next_task_at(at(MONDAY, 21, 30), records) is None
    late_notice = scheduler(
        [
            task_dict("bed", at="21:00", sleep=True),
            task_dict("late", at="23:00", notice="もうすぐ"),
        ],
        wake=WAKE_6,
    )
    assert late_notice.due_notice(at(MONDAY, 22, 55), records) is None


def test_起床時刻の前は予告も出ない() -> None:
    s = scheduler(
        [task_dict("wash", at="07:00"), task_dict("bed", at="21:00", sleep=True)],
        wake=WAKE_6,
    )
    s_notice = scheduler(
        [
            task_dict("wash", at="06:00", notice="もうすぐ", notice_min=30),
            task_dict("bed", at="21:00", sleep=True),
        ],
        wake={"weekday": "06:00", "holiday": "06:00"},
    )
    records = FakeRecords()
    assert s.due_task(at(MONDAY, 5, 0), records) is None
    assert s.next_task_at(at(MONDAY, 5, 0), records) is None
    # 06:00 の予告の時刻（05:30）は起床時刻より前なので言わない
    assert s_notice.due_notice(at(MONDAY, 5, 45), records) is None


def test_寝るタスクがなければ夜中もタスクと予告が出る() -> None:
    s = scheduler(
        [task_dict("night", at="02:00", notice="もうすぐ")],
        wake={"weekday": "01:00", "holiday": "01:00"},
    )
    records = FakeRecords()
    assert s.due_notice(at(MONDAY, 1, 55), records) is not None
    assert due_id(s, at(MONDAY, 2, 0), records) == "night"


def test_寝るタスクの窓の中で結果がなければ寝るタスクを始める() -> None:
    # 7.4: 21:30 に起動した。寝るタスクにまだ結果がない
    s = _sleepy()
    records = FakeRecords()
    assert not s.in_sleep_period(at(MONDAY, 21, 30), records)
    assert due_id(s, at(MONDAY, 21, 30), records) == "bed"


def test_寝るタスクの予告はおやすみの前なら出る() -> None:
    s = _sleepy()
    assert s.due_notice(at(MONDAY, 20, 55), FakeRecords()) is not None


def test_おやすみの時間は割り込みもない() -> None:
    s = _sleepy()
    records = FakeRecords()
    records.put(MONDAY, "bed", TaskResult.DONE, at(MONDAY, 21, 5))
    teeth = running(SLEEPY_TASKS, "teeth", MONDAY, wake=SLEEPY_WAKE)
    assert s.cut_in(at(MONDAY, 23, 0), records, teeth, Phase.READY) is None


def test_寝るタスクは割り込んだ後におやすみになる() -> None:
    s = _sleepy()
    records = FakeRecords()
    wash = running(SLEEPY_TASKS, "wash", MONDAY, wake=SLEEPY_WAKE)
    cut = s.cut_in(at(MONDAY, 21, 30), records, wash, Phase.CALLING)
    assert cut is not None and cut.task.id == "bed"


@pytest.mark.parametrize("phase", [Phase.READY, Phase.CALLING, Phase.LATER])
def test_寝るタスクは待つ長さの間はほかのタスクを待ち_過ぎたらどの段階にも割り込む(
    phase: Phase,
) -> None:
    # 6.1、6.11: 21:00 の寝るタスクは 21:30 まで待ち、21:30 からは割り込む
    s = _sleepy()
    records = FakeRecords()
    wash = running(SLEEPY_TASKS, "wash", MONDAY, wake=SLEEPY_WAKE)
    assert s.cut_in(at(MONDAY, 21, 0), records, wash, phase) is None
    assert s.cut_in(at(MONDAY, 21, 29) + timedelta(seconds=59), records, wash, phase) is None
    cut = s.cut_in(at(MONDAY, 21, 30), records, wash, phase)
    assert cut is not None and cut.task.id == "bed"


def test_待つ長さが0なら寝るタスクは時刻にすぐ割り込む() -> None:
    s = scheduler(SLEEPY_TASKS, wake=SLEEPY_WAKE, sleep_wait_min=0)
    records = FakeRecords()
    wash = running(SLEEPY_TASKS, "wash", MONDAY, wake=SLEEPY_WAKE)
    cut = s.cut_in(at(MONDAY, 21, 0), records, wash, Phase.READY)
    assert cut is not None and cut.task.id == "bed"


NIGHT_CHAIN = [
    task_dict("bed", at="21:00", sleep=True),  # 設定の順で前にあっても、待つ間は後に回す
    task_dict("bath", at="20:00"),
    task_dict("pajamas", at=None, after="bath"),
    task_dict("teeth", at=None, after="pajamas"),
]


def test_寝るタスクを待つ間は続きのタスクを先に始め_流れが終わったら寝るタスクを始める() -> None:
    # 6.1: パジャマが 21:05 に終わったら、寝るタスクより先に続きの歯みがきを始める
    s = scheduler(NIGHT_CHAIN, wake=SLEEPY_WAKE)
    records = FakeRecords()
    records.put(MONDAY, "bath", TaskResult.DONE, at(MONDAY, 20, 30))
    records.put(MONDAY, "pajamas", TaskResult.DONE, at(MONDAY, 21, 5))
    assert due_id(s, at(MONDAY, 21, 5), records) == "teeth"
    records.put(MONDAY, "teeth", TaskResult.DONE, at(MONDAY, 21, 15))
    assert due_id(s, at(MONDAY, 21, 15), records) == "bed"


def test_寝るタスクの時刻にほかのタスクがなければすぐ始める() -> None:
    s = scheduler(NIGHT_CHAIN, wake=SLEEPY_WAKE)
    records = FakeRecords()
    for task_id in ("bath", "pajamas", "teeth"):
        records.put(MONDAY, task_id, TaskResult.DONE, at(MONDAY, 20, 40))
    assert due_id(s, at(MONDAY, 21, 0), records) == "bed"


def test_待つ長さを過ぎたら続きのタスクより寝るタスクを先に始める() -> None:
    s = scheduler(NIGHT_CHAIN, wake=SLEEPY_WAKE)
    records = FakeRecords()
    records.put(MONDAY, "bath", TaskResult.DONE, at(MONDAY, 20, 30))
    records.put(MONDAY, "pajamas", TaskResult.DONE, at(MONDAY, 21, 31))
    assert due_id(s, at(MONDAY, 21, 31), records) == "bed"
