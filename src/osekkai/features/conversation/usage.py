"""1 日の回数の記録（DailyUsage）。要件 5.3〜5.6、8.1。

機能の保存先の usage.json に、日付・回数・読めなかった日かを原子的に書く。
読めない記録は使いすぎの側に倒さず、その日を上限に達したとみなす（5.6）。
ログには記録の名前と理由の種類だけを出し、中身は出さない（8.1）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Final

from osekkai.core.clock import Clock
from osekkai.core.storage import JsonValue, StateStore

logger = logging.getLogger(__name__)

_KEY: Final = "usage"
_VERSION: Final = 1


@dataclass
class _Day:
    day: date
    count: int
    blocked: bool


class DailyUsage:
    """その日に話した返事の数。1 つ足すごとに保存先に書く。

    reached と add_one には、時刻が確かなときの日付だけを渡す（門で先に確かめる）。
    起動は時刻の同期より前のことが多いので、記録はその日付が初めて来たときに読む。
    古い日付を書かないため、load では読まない。
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store
        self._state: _Day | None = None

    @classmethod
    def load(cls, store: StateStore, clock: Clock) -> DailyUsage:
        """保存先の記録を使う口を作る。中を読むのは確かな日付が来たとき。

        clock は設計の口の形に合わせて受け取る。同期の前の日付を使わないため、ここでは読まない。
        """
        del clock
        return cls(store)

    def reached(self, today: date, limit: int) -> bool:
        """上限に達したか、読めなかった日か。保存の日付と違う日は 0 回とみなす（5.5）。"""
        state = self._ensure(today)
        if today != state.day:
            return limit <= 0
        return state.blocked or state.count >= limit

    def add_one(self, today: date) -> int:
        """1 つ足して保存し、その日の回数を返す。保存に失敗しても記憶の中で数える。"""
        state = self._ensure(today)
        if today != state.day:
            state.day = today
            state.count = 0
            state.blocked = False
        state.count += 1
        self._save(state)
        return state.count

    def _ensure(self, today: date) -> _Day:
        """初めてなら記録を読む。読めなければ today を読めなかった日として書く（5.6）。"""
        if self._state is None:
            self._state = self._read(today)
        return self._state

    def _read(self, today: date) -> _Day:
        try:
            # 壊れたファイルは基盤が退避して None を返すので、先にあるかを見る
            existed = (self._store.directory / f"{_KEY}.json").exists()
            data = self._store.load(_KEY)
        except OSError as exc:
            return self._unreadable(today, type(exc).__name__)
        if data is None:
            if existed:
                return self._unreadable(today, "壊れていた")
            return _Day(today, 0, False)
        if data.get("version") != _VERSION:
            return self._unreadable(today, "版が違う")
        parsed = _parse(data)
        if parsed is None:
            return self._unreadable(today, "形が違う")
        return parsed

    def _unreadable(self, today: date, reason: str) -> _Day:
        logger.warning("回数の記録を読めませんでした: %s (%s)", _KEY, reason)
        # 退避された後も次に 0 からにならないよう、必ず書いておく
        state = _Day(today, 0, True)
        self._save(state)
        return state

    def _save(self, state: _Day) -> None:
        data: dict[str, JsonValue] = {
            "version": _VERSION,
            "date": state.day.isoformat(),
            "count": state.count,
            "blocked": state.blocked,
        }
        try:
            self._store.save(_KEY, data)
        except OSError as exc:
            logger.warning("回数の記録を書けませんでした: %s (%s)", _KEY, type(exc).__name__)


def _parse(data: dict[str, JsonValue]) -> _Day | None:
    """形が正しければその日の記録、違えば None。"""
    raw_date = data.get("date")
    count = data.get("count")
    blocked = data.get("blocked")
    if not isinstance(raw_date, str):
        return None
    try:
        day = date.fromisoformat(raw_date)
    except ValueError:
        return None
    # bool は int の仲間なので先に外す
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    if not isinstance(blocked, bool):
        return None
    return _Day(day, count, blocked)
