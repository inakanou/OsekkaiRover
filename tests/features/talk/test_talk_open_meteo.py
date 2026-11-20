"""日ごとの予報のアダプタ OpenMeteoWeather のテスト（5.1、5.2、5.10、9.2、9.4、10.1、10.2）。

外へは問い合わせない。`opener` に偽物を渡し、本物の `urllib.request.urlopen` は
このモジュールの間ずっと、呼ばれたらテストを失敗させるものに差し替える。
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from osekkai.features.talk.adapters import open_meteo
from osekkai.features.talk.adapters.open_meteo import OpenMeteoWeather, forecast_url
from osekkai.features.talk.model import (
    Condition,
    DayForecast,
    WeatherProblem,
    WeatherUnavailable,
)

FIXTURE = Path(__file__).parent / "fixtures" / "open_meteo_forecast.json"
TODAY = date(2026, 11, 20)
TOMORROW = date(2026, 11, 21)

# ログに出てはいけない座標（丸める前と後）。丸めると 35.7 と 139.6 になる
HIDDEN_LAT = 35.6543
HIDDEN_LON = 139.6123
HIDDEN_TEXTS = ("35.6543", "139.6123", "35.7", "139.6", "api.open-meteo.com", "latitude")
# 応答の中の値（ログに出てはいけない）
HIDDEN_REASON = "HIDDEN_REASON_TEXT"

BASE_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    "&timezone=Asia%2FTokyo&forecast_days=2"
)


@pytest.fixture(autouse=True)
def _no_real_urlopen(monkeypatch: pytest.MonkeyPatch) -> None:
    """本物の urlopen が呼ばれたら失敗させる（外へ問い合わせない）。"""

    def forbidden(*args: object, **kwargs: object) -> object:
        pytest.fail("本物の urlopen が呼ばれました")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


def _fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _fixture_with(**daily: list[Any]) -> bytes:
    data = json.loads(_fixture_bytes())
    data["daily"].update(daily)
    return json.dumps(data).encode()


class FakeResponse(io.BytesIO):
    """urlopen の返す応答の代わり（with で使え、read(n) で読める）。"""


class FakeOpener:
    """urlopen の代わり。決めた応答を返すか、決めた例外を上げ、受け取ったものを覚える。"""

    def __init__(self, body: bytes = b"", error: BaseException | None = None) -> None:
        self.body = body
        self.error = error
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float | None] = []

    def __call__(self, request: urllib.request.Request, timeout: float | None = None) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.body)


def _weather(
    opener: Callable[..., Any], lat: float = 35.68, lon: float = 139.69
) -> OpenMeteoWeather:
    return OpenMeteoWeather(lat, lon, opener=opener)


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.open-meteo.com/v1/forecast", code, "error", Message(), io.BytesIO(body)
    )


# --- 問い合わせの URL（9.2） ---


@pytest.mark.parametrize(
    ("lat", "lon", "want_lat", "want_lon"),
    [
        (35.68, 139.69, "35.7", "139.7"),
        (35.65, 139.65, "35.7", "139.7"),  # 浮動小数点で 35.649999… でも 0.5 は上へ
        (35.64, 139.75, "35.6", "139.8"),
        (HIDDEN_LAT, HIDDEN_LON, "35.7", "139.6"),
        (-33.85, -151.25, "-33.9", "-151.3"),  # 負は 0 から遠い方へ
        (-0.04, 0.0, "0.0", "0.0"),  # -0.0 と書かない
        (90.0, -180.0, "90.0", "-180.0"),
        (35, 139, "35.0", "139.0"),
    ],
)
def test_URL_には_0_1_度に丸めた座標だけが入る(
    lat: float, lon: float, want_lat: str, want_lon: str
) -> None:
    assert forecast_url(lat, lon) == BASE_URL.format(lat=want_lat, lon=want_lon)


def test_問い合わせは丸めた座標の_GET_を_1_回だけ時間切れを付けて送る() -> None:
    opener = FakeOpener(_fixture_bytes())
    _weather(opener, HIDDEN_LAT, HIDDEN_LON).daily(TODAY, 7.5)
    assert len(opener.requests) == 1
    request = opener.requests[0]
    assert request.full_url == BASE_URL.format(lat="35.7", lon="139.6")
    assert request.get_method() == "GET"
    assert request.data is None
    assert opener.timeouts == [7.5]
    assert "models" not in request.full_url
    assert str(HIDDEN_LAT) not in request.full_url
    assert str(HIDDEN_LON) not in request.full_url


def test_個人の値のない決まった_User_Agent_を付ける() -> None:
    opener = FakeOpener(_fixture_bytes())
    _weather(opener).daily(TODAY, 5.0)
    request = opener.requests[0]
    assert request.get_header("User-agent") == "OsekkaiRover"
    # 決まった頭書きのほかに、何も足さない
    assert {name for name, _ in request.header_items()} <= {"User-agent", "Accept"}


def test_opener_を渡さなければ_urllib_の_urlopen_を呼ぶ時に使う(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = FakeOpener(_fixture_bytes())
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    result = OpenMeteoWeather(35.68, 139.69).daily(TODAY, 3.0)
    assert isinstance(result, DayForecast)
    assert opener.timeouts == [3.0]


# --- 見本を読む（5.1、5.2） ---


def test_朝は見本から今日の予報を読む() -> None:
    result = _weather(FakeOpener(_fixture_bytes())).daily(TODAY, 5.0)
    assert result == DayForecast(TODAY, Condition.SUNNY, 15, 7, 6)


def test_夜は見本からあしたの予報を読む() -> None:
    result = _weather(FakeOpener(_fixture_bytes())).daily(TOMORROW, 5.0)
    # 13.2 → 13、5.5 → 6（偶数への丸めにしない）
    assert result == DayForecast(TOMORROW, Condition.SUNNY, 13, 6, 0)


def test_天気のコードを天気の種類に分ける() -> None:
    body = _fixture_with(weather_code=[63, 999])
    weather = _weather(FakeOpener(body))
    today = weather.daily(TODAY, 5.0)
    tomorrow = weather.daily(TOMORROW, 5.0)
    assert isinstance(today, DayForecast) and today.condition is Condition.RAIN
    assert isinstance(tomorrow, DayForecast) and tomorrow.condition is Condition.UNKNOWN


def test_天気のコードが_null_なら分からないとして気温は通す() -> None:
    result = _weather(FakeOpener(_fixture_with(weather_code=[None, 0]))).daily(TODAY, 5.0)
    assert result == DayForecast(TODAY, Condition.UNKNOWN, 15, 7, 6)


def test_小数の天気のコードも整数として読む() -> None:
    result = _weather(FakeOpener(_fixture_with(weather_code=[71.0, 0]))).daily(TODAY, 5.0)
    assert isinstance(result, DayForecast) and result.condition is Condition.SNOW


def test_降水確率の_null_は確率なしとして通す() -> None:
    body = _fixture_with(precipitation_probability_max=[None, 0])
    result = _weather(FakeOpener(body)).daily(TODAY, 5.0)
    assert result == DayForecast(TODAY, Condition.SUNNY, 15, 7, None)


def test_氷点下の気温も四捨五入する() -> None:
    body = _fixture_with(temperature_2m_max=[-0.4, 1.0], temperature_2m_min=[-5.5, 0.0])
    result = _weather(FakeOpener(body)).daily(TODAY, 5.0)
    assert isinstance(result, DayForecast)
    assert (result.t_max, result.t_min) == (0, -6)


# --- 取れなかった結果（5.10、10.1、10.2） ---


def _kind(result: object) -> WeatherProblem:
    assert isinstance(result, WeatherUnavailable)
    return result.kind


def test_対象の日がなければ古いとして返す() -> None:
    result = _weather(FakeOpener(_fixture_bytes())).daily(date(2026, 11, 22), 5.0)
    assert _kind(result) is WeatherProblem.STALE


def test_日付の並びが空なら古いとして返す() -> None:
    body = _fixture_with(
        time=[],
        weather_code=[],
        temperature_2m_max=[],
        temperature_2m_min=[],
        precipitation_probability_max=[],
    )
    assert _kind(_weather(FakeOpener(body)).daily(TODAY, 5.0)) is WeatherProblem.STALE


@pytest.mark.parametrize(
    "daily",
    [
        {"temperature_2m_max": [None, 13.2]},
        {"temperature_2m_min": [None, 5.5]},
    ],
)
def test_気温が欠けていれば欠けているとして返す(daily: dict[str, list[Any]]) -> None:
    result = _weather(FakeOpener(_fixture_with(**daily))).daily(TODAY, 5.0)
    assert _kind(result) is WeatherProblem.INCOMPLETE


def test_ほかの日の気温の欠けは対象の日に響かない() -> None:
    body = _fixture_with(temperature_2m_max=[14.7, None])
    assert isinstance(_weather(FakeOpener(body)).daily(TODAY, 5.0), DayForecast)


def _without(key: str) -> bytes:
    data = json.loads(_fixture_bytes())
    del data["daily"][key]
    return json.dumps(data).encode()


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"\xff\xfe",
        b"[]",
        b"null",
        b'{"latitude": 35.7}',
        b'{"daily": []}',
        b'{"daily": {"time": "2026-11-20"}}',
        _without("time"),
        _without("weather_code"),
        _without("temperature_2m_max"),
        _without("temperature_2m_min"),
        _without("precipitation_probability_max"),
        _fixture_with(temperature_2m_max=[14.7]),  # 長さが日付と違う
        _fixture_with(temperature_2m_max=["14.7", 13.2]),
        _fixture_with(temperature_2m_min=[True, 5.5]),
        _fixture_with(weather_code=["0", 0]),
        _fixture_with(weather_code=[0.5, 0]),
        _fixture_with(precipitation_probability_max=["6", 0]),
        _fixture_with(time=[20261120, "2026-11-21"]),
        b'{"daily": {"time": ["2026-11-20"], "weather_code": [0],'
        b' "temperature_2m_max": [NaN], "temperature_2m_min": [1],'
        b' "precipitation_probability_max": [0]}}',
        b'{"daily": {"time": ["2026-11-20"], "weather_code": [0],'
        b' "temperature_2m_max": [Infinity], "temperature_2m_min": [1],'
        b' "precipitation_probability_max": [0]}}',
        b'{"error": true, "reason": "' + HIDDEN_REASON.encode() + b'"}',
    ],
)
def test_形が違えば形の違いとして返す(body: bytes) -> None:
    assert _kind(_weather(FakeOpener(body)).daily(TODAY, 5.0)) is WeatherProblem.BAD_RESPONSE


def test_大きすぎる応答は形の違いとして返す() -> None:
    body = _fixture_bytes()[:-1] + b"," + b'"pad":"' + b"x" * open_meteo.MAX_BYTES + b'"}'
    assert _kind(_weather(FakeOpener(body)).daily(TODAY, 5.0)) is WeatherProblem.BAD_RESPONSE


def test_HTTP_400_の誤りの応答は形の違いとして返す() -> None:
    error = _http_error(400, b'{"error": true, "reason": "' + HIDDEN_REASON.encode() + b'"}')
    result = _weather(FakeOpener(error=error)).daily(TODAY, 5.0)
    assert _kind(result) is WeatherProblem.BAD_RESPONSE


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_そのほかの_HTTP_の誤りはつながらないとして返す(code: int) -> None:
    error = _http_error(code)
    assert _kind(_weather(FakeOpener(error=error)).daily(TODAY, 5.0)) is WeatherProblem.CONNECTION


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("name resolution"),
        ConnectionError(),
        ConnectionResetError(),
        OSError("network unreachable"),
    ],
)
def test_つながらなければつながらないとして返す(error: BaseException) -> None:
    assert _kind(_weather(FakeOpener(error=error)).daily(TODAY, 5.0)) is WeatherProblem.CONNECTION


@pytest.mark.parametrize(
    "error",
    # socket.timeout は TimeoutError の別名なので、TimeoutError で確かめる
    [
        TimeoutError(),
        TimeoutError("timed out"),
        urllib.error.URLError(TimeoutError("timed out")),
    ],
)
def test_時間切れなら時間切れとして返す(error: BaseException) -> None:
    assert _kind(_weather(FakeOpener(error=error)).daily(TODAY, 5.0)) is WeatherProblem.TIMEOUT


class _SlowReadResponse(io.BytesIO):
    def read(self, size: int | None = -1) -> bytes:
        raise TimeoutError


def test_読む途中の時間切れも時間切れとして返す() -> None:
    def opener(request: urllib.request.Request, timeout: float | None = None) -> Any:
        return _SlowReadResponse()

    assert _kind(_weather(opener).daily(TODAY, 5.0)) is WeatherProblem.TIMEOUT


def test_思いがけない例外も外へ出さない() -> None:
    def opener(request: urllib.request.Request, timeout: float | None = None) -> Any:
        raise RuntimeError(HIDDEN_REASON)

    assert isinstance(_weather(opener).daily(TODAY, 5.0), WeatherUnavailable)


# --- ログ（9.4） ---


def _failures() -> list[Callable[..., Any]]:
    return [
        FakeOpener(b"not json"),
        FakeOpener(b'{"error": true, "reason": "' + HIDDEN_REASON.encode() + b'"}'),
        FakeOpener(_fixture_with(temperature_2m_max=[None, None])),
        FakeOpener(error=_http_error(400, HIDDEN_REASON.encode())),
        FakeOpener(error=_http_error(503, HIDDEN_REASON.encode())),
        FakeOpener(error=urllib.error.URLError(HIDDEN_REASON)),
        FakeOpener(error=TimeoutError(HIDDEN_REASON)),
    ]


def test_ログには決まった文言と種類だけを出し_座標も応答の中身も出さない(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="osekkai"):
        for opener in _failures():
            _weather(opener, HIDDEN_LAT, HIDDEN_LON).daily(TODAY, 5.0)
        _weather(FakeOpener(_fixture_bytes()), HIDDEN_LAT, HIDDEN_LON).daily(TODAY, 5.0)
        _weather(FakeOpener(_fixture_bytes()), HIDDEN_LAT, HIDDEN_LON).daily(date(2027, 1, 1), 5.0)
    assert caplog.records, "失敗の種類がログに出ていません"
    text = caplog.text
    for hidden in (*HIDDEN_TEXTS, HIDDEN_REASON, "14.7", "7.1", "2026-11-20"):
        assert hidden not in text, hidden
    for record in caplog.records:
        assert record.getMessage().startswith(open_meteo.MSG_UNAVAILABLE)
    kinds = {r.getMessage().removeprefix(open_meteo.MSG_UNAVAILABLE) for r in caplog.records}
    assert any(WeatherProblem.STALE.value in k for k in kinds)
    assert any(WeatherProblem.TIMEOUT.value in k for k in kinds)


def test_取れたときはログを出さない(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="osekkai"):
        _weather(FakeOpener(_fixture_bytes())).daily(TODAY, 5.0)
    assert caplog.records == []
