"""1 日 1 回の記録（TalkRecord）と試し用の記録（NullTalkRecord）。要件 4.1〜4.3、6.2、11.4。

機能の保存先の record.json に、日付ごとの夜と朝の結果（話した・諦めた）と、直前の好きなものの
hash を原子的に書く。7 日より古い日は書くときに消す。好きなものの文字は残さない（6.2）。

読めない・壊れて退避された・形や版が違う記録は、話しすぎの側に倒さず、その日を夜も朝も
済んだ扱いにする（4.3）。「その日」は、読めないと分かった後に初めて問われた日付
（`done` か `mark` に渡された日）。起動は時刻の同期より前のことが多いので、日付は
呼ぶ側が確かな時刻から渡し、この記録は時計を持たない。その日を両方「諦めた」として
書き直すので、再起動してもその日は済んだままで、次の日付からは新しい記録で始まる。

ログには記録の名前と理由の種類だけを出し、中身は出さない。
機能のループのスレッドから使う前提だが、念のため錠で守る。
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date, timedelta
from enum import Enum
from typing import Final, Protocol

from osekkai.core.storage import JsonValue, StateStore
from osekkai.features.talk.model import TalkKind

logger = logging.getLogger(__name__)

_KEY: Final = "record"
_VERSION: Final = 1
_KEEP_DAYS: Final = 7  # これより古い日は書くときに消す
_HASH_PATTERN: Final = re.compile(r"[0-9a-f]{16}")


class TalkOutcome(Enum):
    """その日のその種類の結果。どちらも済んだ扱い。"""

    SPOKEN = "spoken"
    MISSED = "missed"


class TalkRecordPort(Protocol):
    """記録の口。本物（TalkRecord）と試し用（NullTalkRecord）が満たす。"""

    def done(self, day: date, kind: TalkKind) -> bool: ...
    def mark(self, day: date, kind: TalkKind, outcome: TalkOutcome) -> None: ...
    @property
    def last_topic(self) -> str | None: ...
    def set_last_topic(self, topic_hash: str) -> None: ...


_Days = dict[date, dict[TalkKind, TalkOutcome]]


class TalkRecord:
    """保存先に書く本物の記録。中を読むのは初めて使われたとき（作るだけでは読まない）。"""

    def __init__(self, store: StateStore) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._loaded = False
        self._days: _Days = {}
        self._last_topic: str | None = None
        # 読めなかったが、まだどの日を済んだ扱いにするか決まっていない
        self._blocked_pending = False
        self._latest: date | None = None

    def done(self, day: date, kind: TalkKind) -> bool:
        """その日のその種類が済んだか（話した・諦めた・読めなかった日）。"""
        with self._lock:
            self._ensure(day)
            return kind in self._days.get(day, {})

    def mark(self, day: date, kind: TalkKind, outcome: TalkOutcome) -> None:
        """結果を記録して保存する。話す前に呼ぶ。

        書けなければ OSError を上げ、記憶の中も前のままにする（呼んだ側は話さない）。
        """
        with self._lock:
            self._ensure(day)
            before = dict(self._days.get(day, {}))
            self._days.setdefault(day, {})[kind] = outcome
            try:
                self._save()
            except OSError as exc:
                self._days[day] = before
                if not before:
                    del self._days[day]
                logger.warning("お話の記録を書けませんでした: %s (%s)", _KEY, type(exc).__name__)
                raise

    @property
    def last_topic(self) -> str | None:
        """直前に選んだ好きなものの hash。なければ None。"""
        with self._lock:
            self._load()
            return self._last_topic

    def set_last_topic(self, topic_hash: str) -> None:
        """直前の好きなものの hash を記録する。好きなものの文字そのものは受け取らない。

        話した後に呼ぶので、書けなくてもログだけにして記憶の中では覚えておく。
        """
        if not _HASH_PATTERN.fullmatch(topic_hash):
            raise ValueError("好きなものの hash ではありません")
        with self._lock:
            self._load()
            self._last_topic = topic_hash
            try:
                self._save()
            except OSError as exc:
                logger.warning("お話の記録を書けませんでした: %s (%s)", _KEY, type(exc).__name__)

    def _ensure(self, day: date) -> None:
        """記録を読み、読めなかったなら day を夜も朝も済んだ日にして書き直す（4.3）。"""
        self._load()
        if self._latest is None or day > self._latest:
            self._latest = day
        if not self._blocked_pending:
            return
        self._blocked_pending = False
        self._days = {day: {kind: TalkOutcome.MISSED for kind in TalkKind}}
        self._last_topic = None
        try:
            self._save()
        except OSError as exc:
            # 書けなくても、記憶の中ではその日を済んだ扱いのままにする
            logger.warning("お話の記録を書けませんでした: %s (%s)", _KEY, type(exc).__name__)

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        reason = self._read()
        if reason is not None:
            logger.warning("お話の記録を読めませんでした: %s (%s)", _KEY, reason)
            self._days = {}
            self._last_topic = None
            self._blocked_pending = True

    def _read(self) -> str | None:
        """読んで中に入れる。読めなければ理由の種類を返す。"""
        try:
            # 壊れたファイルは基盤が退避して None を返すので、先にあるかを見る
            existed = (self._store.directory / f"{_KEY}.json").exists()
            data = self._store.load(_KEY)
        except OSError as exc:
            return type(exc).__name__
        if data is None:
            return "壊れていた" if existed else None
        if data.get("version") != _VERSION:
            return "版が違う"
        parsed = _parse(data)
        if parsed is None:
            return "形が違う"
        self._days, self._last_topic = parsed
        return None

    def _save(self) -> None:
        """古い日を消してから原子的に書く。失敗すれば OSError。"""
        if self._latest is not None:
            oldest = self._latest - timedelta(days=_KEEP_DAYS)
            self._days = {day: kinds for day, kinds in self._days.items() if day >= oldest}
        days: dict[str, JsonValue] = {
            day.isoformat(): {kind.value: outcome.value for kind, outcome in kinds.items()}
            for day, kinds in sorted(self._days.items())
        }
        data: dict[str, JsonValue] = {
            "version": _VERSION,
            "days": days,
            "last_topic": self._last_topic,
        }
        self._store.save(_KEY, data)


class NullTalkRecord:
    """試し用の記録。何も済んでおらず、書いても捨てる（11.4）。"""

    def done(self, day: date, kind: TalkKind) -> bool:
        del day, kind
        return False

    def mark(self, day: date, kind: TalkKind, outcome: TalkOutcome) -> None:
        del day, kind, outcome

    @property
    def last_topic(self) -> str | None:
        return None

    def set_last_topic(self, topic_hash: str) -> None:
        del topic_hash


def _parse(data: dict[str, JsonValue]) -> tuple[_Days, str | None] | None:
    """形が正しければ日ごとの結果と直前の好きなもの、違えば None。"""
    raw_days = data.get("days")
    if not isinstance(raw_days, dict) or "last_topic" not in data:
        return None
    last_topic = data["last_topic"]
    if last_topic is not None and not (
        isinstance(last_topic, str) and _HASH_PATTERN.fullmatch(last_topic)
    ):
        return None
    days: _Days = {}
    for raw_day, raw_kinds in raw_days.items():
        try:
            day = date.fromisoformat(raw_day)
        except ValueError:
            return None
        if not isinstance(raw_kinds, dict):
            return None
        kinds: dict[TalkKind, TalkOutcome] = {}
        for raw_kind, raw_outcome in raw_kinds.items():
            try:
                kind = TalkKind(raw_kind)
                outcome = TalkOutcome(raw_outcome)
            except ValueError:
                return None
            kinds[kind] = outcome
        days[day] = kinds
    return days, last_topic
