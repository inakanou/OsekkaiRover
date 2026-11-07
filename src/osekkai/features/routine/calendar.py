"""今日の種類（平日か休日か）の判定（2.1〜2.6）。

休日は、土日・日本の祝日（振替休日を含む）・設定の追加の休日。それ以外は平日。
祝日の判定には jpholiday を使う。jpholiday は初めて使うときに、この中だけで読み込む。
読み込めなければ「使えない」として、土日と追加の休日だけで決める（2.6）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from datetime import date
from enum import Enum
from typing import Protocol

from osekkai.features.routine.config import TaskConfig, WakeKind

logger = logging.getLogger(__name__)

_SATURDAY = 5  # date.weekday() の土曜日。日曜日は 6


class DayKind(Enum):
    """日の種類。値は設定の `days` と `wake` のキーと同じ文字。"""

    WEEKDAY = "weekday"
    HOLIDAY = "holiday"


_WAKE_KINDS: dict[DayKind, WakeKind] = {DayKind.WEEKDAY: "weekday", DayKind.HOLIDAY: "holiday"}


class HolidaySource(Protocol):
    """祝日の判定。"""

    @property
    def available(self) -> bool:
        """祝日を判定できるかどうか。"""
        ...

    def is_holiday(self, day: date) -> bool:
        """祝日かどうか。使えないときは常に False。"""
        ...


class JpHolidaySource:
    """jpholiday を初めて使うときに読み込む。読み込めなければ available は False。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded = False
        self._check: Callable[[date], bool] | None = None

    def _load(self) -> Callable[[date], bool] | None:
        # 2 つのスレッドが同時に初めて呼んでも、読み込みの終わりを待ってから答える
        with self._lock:
            if not self._loaded:
                try:
                    import jpholiday
                except Exception as error:  # 壊れた入れ方でも、使えないとして扱う
                    logger.warning(
                        "祝日のライブラリを読み込めませんでした: %s", type(error).__name__
                    )
                else:
                    self._check = jpholiday.is_holiday
                self._loaded = True
            return self._check

    @property
    def available(self) -> bool:
        return self._load() is not None

    def is_holiday(self, day: date) -> bool:
        check = self._load()
        if check is None:
            return False
        try:
            return bool(check(day))
        except Exception as error:
            logger.warning("祝日を判定できませんでした: %s", type(error).__name__)
            return False


class DayKinds:
    """日付から、平日か休日かを決める。"""

    def __init__(self, extra_holidays: Iterable[date], holidays: HolidaySource) -> None:
        self._extra = frozenset(extra_holidays)
        self._holidays = holidays

    def kind_of(self, day: date) -> DayKind:
        """その日の種類。土日・祝日・追加の休日なら休日、それ以外は平日（2.1〜2.4）。"""
        if day.weekday() >= _SATURDAY or day in self._extra or self._is_holiday(day):
            return DayKind.HOLIDAY
        return DayKind.WEEKDAY

    def applies(self, task: TaskConfig, day: date) -> bool:
        """そのタスクが、その日に当てはまるかどうか（2.5）。"""
        return task.applies_to(_WAKE_KINDS[self.kind_of(day)])

    def _is_holiday(self, day: date) -> bool:
        """祝日かどうか。判定で例外が出たら、祝日でないとして型の名前だけを記録する。"""
        try:
            return self._holidays.is_holiday(day)
        except Exception as error:
            logger.warning("祝日を判定できませんでした: %s", type(error).__name__)
            return False
