"""あいさつの判定と記録（3.1〜3.7、3.9、3.10、4.5）。

判定（GreetingPolicy）は純粋な部品で、時計を持たない。時刻と状態は呼ぶ側が渡す。
記録（GreetingRecords）は、人ごと（知らない人は UNKNOWN の 1 つの鍵）に、最後に見た時刻と
朝のあいさつをした日を持ち、基盤の保存先の `greetings.json` に書く（3.7、3.9）。
名前・呼び名・言葉・時刻は、ログに出さない（6.3）。ログには決まった文言と例外の型の名前だけを出す。
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Final

from osekkai.features.presence import texts

if TYPE_CHECKING:
    from osekkai.core.storage import JsonValue, StateStore
    from osekkai.features.presence.config import PresenceConfig

logger = logging.getLogger(__name__)

UNKNOWN: Final = "_unknown"  # 知らない人の記録の鍵（覚えた人の名前は a-z で始まるので重ならない）
STORE_KEY: Final = "greetings"
FORMAT_VERSION: Final = 1
SAVE_INTERVAL: Final = 60.0  # 見ている間の記録を保存する間（秒）
LOOK_SECONDS: Final = 1.0  # あいさつまでに見続ける時間（3.1）
WAIT_RECOGNITION_SECONDS: Final = 3.0  # 誰か分かるまで待つ時間（3.2）


class GreetingKind(Enum):
    MORNING = "morning"  # おはよう（3.3）
    OKAERI = "okaeri"  # おかえり（3.4）
    HELLO = "hello"  # やっほー（3.5）


@dataclass(frozen=True)
class GreetingDecision:
    kind: GreetingKind
    who: str | None  # None は知らない人


def record_key(who: str | None) -> str:
    """記録の鍵。知らない人は UNKNOWN（3.7）。"""
    return UNKNOWN if who is None else who


class _Unreadable(Exception):
    """保存の形がおかしい。理由は決まった文言だけ。"""


class GreetingRecords:
    """あいさつの記録（ctx.store の greetings）。

    最後に見た時刻は、見ている間ずっと手元で更新し、保存は SAVE_INTERVAL 秒に 1 回にする
    （カードへの書き込みを減らす）。あいさつしたときと消したときは、すぐ保存する。
    保存に失敗しても、見ている間とあいさつの記録は止めない（手元に残り、次に保存し直す）。
    """

    def __init__(
        self,
        store: StateStore,
        last_seen: dict[str, datetime] | None = None,
        morning: dict[str, date] | None = None,
    ) -> None:
        self._store = store
        self._last_seen: dict[str, datetime] = dict(last_seen or {})
        self._morning: dict[str, date] = dict(morning or {})
        self._saved_at: datetime | None = None

    @classmethod
    def load(cls, store: StateStore) -> GreetingRecords:
        """読む。なければ、版が違う・壊れている・形がおかしいときは空（次の保存で上書きする）。"""
        data = store.load(STORE_KEY)
        if data is None:
            return cls(store)
        try:
            last_seen, morning = _parse(data)
        except _Unreadable as e:
            logger.warning("あいさつの記録を読めないので空にします: %s", e)
            return cls(store)
        return cls(store, last_seen, morning)

    def last_seen(self, key: str) -> datetime | None:
        return self._last_seen.get(key)

    def morning_done(self, key: str, day: date) -> bool:
        return self._morning.get(key) == day

    def mark_seen(self, key: str, now: datetime) -> None:
        """見ている間の最後に見た時刻を更新する。保存は前の保存から SAVE_INTERVAL 秒たってから。"""
        self._last_seen[key] = now
        saved_at = self._saved_at
        if saved_at is None or not timedelta(0) <= now - saved_at < timedelta(
            seconds=SAVE_INTERVAL
        ):
            self._try_save(now)

    def mark_greeted(self, decision: GreetingDecision, now: datetime) -> None:
        """あいさつしたことを記録して、すぐ保存する。朝のあいさつなら、その日を覚える。"""
        key = record_key(decision.who)
        self._last_seen[key] = now
        if decision.kind is GreetingKind.MORNING:
            self._morning[key] = now.date()
        self._try_save(now)

    def forget(self, key: str | None) -> None:
        """1 人の記録を消す。None ならすべて消す（5.6、5.7）。保存に失敗したら例外を投げる。"""
        if key is None:
            self._last_seen.clear()
            self._morning.clear()
        else:
            self._last_seen.pop(key, None)
            self._morning.pop(key, None)
        self.save()

    def save(self) -> None:
        """保存先に原子的に書く。失敗したら例外を投げ、前の内容は残る。"""
        last_seen: dict[str, JsonValue] = {
            k: v.isoformat() for k, v in sorted(self._last_seen.items())
        }
        morning: dict[str, JsonValue] = {k: v.isoformat() for k, v in sorted(self._morning.items())}
        self._store.save(
            STORE_KEY, {"version": FORMAT_VERSION, "last_seen": last_seen, "morning": morning}
        )

    def _try_save(self, now: datetime) -> None:
        self._saved_at = now
        try:
            self.save()
        except OSError as e:
            logger.warning("あいさつの記録を保存できませんでした: %s", type(e).__name__)


def _parse(data: dict[str, JsonValue]) -> tuple[dict[str, datetime], dict[str, date]]:
    version = data.get("version")
    if isinstance(version, bool) or version != FORMAT_VERSION:
        raise _Unreadable("版が違います")
    raw_seen = data.get("last_seen")
    raw_morning = data.get("morning")
    if not isinstance(raw_seen, dict) or not isinstance(raw_morning, dict):
        raise _Unreadable("形がおかしいです")
    last_seen: dict[str, datetime] = {}
    for key, value in raw_seen.items():
        if not isinstance(value, str):
            raise _Unreadable("形がおかしいです")
        try:
            seen = datetime.fromisoformat(value)
        except ValueError:
            raise _Unreadable("時刻の形がおかしいです") from None
        if seen.tzinfo is None:
            raise _Unreadable("時刻の形がおかしいです")
        last_seen[key] = seen
    morning: dict[str, date] = {}
    for key, value in raw_morning.items():
        if not isinstance(value, str):
            raise _Unreadable("形がおかしいです")
        try:
            morning[key] = date.fromisoformat(value)
        except ValueError:
            raise _Unreadable("日の形がおかしいです") from None
    return last_seen, morning


def in_hours(now: time, hours: tuple[time, time]) -> bool:
    """時間帯の中か。始めを含み、終わりを含まない。始めが終わりより後なら日をまたぐ（3.10）。"""
    start, end = hours
    if start < end:
        return start <= now < end
    return now >= start or now < end


class GreetingPolicy:
    """いつ・誰に・何と言うかを決める。やっほーの言葉の選び方は差し替えられる。"""

    def __init__(
        self,
        config: PresenceConfig,
        choose: Callable[[Sequence[str]], str] = random.choice,
    ) -> None:
        self._config = config
        self._choose = choose

    def decide(
        self,
        now: datetime,
        *,
        synced: bool,
        who: str | None,
        looked_for: float,
        waiting_recognition: float,
        has_known_people: bool,
        records: GreetingRecords,
    ) -> GreetingDecision | None:
        """あいさつするなら、その種類と相手。しないなら None。

        - synced: 時刻が確かか（4.5）
        - who: 投票で決まった人。まだ決まらないか知らない人なら None
        - looked_for: 顔を見続けた秒数（3.1）
        - waiting_recognition: 誰か分からないまま待った秒数（3.2）
        - has_known_people: 覚えた顔があるか
        """
        config = self._config
        if not synced:
            return None
        if not in_hours(now.time(), config.greet_hours):
            return None
        if looked_for < LOOK_SECONDS:
            return None
        if has_known_people and who is None and waiting_recognition < WAIT_RECOGNITION_SECONDS:
            return None
        key = record_key(who)
        seen = records.last_seen(key)
        away = None if seen is None else now - seen
        if away is not None and away < timedelta(minutes=config.away_minutes):
            return None
        if now.time() < config.morning_end and not records.morning_done(key, now.date()):
            return GreetingDecision(GreetingKind.MORNING, who)
        if away is None or away >= timedelta(minutes=config.okaeri_minutes):
            return GreetingDecision(GreetingKind.OKAERI, who)
        return GreetingDecision(GreetingKind.HELLO, who)

    def phrase(self, decision: GreetingDecision, call_name: str | None) -> str:
        """あいさつの言葉。知らない人か呼び名がなければ、呼び名を入れない言葉（3.3〜3.6）。"""
        known = decision.who is not None and call_name is not None
        if decision.kind is GreetingKind.MORNING:
            template = texts.MORNING_KNOWN if known else texts.MORNING_UNKNOWN
        elif decision.kind is GreetingKind.OKAERI:
            template = texts.OKAERI_KNOWN if known else texts.OKAERI_UNKNOWN
        else:
            template = self._choose(texts.HELLO_KNOWN if known else texts.HELLO_UNKNOWN)
        if known and call_name is not None:
            return texts.fill(template, call_name)
        return template
