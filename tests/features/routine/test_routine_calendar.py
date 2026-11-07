"""今日の種類の判定（2.1〜2.6）のテスト。"""

from __future__ import annotations

import logging
import sys
from datetime import date

import pytest

from osekkai.features.routine.calendar import DayKind, DayKinds, JpHolidaySource
from osekkai.features.routine.config import TaskConfig

MONDAY = date(2026, 4, 6)
SATURDAY = date(2026, 4, 11)
SUNDAY = date(2026, 4, 12)
SHOWA_DAY = date(2026, 4, 29)  # 昭和の日（水）
SUBSTITUTE = date(2026, 5, 6)  # 振替休日（5/3 の憲法記念日が日曜）
EXTRA = date(2026, 4, 8)  # 水曜日


class FakeHolidays:
    """決まった日だけを祝日とする、テスト用の祝日の判定。"""

    def __init__(self, days: set[date], *, available: bool = True) -> None:
        self._days = days
        self._available = available
        self.asked: list[date] = []

    @property
    def available(self) -> bool:
        return self._available

    def is_holiday(self, day: date) -> bool:
        self.asked.append(day)
        return self._available and day in self._days


class BrokenHolidays:
    """判定のたびに例外を出す祝日の判定。"""

    @property
    def available(self) -> bool:
        return True

    def is_holiday(self, day: date) -> bool:
        raise RuntimeError("壊れた")


def _task(days: str) -> TaskConfig:
    return TaskConfig.model_validate(
        {"id": "t", "at": "07:00", "days": days, "call": "c", "praise": "p", "lcd": "x"}
    )


def test_土曜日と日曜日は休日() -> None:
    kinds = DayKinds([], FakeHolidays(set()))
    assert kinds.kind_of(SATURDAY) is DayKind.HOLIDAY
    assert kinds.kind_of(SUNDAY) is DayKind.HOLIDAY


def test_祝日は休日() -> None:
    kinds = DayKinds([], FakeHolidays({SHOWA_DAY}))
    assert kinds.kind_of(SHOWA_DAY) is DayKind.HOLIDAY


def test_追加の休日は休日() -> None:
    kinds = DayKinds([EXTRA], FakeHolidays(set()))
    assert kinds.kind_of(EXTRA) is DayKind.HOLIDAY


def test_休日でない日は平日() -> None:
    kinds = DayKinds([EXTRA], FakeHolidays({SHOWA_DAY}))
    assert kinds.kind_of(MONDAY) is DayKind.WEEKDAY


def test_種類の値は設定の日の種類の文字と同じ() -> None:
    assert DayKind("weekday") is DayKind.WEEKDAY
    assert DayKind("holiday") is DayKind.HOLIDAY


def test_本物の祝日の判定で祝日と振替休日が休日になる() -> None:
    source = JpHolidaySource()
    kinds = DayKinds([], source)
    assert source.available is True
    assert kinds.kind_of(SHOWA_DAY) is DayKind.HOLIDAY
    assert kinds.kind_of(SUBSTITUTE) is DayKind.HOLIDAY
    assert kinds.kind_of(date(2026, 1, 1)) is DayKind.HOLIDAY
    assert kinds.kind_of(MONDAY) is DayKind.WEEKDAY


@pytest.mark.parametrize(
    ("days", "weekday", "holiday"),
    [("everyday", True, True), ("weekday", True, False), ("holiday", False, True)],
)
def test_タスクの対象の日が当てはまるか(days: str, weekday: bool, holiday: bool) -> None:
    kinds = DayKinds([], FakeHolidays({SHOWA_DAY}))
    task = _task(days)
    assert kinds.applies(task, MONDAY) is weekday
    assert kinds.applies(task, SATURDAY) is holiday
    assert kinds.applies(task, SHOWA_DAY) is holiday


def test_祝日を判定できないときは土日と追加の休日だけ() -> None:
    kinds = DayKinds([EXTRA], FakeHolidays({SHOWA_DAY}, available=False))
    assert kinds.kind_of(SHOWA_DAY) is DayKind.WEEKDAY
    assert kinds.kind_of(SATURDAY) is DayKind.HOLIDAY
    assert kinds.kind_of(EXTRA) is DayKind.HOLIDAY
    assert kinds.kind_of(MONDAY) is DayKind.WEEKDAY


def test_祝日の判定の例外は祝日でないとして型の名前だけを記録する(
    caplog: pytest.LogCaptureFixture,
) -> None:
    kinds = DayKinds([EXTRA], BrokenHolidays())
    with caplog.at_level(logging.WARNING, logger="osekkai.features.routine"):
        assert kinds.kind_of(MONDAY) is DayKind.WEEKDAY
        assert kinds.kind_of(SATURDAY) is DayKind.HOLIDAY
        assert kinds.kind_of(EXTRA) is DayKind.HOLIDAY
    messages = [r.getMessage() for r in caplog.records]
    assert messages
    assert all("RuntimeError" in m for m in messages)
    assert all("壊れた" not in m and "2026" not in m for m in messages)


def test_jpholiday_を読み込めなければ使えないとして常に祝日でない(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "jpholiday", None)
    source = JpHolidaySource()
    assert source.available is False
    assert source.is_holiday(SHOWA_DAY) is False
    kinds = DayKinds([], source)
    assert kinds.kind_of(SHOWA_DAY) is DayKind.WEEKDAY
    assert kinds.kind_of(SUNDAY) is DayKind.HOLIDAY


def test_jpholiday_は初めて使うときに読み込む(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "jpholiday", None)
    source = JpHolidaySource()  # 作るだけでは読み込まない
    monkeypatch.delitem(sys.modules, "jpholiday")
    assert source.available is True
    assert source.is_holiday(SUBSTITUTE) is True


def test_jpholiday_の判定の例外は祝日でないとする(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import jpholiday

    def boom(day: date) -> bool:
        raise ValueError("x")

    monkeypatch.setattr(jpholiday, "is_holiday", boom)
    source = JpHolidaySource()
    with caplog.at_level(logging.WARNING, logger="osekkai.features.routine"):
        assert source.is_holiday(SHOWA_DAY) is False
    assert any("ValueError" in r.getMessage() for r in caplog.records)
