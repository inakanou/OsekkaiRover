"""前もっての用意と、会話の AI の豆知識の出どころ。

要件 1.5、1.6、5.10、5.11、6.6〜6.8、7.4、7.5、10.1、10.2、10.4。

`Preparer` は天気・注意報・豆知識を別々のデーモンのスレッドで並べて取り、話す時に
決めた秒数だけ待って、そろった分だけを返す。返した後は中身を手放す（6.8）。
`ChatFactSource` は会話の AI の口を包み、豆知識の頼み方で問い合わせて返事を整える。

どれも例外を外へ出さない（10.1）。ログには決まった文言と種類だけを出し、
好きなもの・豆知識・天気の値は出さない（1.8、9.4）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from random import Random
from typing import Final

from osekkai.features.conversation.api import (
    ChatModelPort,
    Failed,
    FailureKind,
    Refused,
    Replied,
    shape_reply,
)
from osekkai.features.talk.config import TalkConfig
from osekkai.features.talk.model import (
    AlertKind,
    AlertPort,
    AlertsUnavailable,
    DayForecast,
    FactProblem,
    FactResult,
    FactSource,
    FactUnavailable,
    Prepared,
    TalkKind,
    WeatherPort,
    WeatherUnavailable,
)
from osekkai.features.talk.record import TalkRecordPort
from osekkai.features.talk.topics import choose_topic, fact_request, topic_hash

logger = logging.getLogger(__name__)

DEFAULT_WAIT_S: Final = 5.0  # 話す時に用意を待つ長さ（7.4）

_PART_WEATHER: Final = "weather"
_PART_ALERTS: Final = "alerts"
_PART_FACT: Final = "fact"


class ChatFactSource:
    """会話の AI の口を包んだ豆知識の出どころ（`FactSource`）。"""

    def __init__(self, chat: ChatModelPort) -> None:
        self._chat = chat

    def fact(self, kind: TalkKind, topic: str, today: date, timeout_s: float) -> FactResult:
        """豆知識を 1 つ頼む。空・拒否・失敗・時間切れは取れなかった結果にする（6.7）。"""
        system, turns = fact_request(kind, topic, today)
        try:
            result = self._chat.reply(system, turns, timeout_s)
        except Exception as exc:  # 口の思いがけない例外も外へ出さない（10.1）
            logger.warning("豆知識の問い合わせで思いがけない失敗: %s", type(exc).__name__)
            return FactUnavailable(FactProblem.FAILED)
        if isinstance(result, Replied):
            shaped = shape_reply(result.text)  # 2 文・120 文字まで、記号なし（6.6）
            return shaped if shaped else FactUnavailable(FactProblem.EMPTY)
        if isinstance(result, Refused):
            return FactUnavailable(FactProblem.REFUSED)
        if isinstance(result, Failed) and result.kind is FailureKind.TIMEOUT:
            return FactUnavailable(FactProblem.TIMEOUT)
        return FactUnavailable(FactProblem.FAILED)


@dataclass
class _Session:
    """1 回分の用意。スレッドはこの入れ物にだけ書く（捨てた後に戻っても用意に混ざらない）。"""

    kind: TalkKind
    weather_day: date
    today: date
    started_at: float
    timeout_s: float
    pending: set[str] = field(default_factory=set)
    forecast: DayForecast | None = None
    alerts: frozenset[AlertKind] | None = None
    fact: str | None = None
    topic_hash: str | None = None


class Preparer:
    """天気・注意報・豆知識を、話す前に並べて取っておく。"""

    def __init__(
        self,
        *,
        weather: WeatherPort | None,
        alerts: AlertPort | None,
        facts: FactSource | None,
        record: TalkRecordPort,
        config: TalkConfig,
        rng: Random,
        monotonic: Callable[[], float],
    ) -> None:
        # weather が None なら現在地がない（1.5）、alerts が None なら注意報の地域がない（5.11）、
        # facts が None なら鍵かライブラリがない（10.4）
        self._weather = weather
        self._alerts = alerts if weather is not None else None
        self._facts = facts
        self._record = record
        self._config = config
        self._rng = rng
        self._monotonic = monotonic
        self._cond = threading.Condition()
        self._session: _Session | None = None

    def start(self, kind: TalkKind, weather_day: date, today: date) -> None:
        """用意を始める。同じ種類と日で始めていれば何もしない。違えば前の用意を捨てる。"""
        try:
            self._start(kind, weather_day, today)
        except Exception as exc:  # 用意の失敗で話す流れを止めない（10.1）
            logger.warning("お話の用意を始められませんでした: %s", type(exc).__name__)

    def take(
        self, kind: TalkKind, weather_day: date, today: date, wait_s: float = DEFAULT_WAIT_S
    ) -> Prepared:
        """最大 `wait_s` 待って、そろった分を返し、中身を手放す（7.4、7.5、6.8）。"""
        try:
            session = self._start(kind, weather_day, today)
            self._wait(session, wait_s)
            with self._cond:
                prepared = Prepared(
                    kind=kind,
                    weather_day=weather_day,
                    forecast=session.forecast,
                    alerts=session.alerts,
                    fact=session.fact,
                    topic_hash=session.topic_hash if session.fact is not None else None,
                )
                missing = sorted(session.pending)
                self._session = None
        except Exception as exc:  # 何があっても話せる形（何もない用意）で返す（10.1）
            logger.warning("お話の用意を受け取れませんでした: %s", type(exc).__name__)
            self.discard()
            return Prepared(kind, weather_day, None, None, None, None)
        if missing:
            logger.info("お話の用意が間に合いませんでした: %s", ",".join(missing))
        return prepared

    def discard(self) -> None:
        """用意を捨てる。まだ動いているスレッドの結果は、もう用意に入らない。"""
        with self._cond:
            self._session = None
            self._cond.notify_all()

    # --- 中 -------------------------------------------------------------------

    def _start(self, kind: TalkKind, weather_day: date, today: date) -> _Session:
        with self._cond:
            current = self._session
            if current is not None and (current.kind, current.weather_day, current.today) == (
                kind,
                weather_day,
                today,
            ):
                return current
            # 違う種類・日、または日付が変わった用意は捨てる
            session = _Session(
                kind=kind,
                weather_day=weather_day,
                today=today,
                started_at=self._monotonic(),
                timeout_s=self._config.fetch_timeout_seconds,
            )
            jobs: list[tuple[str, Callable[[], None]]] = []
            weather = self._weather
            alerts = self._alerts
            facts = self._facts
            if weather is not None:
                jobs.append((_PART_WEATHER, lambda: self._fetch_weather(session, weather)))
            if alerts is not None:
                jobs.append((_PART_ALERTS, lambda: self._fetch_alerts(session, alerts)))
            if facts is not None:
                topic = choose_topic(self._config.topics, self._last_topic(), self._rng)
                if topic is not None:
                    jobs.append((_PART_FACT, lambda: self._fetch_fact(session, facts, topic)))
            # 待つものをそろえてから見せる（同じ用意を待つ側が、空の待ちで先に返さないため）
            session.pending.update(name for name, _ in jobs)
            self._session = session
        for name, job in jobs:
            thread = threading.Thread(
                target=self._run, args=(session, name, job), name=f"talk-{name}", daemon=True
            )
            thread.start()
        return session

    def _last_topic(self) -> str | None:
        try:
            return self._record.last_topic
        except Exception as exc:  # 読めなくても選ぶ（直前を除けないだけ）
            logger.warning("直前の好きなものを読めませんでした: %s", type(exc).__name__)
            return None

    def _wait(self, session: _Session, wait_s: float) -> None:
        """そろうか、待つ長さか問い合わせの時間切れが来るまで待つ。"""
        deadline = min(self._monotonic() + wait_s, session.started_at + session.timeout_s)
        with self._cond:
            while session.pending and self._session is session:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return
                if not self._cond.wait(remaining):
                    # 待ち切った（差し替えた時計が進まなくても、ここで必ず止まる）
                    return

    def _run(self, session: _Session, name: str, job: Callable[[], None]) -> None:
        try:
            job()
        except Exception as exc:  # 口の思いがけない例外も外へ出さない（10.1）
            logger.warning("お話の用意で思いがけない失敗: %s %s", name, type(exc).__name__)
        finally:
            with self._cond:
                session.pending.discard(name)
                self._cond.notify_all()

    def _in_time(self, session: _Session) -> bool:
        """時間切れ（10.2）の後に戻った結果は使わない。"""
        return self._monotonic() - session.started_at <= session.timeout_s

    def _fetch_weather(self, session: _Session, weather: WeatherPort) -> None:
        result = weather.daily(session.weather_day, session.timeout_s)
        if isinstance(result, WeatherUnavailable):
            logger.info("お話の天気を取れませんでした: %s", result.kind.value)
            return
        if result.day != session.weather_day:  # お話の日のものでない（5.10）
            logger.info("お話の天気を取れませんでした: stale")
            return
        if not self._in_time(session):
            logger.info("お話の天気を取れませんでした: timeout")
            return
        with self._cond:
            session.forecast = result

    def _fetch_alerts(self, session: _Session, alerts: AlertPort) -> None:
        result = alerts.active(session.timeout_s)
        if isinstance(result, AlertsUnavailable):
            logger.info("お話の注意報を取れませんでした: %s", result.kind.value)
            return
        if not self._in_time(session):
            logger.info("お話の注意報を取れませんでした: timeout")
            return
        with self._cond:
            session.alerts = result

    def _fetch_fact(self, session: _Session, facts: FactSource, topic: str) -> None:
        result = facts.fact(session.kind, topic, session.today, session.timeout_s)
        if isinstance(result, FactUnavailable):
            logger.info("お話の豆知識を取れませんでした: %s", result.kind.value)
            return
        if not result:
            logger.info("お話の豆知識を取れませんでした: %s", FactProblem.EMPTY.value)
            return
        if not self._in_time(session):
            logger.info("お話の豆知識を取れませんでした: %s", FactProblem.TIMEOUT.value)
            return
        with self._cond:
            session.fact = result
            session.topic_hash = topic_hash(topic)
