"""前もっての用意（Preparer）と会話の AI の豆知識の出どころ（ChatFactSource）を確かめる。

要件 1.5、1.6、5.10、5.11、6.6、6.7、6.8、7.4、7.5、10.1、10.2、10.4。
偽の天気・偽の注意報・偽の会話の AI の遅れ（`HOLD_S` と `release()`）と失敗を組み合わせる。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Sequence
from datetime import date

import pytest
from talk_fakes import HOLD_S, FakeAlerts, FakeChatModel, FakeWeather, sunny_forecast

from osekkai.features.conversation.api import (
    ChatResult,
    Failed,
    FailureKind,
    Refused,
    Replied,
    Turn,
)
from osekkai.features.talk.config import Location, TalkConfig, WarningArea
from osekkai.features.talk.model import (
    AlertKind,
    AlertsProblem,
    AlertsUnavailable,
    Condition,
    DayForecast,
    FactProblem,
    FactUnavailable,
    Prepared,
    TalkKind,
    WeatherProblem,
    WeatherUnavailable,
)
from osekkai.features.talk.prepare import ChatFactSource, Preparer
from osekkai.features.talk.record import TalkOutcome
from osekkai.features.talk.topics import fact_request, topic_hash

TODAY = date(2026, 11, 20)
TOMORROW = date(2026, 11, 21)
SHORT_WAIT_S = 0.3  # 揃わないときに待つ長さ（テストを速くするため短くする）
LONG_WAIT_S = 5.0  # 揃うときに待つ上限（揃えばすぐ返る）
HIDDEN_FACT = "HIDDEN_FACT ぱんだは たけを たべるよ。"
HIDDEN_TOPIC = "HIDDEN_TOPIC"


class StubRecord:
    """直前の好きなものだけを持つ記録。"""

    def __init__(self, last_topic: str | None = None, *, broken: bool = False) -> None:
        self._last_topic = last_topic
        self._broken = broken

    def done(self, day: date, kind: TalkKind) -> bool:
        return False

    def mark(self, day: date, kind: TalkKind, outcome: TalkOutcome) -> None:
        return None

    @property
    def last_topic(self) -> str | None:
        if self._broken:
            raise OSError("broken")
        return self._last_topic

    def set_last_topic(self, topic_hash: str) -> None:
        self._last_topic = topic_hash


class FakeFacts:
    """偽の豆知識の出どころ（`FactSource`）。`delay_s` だけ止まり、`release()` で早めに放す。"""

    def __init__(
        self, result: str | FactUnavailable = HIDDEN_FACT, *, delay_s: float = 0.0
    ) -> None:
        self._result = result
        self._delay_s = delay_s
        self._released = threading.Event()
        self.calls: list[tuple[TalkKind, str, date, float]] = []

    def release(self) -> None:
        self._released.set()

    def fact(
        self, kind: TalkKind, topic: str, today: date, timeout_s: float
    ) -> str | FactUnavailable:
        self.calls.append((kind, topic, today, timeout_s))
        if self._delay_s > 0:
            self._released.wait(self._delay_s)
        return self._result


class RaisingWeather:
    def daily(self, day: date, timeout_s: float) -> DayForecast:
        raise RuntimeError("boom")


class RaisingChat:
    def reply(self, system: str, turns: Sequence[Turn], timeout_s: float) -> ChatResult:
        raise RuntimeError("boom")


class StepClock:
    """手で進める単調な時計。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_config(**overrides: object) -> TalkConfig:
    values: dict[str, object] = {
        "enabled": True,
        "location": Location(latitude=35.7, longitude=139.7),
        "warning_area": WarningArea(office="130000", city="1311300"),
        "topics": ["ぱんだ"],
        "fetch_timeout_seconds": 20.0,
    }
    values.update(overrides)
    return TalkConfig.model_validate(values)


def make_preparer(
    *,
    weather: FakeWeather | RaisingWeather | None = None,
    alerts: FakeAlerts | None = None,
    facts: FakeFacts | ChatFactSource | None = None,
    record: StubRecord | None = None,
    config: TalkConfig | None = None,
    monotonic: StepClock | None = None,
    seed: int = 0,
) -> Preparer:
    return Preparer(
        weather=weather,
        alerts=alerts,
        facts=facts,
        record=record or StubRecord(),
        config=config or make_config(),
        rng=random.Random(seed),
        monotonic=monotonic or time.monotonic,
    )


