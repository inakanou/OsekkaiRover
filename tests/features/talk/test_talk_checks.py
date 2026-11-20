"""朝と夜のお話の self-check の項目（talk.setup、talk.weather）のテスト。

要件 1.7、1.8、10.4、11.1、11.2。

一時の保存先（XDG）と設定ファイルから基盤の ConfigLoader で報告を作り、基盤の SelfCheckRunner で
動かす。外へは問い合わせない: 天気と注意報のアダプタは checks の名前を偽物に差し替え、
念のため urllib の口も塞ぐ。鍵とライブラリの有無は、おしゃべりの口（api）を差し替える。
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import osekkai.features.conversation.api as conversation_api
from osekkai import __main__ as cli
from osekkai.adapters.fake.clock import FakeClock
from osekkai.adapters.fake.probes import FakeProbes
from osekkai.core.config import ConfigLoader, ConfigReport, FeatureConfig
from osekkai.core.paths import Paths
from osekkai.core.registry import FeatureRegistry
from osekkai.core.selfcheck import (
    REASON_SKIPPED_BUSY,
    CheckContext,
    CheckResult,
    CheckStatus,
    SelfCheckRunner,
    render_lines,
)
from osekkai.features.conversation.envfile import SetupProblem
from osekkai.features.talk import checks as checks_module
from osekkai.features.talk.checks import (
    FETCH_TIMEOUT_S,
    REASON_ALERTS_UNAVAILABLE,
    REASON_CONFIG_UNUSABLE,
    REASON_DISABLED,
    REASON_FACTS_ONLY,
    REASON_NO_KEY_NOTHING,
    REASON_NO_KEY_WEATHER_ONLY,
    REASON_NO_LOCATION,
    REASON_NO_ROUTINE,
    REASON_NOTHING_TO_TALK,
    REASON_WEATHER_ONLY,
    REASON_WEATHER_UNAVAILABLE,
    TALK_CHECKS,
    WEATHER_CHECK_TIMEOUT_S,
)
from osekkai.features.talk.config import TalkConfig
from osekkai.features.talk.feature import TalkFeature
from osekkai.features.talk.model import (
    AlertKind,
    AlertsProblem,
    AlertsUnavailable,
    Condition,
    DayForecast,
    WeatherProblem,
    WeatherUnavailable,
)

SETUP = "talk.setup"
WEATHER = "talk.weather"
# 出力とログに出てはいけない、目立つ偽の値
HIDDEN_NAME = "zzhiddenname"
HIDDEN_TOPIC = "zzhiddentopic"
LATITUDE = 35.6812
LONGITUDE = 139.7671
OFFICE = "130000"
CITY = "1311300"
TODAY = date(2026, 11, 20)
NOW = datetime(2026, 11, 20, 7, 30, tzinfo=timezone(timedelta(hours=9)))

LOCATION = f"  location: {{latitude: {LATITUDE}, longitude: {LONGITUDE}}}\n"
AREA = f'  warning_area: {{office: "{OFFICE}", city: "{CITY}"}}\n'
TOPICS = f"  topics: [{HIDDEN_TOPIC}]\n"
NAME = f"  call_name: {HIDDEN_NAME}\n"
ROUTINE_ON = "routine: {}\n"
ROUTINE_OFF = "routine: {enabled: false}\n"
FORECAST = DayForecast(day=TODAY, condition=Condition.SUNNY, t_max=17, t_min=-3, rain_percent=10)


class _Routine(FeatureConfig):
    """日課の節の代わり。checks は基本の `FeatureConfig` の `enabled` だけを見る。"""


@dataclass
class _Calls:
    """偽のアダプタへの呼び出しの記録と、返す答え。"""

    weather_result: Any = FORECAST
    alerts_result: Any = frozenset({AlertKind.HEAVY_RAIN})
    weather_made: list[tuple[float, float]] = field(default_factory=list)
    daily: list[tuple[date, float]] = field(default_factory=list)
    alerts_made: list[tuple[str, str]] = field(default_factory=list)
    alerts_now: list[datetime] = field(default_factory=list)
    active: list[float] = field(default_factory=list)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """外へ問い合わせたらテストを落とす。"""

    def _blocked(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("外へ問い合わせました")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    record = _Calls()

    class _Weather:
        def __init__(self, latitude: float, longitude: float) -> None:
            record.weather_made.append((latitude, longitude))

        def daily(self, day: date, timeout_s: float) -> Any:
            record.daily.append((day, timeout_s))
            return record.weather_result

    class _Alerts:
        def __init__(self, office: str, city: str, now: Any) -> None:
            record.alerts_made.append((office, city))
            self._now = now

        def active(self, timeout_s: float) -> Any:
            record.alerts_now.append(self._now())
            record.active.append(timeout_s)
            return record.alerts_result

    monkeypatch.setattr(checks_module, "OpenMeteoWeather", _Weather)
    monkeypatch.setattr(checks_module, "JmaAlerts", _Alerts)
    monkeypatch.setattr(checks_module, "make_clock", lambda: FakeClock(NOW))
    return record


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """鍵とライブラリの口を差し替える。中の値を変えると答えが変わる。"""
    state: dict[str, Any] = {"library": True, "problem": None}
    monkeypatch.setattr(conversation_api, "library_available", lambda: state["library"])
    monkeypatch.setattr(conversation_api, "setup_problem", lambda _paths: state["problem"])
    return state


@pytest.fixture
def paths(xdg_dirs: dict[str, Path]) -> Paths:
    return Paths()


def _report(paths: Paths, text: str) -> ConfigReport:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")
    models: dict[str, type[FeatureConfig]] = {"talk": TalkConfig, "routine": _Routine}
    return ConfigLoader(paths, models).load(create_missing=False)


def _talk(*lines: str, enabled: bool = True, routine: str = ROUTINE_ON) -> str:
    return f"talk:\n  enabled: {str(enabled).lower()}\n{''.join(lines)}{routine}"


def _run_all(paths: Paths, text: str, *, hardware_allowed: bool = True) -> dict[str, CheckResult]:
    ctx = CheckContext(
        paths=paths, report=_report(paths, text), service=None, hardware_allowed=hardware_allowed
    )
    return dict(SelfCheckRunner().run(TALK_CHECKS, ctx))


def _setup(paths: Paths, text: str) -> CheckResult:
    return _run_all(paths, text)[SETUP]


def _weather(paths: Paths, text: str) -> CheckResult:
    return _run_all(paths, text)[WEATHER]


# --- 項目の申告 ---


def test_機能の申告に_2_つの項目を付ける() -> None:
    assert TalkFeature.spec.checks == TALK_CHECKS
    assert [check.name for check in TALK_CHECKS] == [SETUP, WEATHER]
    setup, weather = TALK_CHECKS
    assert setup.hardware is False
    assert weather.hardware is True  # ロックを取れないときと --no-hardware では動かない
    assert weather.timeout_s == WEATHER_CHECK_TIMEOUT_S == 25.0
    # 予報と注意報を順に取っても、項目の時間に収まる
    assert FETCH_TIMEOUT_S * 2 < WEATHER_CHECK_TIMEOUT_S


# --- talk.setup ---


@pytest.mark.usefixtures("key", "calls")
class TestSetup:
    def test_無効なら飛ばす(self, paths: Paths) -> None:
        result = _setup(paths, _talk(LOCATION, TOPICS, enabled=False))
        assert result == CheckResult(CheckStatus.SKIPPED, REASON_DISABLED)

    def test_設定を使えなければ飛ばす(self, paths: Paths) -> None:
        result = _setup(paths, _talk(LOCATION, "  rain_percent: 0\n"))
        assert result == CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)

    def test_節がなければ既定の無効で飛ばす(self, paths: Paths) -> None:
        result = _setup(paths, ROUTINE_ON)
        assert result == CheckResult(CheckStatus.SKIPPED, REASON_DISABLED)

    def test_現在地も好きなものもなければ話すことがない(self, paths: Paths) -> None:
        assert _setup(paths, _talk()) == CheckResult(CheckStatus.OK, REASON_NOTHING_TO_TALK)

    def test_現在地だけなら天気だけ(self, paths: Paths) -> None:
        assert _setup(paths, _talk(LOCATION, AREA)) == CheckResult(
            CheckStatus.OK, REASON_WEATHER_ONLY
        )

    def test_好きなものだけなら豆知識だけ(self, paths: Paths) -> None:
        assert _setup(paths, _talk(TOPICS)) == CheckResult(CheckStatus.OK, REASON_FACTS_ONLY)

    def test_どちらもあり鍵も使えれば理由なしのよい(self, paths: Paths) -> None:
        assert _setup(paths, _talk(LOCATION, TOPICS)) == CheckResult(CheckStatus.OK)

    @pytest.mark.parametrize("problem", list(SetupProblem))
    def test_鍵が使えなければ天気だけ(
        self, paths: Paths, key: dict[str, Any], problem: SetupProblem
    ) -> None:
        key["problem"] = problem
        assert _setup(paths, _talk(LOCATION, TOPICS)) == CheckResult(
            CheckStatus.OK, REASON_NO_KEY_WEATHER_ONLY
        )

    def test_ライブラリがなければ天気だけ(self, paths: Paths, key: dict[str, Any]) -> None:
        key["library"] = False
        assert _setup(paths, _talk(LOCATION, TOPICS)) == CheckResult(
            CheckStatus.OK, REASON_NO_KEY_WEATHER_ONLY
        )

    def test_好きなものだけで鍵がなければ決まった言葉だけ(
        self, paths: Paths, key: dict[str, Any]
    ) -> None:
        key["problem"] = SetupProblem.NO_FILE
        assert _setup(paths, _talk(TOPICS)) == CheckResult(CheckStatus.OK, REASON_NO_KEY_NOTHING)
        # 決まった短い言葉は話すので「話すことがない」とは言わない（7.6、10.4）
        assert REASON_NO_KEY_NOTHING == "鍵がないので決まった言葉だけ"

    def test_現在地だけなら鍵を見ない(self, paths: Paths, key: dict[str, Any]) -> None:
        key["problem"] = SetupProblem.NO_FILE
        key["library"] = False
        assert _setup(paths, _talk(LOCATION)) == CheckResult(CheckStatus.OK, REASON_WEATHER_ONLY)

    def test_日課が無効なら話さない(self, paths: Paths) -> None:
        result = _setup(paths, _talk(LOCATION, TOPICS, routine=ROUTINE_OFF))
        assert result == CheckResult(CheckStatus.OK, REASON_NO_ROUTINE)

    def test_日課の設定を使えなければ話さない(self, paths: Paths) -> None:
        result = _setup(paths, _talk(LOCATION, TOPICS, routine="routine: {enabled: [1]}\n"))
        assert result == CheckResult(CheckStatus.OK, REASON_NO_ROUTINE)

    def test_日課がないことを話すことがないより先に知らせる(self, paths: Paths) -> None:
        assert _setup(paths, _talk(routine=ROUTINE_OFF)) == CheckResult(
            CheckStatus.OK, REASON_NO_ROUTINE
        )

    def test_日課の有無は基本の設定の有効だけを見る(
        self, paths: Paths, key: dict[str, Any]
    ) -> None:
        report = _report(paths, _talk(LOCATION, TOPICS, routine=""))
        features = dict(report.features)
        features["routine"] = FeatureConfig(enabled=True)
        ctx = CheckContext(
            paths=paths,
            report=ConfigReport(core=report.core, features=features),
            service=None,
            hardware_allowed=False,
        )
        assert dict(SelfCheckRunner().run(TALK_CHECKS, ctx))[SETUP] == CheckResult(CheckStatus.OK)

    def test_設定の項目は外へ問い合わせない(self, paths: Paths, calls: _Calls) -> None:
        ctx = CheckContext(
            paths=paths,
            report=_report(paths, _talk(LOCATION, AREA, TOPICS)),
            service=None,
            hardware_allowed=False,
        )
        results = dict(SelfCheckRunner().run(TALK_CHECKS, ctx))
        assert results[SETUP] == CheckResult(CheckStatus.OK)
        assert calls.weather_made == [] and calls.alerts_made == []


# --- talk.weather ---


@pytest.mark.usefixtures("key")
class TestWeather:
    def test_無効なら飛ばして問い合わせない(self, paths: Paths, calls: _Calls) -> None:
        result = _weather(paths, _talk(LOCATION, AREA, enabled=False))
        assert result == CheckResult(CheckStatus.SKIPPED, REASON_DISABLED)
        assert calls.weather_made == [] and calls.alerts_made == []

    def test_設定を使えなければ飛ばす(self, paths: Paths, calls: _Calls) -> None:
        result = _weather(paths, _talk(LOCATION, "  rain_percent: 0\n"))
        assert result == CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)
        assert calls.weather_made == []

    def test_現在地がなければ飛ばす(self, paths: Paths, calls: _Calls) -> None:
        result = _weather(paths, _talk(TOPICS))
        assert result == CheckResult(CheckStatus.SKIPPED, REASON_NO_LOCATION)
        assert calls.weather_made == [] and calls.alerts_made == []

    def test_予報と注意報を取れればよい(self, paths: Paths, calls: _Calls) -> None:
        assert _weather(paths, _talk(LOCATION, AREA)) == CheckResult(CheckStatus.OK)
        assert calls.weather_made == [(LATITUDE, LONGITUDE)]
        # 今日は基盤の時計から（datetime.now() を直に呼ばない）
        assert calls.daily == [(TODAY, FETCH_TIMEOUT_S)]
        assert calls.alerts_made == [(OFFICE, CITY)]
        assert calls.alerts_now == [NOW]
        assert calls.active == [FETCH_TIMEOUT_S]

    def test_注意報の地域がなければ注意報を取らない(self, paths: Paths, calls: _Calls) -> None:
        assert _weather(paths, _talk(LOCATION)) == CheckResult(CheckStatus.OK)
        assert len(calls.daily) == 1
        assert calls.alerts_made == []

    @pytest.mark.parametrize("problem", list(WeatherProblem))
    def test_予報を取れなければよいに理由を添える(
        self, paths: Paths, calls: _Calls, problem: WeatherProblem
    ) -> None:
        calls.weather_result = WeatherUnavailable(problem)
        assert _weather(paths, _talk(LOCATION, AREA)) == CheckResult(
            CheckStatus.OK, REASON_WEATHER_UNAVAILABLE
        )
        assert len(calls.active) == 1  # 予報を取れなくても注意報は確かめる

    @pytest.mark.parametrize("problem", list(AlertsProblem))
    def test_注意報を取れなければよいに理由を添える(
        self, paths: Paths, calls: _Calls, problem: AlertsProblem
    ) -> None:
        calls.alerts_result = AlertsUnavailable(problem)
        assert _weather(paths, _talk(LOCATION, AREA)) == CheckResult(
            CheckStatus.OK, REASON_ALERTS_UNAVAILABLE
        )

    def test_どちらも取れなければ両方の理由を足す(self, paths: Paths, calls: _Calls) -> None:
        calls.weather_result = WeatherUnavailable(WeatherProblem.TIMEOUT)
        calls.alerts_result = AlertsUnavailable(AlertsProblem.TIMEOUT)
        assert _weather(paths, _talk(LOCATION, AREA)) == CheckResult(
            CheckStatus.OK, f"{REASON_WEATHER_UNAVAILABLE}、{REASON_ALERTS_UNAVAILABLE}"
        )

    def test_注意報がなくてもよい(self, paths: Paths, calls: _Calls) -> None:
        calls.alerts_result = frozenset()
        assert _weather(paths, _talk(LOCATION, AREA)) == CheckResult(CheckStatus.OK)

    def test_ハードを触れないときは基盤が動かさない(self, paths: Paths, calls: _Calls) -> None:
        results = _run_all(paths, _talk(LOCATION, AREA), hardware_allowed=False)
        assert results[WEATHER] == CheckResult(CheckStatus.SKIPPED, REASON_SKIPPED_BUSY)
        assert calls.weather_made == [] and calls.alerts_made == []


# --- だめにしない・値を出さない（11.2、1.8） ---


@pytest.mark.parametrize(
    "text",
    [
        _talk(NAME, LOCATION, AREA, TOPICS),
        _talk(NAME, LOCATION, AREA, TOPICS, routine=ROUTINE_OFF),
        _talk(NAME, TOPICS),
        _talk(NAME, LOCATION, AREA),
        _talk(NAME, LOCATION, AREA, TOPICS, enabled=False),
    ],
)
@pytest.mark.parametrize("weather_ok", [True, False])
@pytest.mark.parametrize("key_ok", [True, False])
def test_だめにせず_表示とログに値を出さない(
    paths: Paths,
    calls: _Calls,
    key: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    text: str,
    weather_ok: bool,
    key_ok: bool,
) -> None:
    caplog.set_level(logging.DEBUG)
    if not weather_ok:
        calls.weather_result = WeatherUnavailable(WeatherProblem.BAD_RESPONSE)
        calls.alerts_result = AlertsUnavailable(AlertsProblem.BAD_RESPONSE)
    if not key_ok:
        key["problem"] = SetupProblem.NOT_WRITTEN
    results = _run_all(paths, text)
    assert all(result.status is not CheckStatus.FAILED for result in results.values())
    shown = "\n".join(render_lines(list(results.items()))) + caplog.text
    for value in (HIDDEN_NAME, HIDDEN_TOPIC, "35.6", "139.7", OFFICE, CITY, "17", "-3"):
        assert value not in shown


# --- 起動の口から（--no-hardware で天気の項目を飛ばす） ---


@pytest.fixture
def talk_only(xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, calls: _Calls) -> Path:
    """お話だけを登録し、有効で現在地のある設定を置く。"""
    probes = FakeProbes()
    monkeypatch.setattr(cli, "_build_probes", lambda fake_hardware: probes)
    monkeypatch.setattr(cli, "FeatureRegistry", lambda: FeatureRegistry([TalkFeature]))
    config = Paths().config_file
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(_talk(LOCATION, AREA, routine=""), encoding="utf-8")
    return config


def _rows(capsys: pytest.CaptureFixture[str]) -> dict[str, dict[str, Any]]:
    data = json.loads(capsys.readouterr().out)
    return {row["name"]: row for row in data["results"]}


def test_ハードなしなら天気の項目を飛ばす(
    talk_only: Path, calls: _Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["--self-check", "--fake-hardware", "--no-hardware", "--json"])
    rows = _rows(capsys)
    assert rows[WEATHER] == {"name": WEATHER, "status": "skipped", "reason": REASON_SKIPPED_BUSY}
    assert rows[SETUP]["status"] != "failed"
    assert calls.weather_made == [] and calls.alerts_made == []


def test_手で実行すれば天気を確かめる(
    talk_only: Path, calls: _Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["--self-check", "--fake-hardware", "--json"])
    rows = _rows(capsys)
    assert rows[WEATHER] == {"name": WEATHER, "status": "ok", "reason": ""}
    assert len(calls.daily) == 1 and len(calls.active) == 1
