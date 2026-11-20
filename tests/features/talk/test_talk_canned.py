"""PC 用の偽の天気・偽の注意報・偽の豆知識（12.1、12.2）。"""

import ast
from datetime import date
from pathlib import Path

from osekkai.features.talk.adapters import canned
from osekkai.features.talk.adapters.canned import CannedAlerts, CannedFacts, CannedWeather
from osekkai.features.talk.model import (
    AlertPort,
    Condition,
    DayForecast,
    FactSource,
    TalkKind,
    WeatherPort,
)

TODAY = date(2026, 11, 20)


def test_偽の天気は口の型を満たし決まった予報を返す() -> None:
    # mypy が型を確かめる
    weather: WeatherPort = CannedWeather()
    assert weather.daily(TODAY, 5.0) == DayForecast(TODAY, Condition.SUNNY, 18, 9, 10)


def test_偽の天気は頼まれた日の予報を返す() -> None:
    weather = CannedWeather()
    for day in (date(2026, 1, 1), date(2026, 11, 21), date(2027, 12, 31)):
        assert weather.daily(day, 0.1) == DayForecast(day, Condition.SUNNY, 18, 9, 10)


def test_偽の注意報は口の型を満たし何も出ていない() -> None:
    alerts: AlertPort = CannedAlerts()
    got = alerts.active(5.0)
    assert got == frozenset()
    assert isinstance(got, frozenset)


def test_偽の豆知識は口の型を満たし好きなものの言葉を入れた1文を返す() -> None:
    facts: FactSource = CannedFacts()
    got = facts.fact(TalkKind.NIGHT, "きょうりゅう", TODAY, 5.0)
    assert isinstance(got, str)
    assert "きょうりゅう" in got
    assert "。" not in got  # 1 文だけ


def test_偽の豆知識は好きなものごとに違い日やお話の種類では変わらない() -> None:
    facts = CannedFacts()
    a = facts.fact(TalkKind.NIGHT, "でんしゃ", TODAY, 5.0)
    b = facts.fact(TalkKind.MORNING, "でんしゃ", date(2026, 12, 1), 0.1)
    c = facts.fact(TalkKind.NIGHT, "うちゅう", TODAY, 5.0)
    assert a == b
    assert a != c
    assert isinstance(c, str) and "うちゅう" in c


def test_偽の豆知識は波かっこのある言葉でも壊れない() -> None:
    got = CannedFacts().fact(TalkKind.MORNING, "{name}と{0}", TODAY, 5.0)
    assert isinstance(got, str)
    assert "{name}と{0}" in got


def test_外への問い合わせの道具を読み込まない() -> None:
    tree = ast.parse(Path(canned.__file__).read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            # 相対の読み込み（..model）は level を付けて名前にする
            names.append("." * node.level + (node.module or ""))
    allowed = ("__future__", "datetime", "typing", "..model")
    assert names, "読み込みが見つからない"
    assert all(n.startswith(allowed) for n in names), names
    assert not any(n.split(".")[0] in ("urllib", "http", "socket") for n in names)