def timed_take(
    preparer: Preparer, kind: TalkKind, weather_day: date, wait_s: float
) -> tuple[Prepared, float]:
    started = time.monotonic()
    prepared = preparer.take(kind, weather_day, TODAY, wait_s=wait_s)
    return prepared, time.monotonic() - started


# --- ChatFactSource -----------------------------------------------------------


def test_出どころは豆知識の頼み方で問い合わせて整えた返事を返す() -> None:
    chat = FakeChatModel([Replied("ぱんだは たけを たべるよ😀。 ねるのが すき。 みっつめ。")])
    source = ChatFactSource(chat)
    result = source.fact(TalkKind.NIGHT, "ぱんだ", TODAY, 20.0)
    assert result == "ぱんだは たけを たべるよ。 ねるのが すき。"
    system, turns = fact_request(TalkKind.NIGHT, "ぱんだ", TODAY)
    assert chat.calls[0].system == system
    assert list(chat.calls[0].turns) == turns
    assert chat.calls[0].timeout_s == 20.0


def test_出どころは長い返事を決めた文字数までにする() -> None:
    chat = FakeChatModel([Replied("あ" * 300)])
    result = ChatFactSource(chat).fact(TalkKind.MORNING, "ぱんだ", TODAY, 20.0)
    assert isinstance(result, str)
    assert 0 < len(result) <= 120


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        (Replied("😀😀"), FactProblem.EMPTY),
        (Replied("   "), FactProblem.EMPTY),
        (Refused(), FactProblem.REFUSED),
        (Failed(FailureKind.TIMEOUT), FactProblem.TIMEOUT),
        (Failed(FailureKind.CONNECTION), FactProblem.FAILED),
        (Failed(FailureKind.BUSY), FactProblem.FAILED),
        (Failed(FailureKind.OTHER), FactProblem.FAILED),
    ],
)
def test_出どころは空と拒否と失敗を豆知識なしにする(
    reply: ChatResult, problem: FactProblem
) -> None:
    result = ChatFactSource(FakeChatModel([reply])).fact(TalkKind.NIGHT, "ぱんだ", TODAY, 20.0)
    assert result == FactUnavailable(problem)


def test_出どころは思いがけない例外を外へ出さない() -> None:
    result = ChatFactSource(RaisingChat()).fact(TalkKind.NIGHT, "ぱんだ", TODAY, 20.0)
    assert result == FactUnavailable(FactProblem.FAILED)


# --- Preparer: そろう -----------------------------------------------------------


def test_夜はあしたの天気と注意報と豆知識をそろえて返す() -> None:
    weather = FakeWeather()
    alerts = FakeAlerts(frozenset({AlertKind.THUNDER}))
    facts = FakeFacts()
    preparer = make_preparer(weather=weather, alerts=alerts, facts=facts)
    preparer.start(TalkKind.NIGHT, TOMORROW, TODAY)
    prepared, _ = timed_take(preparer, TalkKind.NIGHT, TOMORROW, LONG_WAIT_S)
    assert prepared.kind is TalkKind.NIGHT
    assert prepared.weather_day == TOMORROW
    assert prepared.forecast == sunny_forecast(TOMORROW)
    assert prepared.alerts == frozenset({AlertKind.THUNDER})
    assert prepared.fact == HIDDEN_FACT
    assert prepared.topic_hash == topic_hash("ぱんだ")
    assert weather.calls[0].day == TOMORROW
    assert weather.calls[0].timeout_s == 20.0
    assert alerts.calls[0].timeout_s == 20.0
    assert facts.calls == [(TalkKind.NIGHT, "ぱんだ", TODAY, 20.0)]


def test_設定の時間切れを各問い合わせに渡す() -> None:
    weather, alerts, facts = FakeWeather(), FakeAlerts(), FakeFacts()
    preparer = make_preparer(
        weather=weather, alerts=alerts, facts=facts, config=make_config(fetch_timeout_seconds=7.5)
    )
    preparer.take(TalkKind.MORNING, TODAY, TODAY, wait_s=LONG_WAIT_S)
    assert weather.calls[0].timeout_s == 7.5
    assert alerts.calls[0].timeout_s == 7.5
    assert facts.calls[0][3] == 7.5


def test_本物の出どころで豆知識をそろえる() -> None:
    chat = FakeChatModel([Replied("ぱんだは たけを たべるよ。")])
    preparer = make_preparer(weather=FakeWeather(), alerts=FakeAlerts(), facts=ChatFactSource(chat))
    prepared = preparer.take(TalkKind.MORNING, TODAY, TODAY, wait_s=LONG_WAIT_S)
    assert prepared.fact == "ぱんだは たけを たべるよ。"
    assert prepared.topic_hash == topic_hash("ぱんだ")


def test_直前と同じ好きなものを選ばずに出どころへ渡す() -> None:
    facts = FakeFacts()
    preparer = make_preparer(
        facts=facts,
        record=StubRecord(topic_hash("ぱんだ")),
        config=make_config(topics=["ぱんだ", "きりん"]),
    )
    prepared = preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert facts.calls[0][1] == "きりん"
    assert prepared.topic_hash == topic_hash("きりん")


# --- Preparer: 取らない ----------------------------------------------------------


def test_現在地がなければ天気と注意報を問い合わせない() -> None:
    facts = FakeFacts()
    preparer = make_preparer(weather=None, alerts=None, facts=facts)
    prepared = preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert prepared.forecast is None
    assert prepared.alerts is None
    assert prepared.fact == HIDDEN_FACT


def test_現在地がなければ注意報の口があっても問い合わせない() -> None:
    alerts = FakeAlerts()
    preparer = make_preparer(weather=None, alerts=alerts, facts=FakeFacts())
    prepared = preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert alerts.call_count == 0
    assert prepared.forecast is None
    assert prepared.alerts is None
    assert prepared.fact == HIDDEN_FACT


def test_注意報の地域がなければ注意報を取らずに天気を取る() -> None:
    weather = FakeWeather()
    preparer = make_preparer(weather=weather, alerts=None, facts=FakeFacts())
    prepared = preparer.take(TalkKind.MORNING, TODAY, TODAY, wait_s=LONG_WAIT_S)
    assert prepared.forecast == sunny_forecast(TODAY)
    assert prepared.alerts is None


def test_好きなものがなければ豆知識を取らない() -> None:
    facts = FakeFacts()
    preparer = make_preparer(weather=FakeWeather(), facts=facts, config=make_config(topics=[]))
    prepared = preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert facts.calls == []
    assert prepared.fact is None
    assert prepared.topic_hash is None
    assert prepared.forecast == sunny_forecast(TOMORROW)


def test_会話の_AI_がなければ豆知識を取らない() -> None:
    preparer = make_preparer(weather=FakeWeather(), facts=None)
    prepared = preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert prepared.fact is None
    assert prepared.topic_hash is None
    assert prepared.forecast is not None


def test_何もなければすぐに空の用意を返す() -> None:
    preparer = make_preparer(weather=None, alerts=None, facts=None)
    prepared, elapsed = timed_take(preparer, TalkKind.NIGHT, TOMORROW, LONG_WAIT_S)
    assert elapsed < 1.0
    assert prepared.forecast is None
    assert prepared.alerts is None
    assert prepared.fact is None


# --- Preparer: 失敗と遅れの組み合わせ ---------------------------------------------


def test_天気が取れなくても注意報と豆知識を返す() -> None:
    preparer = make_preparer(
        weather=FakeWeather(WeatherUnavailable(WeatherProblem.CONNECTION)),
        alerts=FakeAlerts(),
        facts=FakeFacts(),
    )
    prepared = preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert prepared.forecast is None
    assert prepared.fact == HIDDEN_FACT


def test_お話の日でない予報は使わない() -> None:
    preparer = make_preparer(weather=FakeWeather(sunny_forecast(TODAY)), facts=FakeFacts())
    prepared = preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert prepared.forecast is None


def test_注意報だけ取れなければ注意報だけなし() -> None:
    preparer = make_preparer(
        weather=FakeWeather(),
        alerts=FakeAlerts(AlertsUnavailable(AlertsProblem.STALE)),
        facts=FakeFacts(),
    )
    prepared = preparer.take(TalkKind.MORNING, TODAY, TODAY, wait_s=LONG_WAIT_S)
    assert prepared.forecast == sunny_forecast(TODAY)
    assert prepared.alerts is None
    assert prepared.fact == HIDDEN_FACT


@pytest.mark.parametrize("problem", list(FactProblem))
def test_豆知識が取れなければ天気だけで好きなものの_hash_もなし(problem: FactProblem) -> None:
    preparer = make_preparer(weather=FakeWeather(), facts=FakeFacts(FactUnavailable(problem)))
    prepared = preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert prepared.fact is None
    assert prepared.topic_hash is None
    assert prepared.forecast == sunny_forecast(TOMORROW)


@pytest.mark.parametrize("reply", [Refused(), Replied("😀"), Failed(FailureKind.TIMEOUT)])
def test_拒否と空の返事で豆知識なし(reply: ChatResult) -> None:
    preparer = make_preparer(weather=FakeWeather(), facts=ChatFactSource(FakeChatModel([reply])))
    prepared = preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert prepared.fact is None
    assert prepared.topic_hash is None
    assert prepared.forecast is not None


def test_天気の口の例外を外へ出さない() -> None:
    preparer = make_preparer(weather=RaisingWeather(), alerts=FakeAlerts(), facts=FakeFacts())
    prepared = preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert prepared.forecast is None
    assert prepared.fact == HIDDEN_FACT


def test_記録が読めなくても好きなものを選んで豆知識を取る() -> None:
    facts = FakeFacts()
    preparer = make_preparer(facts=facts, record=StubRecord(broken=True))
    prepared = preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert prepared.fact == HIDDEN_FACT


def test_遅れた豆知識を待たずにそろった天気だけを返す() -> None:
    facts = FakeFacts(delay_s=HOLD_S)
    preparer = make_preparer(weather=FakeWeather(), alerts=FakeAlerts(), facts=facts)
    try:
        preparer.start(TalkKind.NIGHT, TOMORROW, TODAY)
        prepared, elapsed = timed_take(preparer, TalkKind.NIGHT, TOMORROW, SHORT_WAIT_S)
    finally:
        facts.release()
    assert SHORT_WAIT_S - 0.05 <= elapsed < SHORT_WAIT_S + 1.0
    assert prepared.forecast == sunny_forecast(TOMORROW)
    assert prepared.alerts == frozenset()
    assert prepared.fact is None
    assert prepared.topic_hash is None


def test_遅れた天気を待たずに豆知識だけを返す() -> None:
    weather = FakeWeather(delay_s=HOLD_S)
    alerts = FakeAlerts(delay_s=HOLD_S)
    preparer = make_preparer(weather=weather, alerts=alerts, facts=FakeFacts())
    try:
        prepared, elapsed = timed_take(preparer, TalkKind.MORNING, TODAY, SHORT_WAIT_S)
    finally:
        weather.release()
        alerts.release()
    assert elapsed < SHORT_WAIT_S + 1.0
    assert prepared.forecast is None
    assert prepared.alerts is None
    assert prepared.fact == HIDDEN_FACT


def test_待つ間にそろえばすぐ返す() -> None:
    weather = FakeWeather(delay_s=HOLD_S)
    preparer = make_preparer(weather=weather, facts=FakeFacts())
    preparer.start(TalkKind.NIGHT, TOMORROW, TODAY)
    assert weather.called.wait(1.0)
    weather.release()
    prepared, elapsed = timed_take(preparer, TalkKind.NIGHT, TOMORROW, LONG_WAIT_S)
    assert elapsed < 2.0
    assert prepared.forecast == sunny_forecast(TOMORROW)


def test_問い合わせの時間切れを過ぎていたら待たない() -> None:
    clock = StepClock()
    weather = FakeWeather(delay_s=HOLD_S)
    preparer = make_preparer(weather=weather, facts=FakeFacts(), monotonic=clock)
    try:
        preparer.start(TalkKind.NIGHT, TOMORROW, TODAY)
        clock.now = 21.0  # 時間切れ（20 秒）を過ぎた
        prepared, elapsed = timed_take(preparer, TalkKind.NIGHT, TOMORROW, LONG_WAIT_S)
    finally:
        weather.release()
    assert elapsed < 1.0
    assert prepared.forecast is None
    assert prepared.fact == HIDDEN_FACT


def test_時間切れの後に返ってきた結果は使わない() -> None:
    clock = StepClock()
    weather = FakeWeather(delay_s=HOLD_S)
    preparer = make_preparer(weather=weather, facts=FakeFacts(), monotonic=clock)
    preparer.start(TalkKind.NIGHT, TOMORROW, TODAY)
    assert weather.called.wait(1.0)
    clock.now = 25.0
    weather.release()
    time.sleep(0.2)  # 遅れた結果が戻るのを待つ
    prepared = preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert prepared.forecast is None


# --- Preparer: 二重に始めない・捨てる ------------------------------------------------


def test_同じ種類と日の用意は二重に始めない() -> None:
    weather, alerts, facts = FakeWeather(), FakeAlerts(), FakeFacts()
    preparer = make_preparer(weather=weather, alerts=alerts, facts=facts)
    preparer.start(TalkKind.NIGHT, TOMORROW, TODAY)
    preparer.start(TalkKind.NIGHT, TOMORROW, TODAY)
    preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert weather.call_count == 1
    assert alerts.call_count == 1
    assert len(facts.calls) == 1


def test_違う種類で始め直すと前の用意を捨てる() -> None:
    weather = FakeWeather()
    preparer = make_preparer(weather=weather, facts=FakeFacts())
    preparer.start(TalkKind.NIGHT, TOMORROW, TODAY)
    prepared = preparer.take(TalkKind.MORNING, TODAY, TODAY, wait_s=LONG_WAIT_S)
    assert prepared.kind is TalkKind.MORNING
    assert prepared.forecast == sunny_forecast(TODAY)
    assert weather.call_count == 2


def test_日付が変わったら前の用意を捨てる() -> None:
    weather = FakeWeather()
    preparer = make_preparer(weather=weather)
    preparer.start(TalkKind.MORNING, TODAY, TODAY)
    prepared = preparer.take(TalkKind.MORNING, TODAY, TOMORROW, wait_s=LONG_WAIT_S)
    assert weather.call_count == 2
    assert prepared.forecast == sunny_forecast(TODAY)


def test_返した後は中身が残らない() -> None:
    weather, facts = FakeWeather(), FakeFacts()
    preparer = make_preparer(weather=weather, facts=facts)
    preparer.start(TalkKind.NIGHT, TOMORROW, TODAY)
    first = preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert first.fact == HIDDEN_FACT
    assert HIDDEN_FACT not in repr(vars(preparer))
    # もう一度聞くと、取っておいたものでなく新しく問い合わせる
    preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert weather.call_count == 2
    assert len(facts.calls) == 2


def test_捨てると次は新しく問い合わせる() -> None:
    weather = FakeWeather()
    preparer = make_preparer(weather=weather)
    preparer.start(TalkKind.NIGHT, TOMORROW, TODAY)
    assert weather.called.wait(1.0)
    preparer.discard()
    preparer.start(TalkKind.NIGHT, TOMORROW, TODAY)
    preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    assert weather.call_count == 2


def test_ログに好きなものと豆知識と天気の値を出さない(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    hidden_forecast = DayForecast(TOMORROW, Condition.SUNNY, 87, 65, 43)
    preparer = make_preparer(
        weather=FakeWeather(hidden_forecast),
        alerts=FakeAlerts(AlertsUnavailable(AlertsProblem.CONNECTION)),
        facts=FakeFacts(FactUnavailable(FactProblem.REFUSED)),
        config=make_config(topics=[HIDDEN_TOPIC]),
    )
    preparer.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    preparer2 = make_preparer(weather=FakeWeather(), facts=FakeFacts())
    preparer2.take(TalkKind.NIGHT, TOMORROW, TODAY, wait_s=LONG_WAIT_S)
    for hidden in (HIDDEN_TOPIC, "HIDDEN_FACT", "87", "65", "43", "35.7", "139.7", "1311300"):
        assert all(hidden not in record.getMessage() for record in caplog.records)
