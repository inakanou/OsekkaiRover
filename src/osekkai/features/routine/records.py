"""記録と星（8.1〜8.8、3.5）。

日ごとのタスクの結果と終わった時刻、言った予告、星の数を、基盤の保存先に書く。
記録（records）と控え（stars）の 2 つのファイルを、この順に原子的に書く（8.4）。
星の数は 2 つの大きい方を使うので、片方だけ書けた後でも減らない。
記録の形を知るのはここだけ。中の値（タスクの名前を含む）はログに出さない。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Protocol

from osekkai.core.clock import Clock
from osekkai.core.storage import JsonValue, StateStore

logger = logging.getLogger(__name__)

_VERSION = 1
_RECORDS = "records"
_STARS = "stars"
# 版の違う記録を残すときの名前の印（self-check が探す）
OTHER_VERSION_MARK = "other-version"


class TaskResult(Enum):
    """タスクの結果。値は保存のときの文字。"""

    DONE = "done"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


class DayRecordView(Protocol):
    """判定の部品（schedule）が記録を読む口。RecordBook と NullRecordBook が形で満たす。"""

    def outcome(self, day: date, task_id: str) -> TaskResult | None:
        """そのタスクの日の結果。なければ None。"""
        ...

    def ended_at(self, day: date, task_id: str) -> datetime | None:
        """そのタスクの日に終わった時刻。なければ None。続きのタスクの窓に使う。"""
        ...

    def notice_said(self, day: date, task_id: str) -> bool:
        """そのタスクの日の予告を、もう言ったかどうか。"""
        ...


@dataclass
class _TaskEntry:
    result: TaskResult
    ended_at: datetime | None


@dataclass
class _Day:
    tasks: dict[str, _TaskEntry] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)


def _set_aside_other_version(store: StateStore, clock: Clock) -> None:
    """版の違う記録を、上書きしないように別の名前で残す（8.5）。"""
    path = store.directory / f"{_RECORDS}.json"
    stamp = clock.now().strftime("%Y%m%dT%H%M%S")
    target = store.directory / f"{_RECORDS}.{OTHER_VERSION_MARK}-{stamp}.json"
    number = 1
    while target.exists():
        number += 1
        target = store.directory / f"{_RECORDS}.{OTHER_VERSION_MARK}-{stamp}-{number}.json"
    try:
        os.replace(path, target)
    except OSError as exc:
        logger.warning("版の違う記録を残せませんでした: %s", type(exc).__name__)
        return
    logger.warning("版の違う記録を別の名前で残しました")


class RecordBook:
    """日ごとの記録と星の数。1 回の変更ごとに保存先に書く。"""

    def __init__(self, store: StateStore, keep_days: int, clock: Clock) -> None:
        self._store = store
        self._keep_days = keep_days
        self._clock = clock
        self._days: dict[date, _Day] = {}
        self._stars = 0
        self._recovered = False

    @classmethod
    def load(cls, store: StateStore, keep_days: int, clock: Clock) -> RecordBook:
        """保存先から読む。壊れた記録は基盤が退避し、星は控えから戻す（8.5）。"""
        book = cls(store, keep_days, clock)
        existed = (store.directory / f"{_RECORDS}.json").exists()
        try:
            records = store.load(_RECORDS)
        except OSError as exc:
            logger.warning("記録を読めませんでした: %s", type(exc).__name__)
            records = None
        days: dict[date, _Day] = {}
        records_stars = 0
        if records is not None and records.get("version") == _VERSION:
            days = _parse_days(records.get("days"))
            records_stars = _parse_stars(records.get("stars"))
        elif existed:
            # 壊れていた（基盤が退避した）か、版が違う（読まずに別の名前で残す）
            if records is not None:
                _set_aside_other_version(store, clock)
            logger.warning("記録を読めなかったので、控えの星から続けます")
            book._recovered = True
        try:
            backup = store.load(_STARS)
        except OSError as exc:
            logger.warning("星の控えを読めませんでした: %s", type(exc).__name__)
            backup = None
        backup_stars = 0
        if backup is not None and backup.get("version") == _VERSION:
            backup_stars = _parse_stars(backup.get("stars"))
        book._days = days
        book._stars = max(records_stars, backup_stars)
        return book

    @property
    def stars(self) -> int:
        """星の数（「できた」の数の合計）。"""
        return self._stars

    @property
    def recovered_from_corruption(self) -> bool:
        """読み込みのときに記録を読めず、控えから続けたかどうか。"""
        return self._recovered

    def outcome(self, day: date, task_id: str) -> TaskResult | None:
        entry = self._entry(day, task_id)
        return entry.result if entry is not None else None

    def ended_at(self, day: date, task_id: str) -> datetime | None:
        entry = self._entry(day, task_id)
        return entry.ended_at if entry is not None else None

    def notice_said(self, day: date, task_id: str) -> bool:
        record = self._days.get(day)
        return record is not None and task_id in record.notices

    def record_outcome(self, day: date, task_id: str, result: TaskResult) -> None:
        """結果を記録する。もう結果があれば上書きしない。できたなら星を 1 つ増やす。"""
        record = self._days.setdefault(day, _Day())
        if task_id in record.tasks:
            return
        record.tasks[task_id] = _TaskEntry(result, self._clock.now())
        if result is TaskResult.DONE:
            self._stars += 1
        self._save()

    def record_notice(self, day: date, task_id: str) -> None:
        """そのタスクの日の予告を言ったことを記録する（8.2、3.5）。"""
        record = self._days.setdefault(day, _Day())
        if task_id in record.notices:
            return
        record.notices.append(task_id)
        self._save()

    def prune(self, today: date) -> None:
        """keep_days より古い日の記録を消す。星は減らさない（8.6、8.7）。"""
        oldest = today - timedelta(days=self._keep_days)
        old = [day for day in self._days if day < oldest]
        if not old:
            return
        for day in old:
            del self._days[day]
        self._save()

    def _entry(self, day: date, task_id: str) -> _TaskEntry | None:
        record = self._days.get(day)
        return record.tasks.get(task_id) if record is not None else None

    def _save(self) -> None:
        # 記録 → 控えの順に書く。書けなければ覚えている内容のまま続け、次の書き込みでまとめて書く
        try:
            self._store.save(_RECORDS, self._records_data())
            self._store.save(_STARS, {"version": _VERSION, "stars": self._stars})
        except OSError as exc:
            logger.warning("記録を書けませんでした: %s", type(exc).__name__)

    def _records_data(self) -> dict[str, JsonValue]:
        days: dict[str, JsonValue] = {}
        for day in sorted(self._days):
            record = self._days[day]
            tasks: dict[str, JsonValue] = {}
            for task_id, entry in record.tasks.items():
                item: dict[str, JsonValue] = {"result": entry.result.value}
                if entry.ended_at is not None:
                    item["ended_at"] = entry.ended_at.isoformat()
                tasks[task_id] = item
            notices: list[JsonValue] = list(record.notices)
            days[day.isoformat()] = {"tasks": tasks, "notices": notices}
        return {"version": _VERSION, "stars": self._stars, "days": days}


class NullRecordBook:
    """試しの実行用。何も覚えず、何も書かない（10.2）。stars は本番の記録を読んだ値を返すだけ。"""

    def __init__(self, stars: int) -> None:
        self._stars = max(stars, 0)

    @classmethod
    def from_store(cls, store: StateStore) -> NullRecordBook:
        """本番の記録と控えから星の数だけを読む。壊れていても退避しない（何も書かない）。"""
        stars = 0
        for key in (_RECORDS, _STARS):
            data = _read_only(store, key)
            if data is not None and data.get("version") == _VERSION:
                stars = max(stars, _parse_stars(data.get("stars")))
        return cls(stars)

    @property
    def stars(self) -> int:
        return self._stars

    @property
    def recovered_from_corruption(self) -> bool:
        return False

    def outcome(self, day: date, task_id: str) -> TaskResult | None:
        return None

    def ended_at(self, day: date, task_id: str) -> datetime | None:
        return None

    def notice_said(self, day: date, task_id: str) -> bool:
        return False

    def record_outcome(self, day: date, task_id: str, result: TaskResult) -> None:
        """何もしない。"""

    def record_notice(self, day: date, task_id: str) -> None:
        """何もしない。"""

    def prune(self, today: date) -> None:
        """何もしない。"""


def _read_only(store: StateStore, key: str) -> dict[str, JsonValue] | None:
    """退避せずに読む。読めなければ None。"""
    try:
        raw = (store.directory / f"{key}.json").read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _parse_stars(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _parse_days(value: JsonValue) -> dict[date, _Day]:
    """日ごとの記録を読む。形のおかしいものは飛ばす。"""
    days: dict[date, _Day] = {}
    if not isinstance(value, dict):
        return days
    for key, item in value.items():
        try:
            day = date.fromisoformat(key)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        record = _Day()
        tasks = item.get("tasks")
        if isinstance(tasks, dict):
            for task_id, task in tasks.items():
                entry = _parse_task(task)
                if entry is not None:
                    record.tasks[task_id] = entry
        notices = item.get("notices")
        if isinstance(notices, list):
            for notice in notices:
                if isinstance(notice, str) and notice not in record.notices:
                    record.notices.append(notice)
        days[day] = record
    return days


def _parse_task(value: JsonValue) -> _TaskEntry | None:
    if not isinstance(value, dict):
        return None
    raw_result = value.get("result")
    if not isinstance(raw_result, str):
        return None
    try:
        result = TaskResult(raw_result)
    except ValueError:
        return None
    return _TaskEntry(result, _parse_time(value.get("ended_at")))


def _parse_time(value: JsonValue) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment
