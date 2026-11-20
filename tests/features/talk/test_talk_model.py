"""朝と夜のお話の型（5.8、12.1）。ポートを満たす偽物は talk_fakes で確かめる。"""

from dataclasses import FrozenInstanceError
from datetime import date

import pytest

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


def test_天気の種類() -> None:
    assert {c.name for c in Condition} == {"SUNNY", "CLOUDY", "RAIN", "SNOW", "THUNDER", "UNKNOWN"}


def test_注意報の種類() -> None:
    assert {k.name for k in AlertKind} == {
        "HEAVY_RAIN",
        "HEAVY_SNOW",
        "STRONG_WIND",
        "THUNDER",
        "ROUGH_SEA",
    }


def test_夜と朝の別() -> None:
    assert {k.name for k in TalkKind} == {"NIGHT", "MORNING"}


def test_日ごとの予報は変えられない() -> None:
    forecast = DayForecast(date(2026, 11, 21), Condition.SUNNY, 18, 9, None)
    with pytest.raises(FrozenInstanceError):
        forecast.t_max = 20  # type: ignore[misc]


def test_取れなかった結果は種類を持つ() -> None:
    assert WeatherUnavailable(WeatherProblem.STALE).kind is WeatherProblem.STALE
    assert WeatherUnavailable(WeatherProblem.INCOMPLETE).kind is WeatherProblem.INCOMPLETE
    assert AlertsUnavailable(AlertsProblem.STALE).kind is AlertsProblem.STALE
    assert FactUnavailable(FactProblem.REFUSED).kind is FactProblem.REFUSED


def test_用意したものは足りない部分を空にできる() -> None:
    prepared = Prepared(
        kind=TalkKind.NIGHT,
        weather_day=date(2026, 11, 21),
        forecast=None,
        alerts=None,
        fact=None,
        topic_hash=None,
    )
    assert prepared.forecast is None and prepared.fact is None
    alerts = frozenset({AlertKind.HEAVY_RAIN})
    with_alerts = Prepared(TalkKind.MORNING, date(2026, 11, 20), None, alerts, "ほんとう", "ab")
    assert with_alerts.alerts == alerts
