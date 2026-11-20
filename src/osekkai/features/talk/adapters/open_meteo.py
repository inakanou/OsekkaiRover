"""Open-Meteo の日ごとの予報のアダプタ（5.1、5.2、5.10、9.2、9.4、10.1、10.2）。

今日とあしたの天気のコード・最高と最低の気温・降水確率を、1 回の GET で取る。
送るのは 0.1 度（約 10 km）に丸めた座標だけ（9.2）。`models` は渡さない（`jma_seamless`
では降水確率が null になるため）。時間切れは必ず付ける（10.2）。

失敗は例外にせず `WeatherUnavailable(種類)` で返す（10.1）。ログには決まった文言と
失敗の種類（と例外の型の名前）だけを出し、URL・座標・応答の中身を出さない（9.4）。
"""

from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from http import HTTPStatus
from http.client import HTTPException
from typing import Any, Final

from ..model import (
    Condition,
    DayForecast,
    WeatherProblem,
    WeatherResult,
    WeatherUnavailable,
    condition_from_wmo,
    round_half_up,
)

logger = logging.getLogger(__name__)

ENDPOINT: Final = "https://api.open-meteo.com/v1/forecast"
DAILY_FIELDS: Final = (
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_probability_max",
)
USER_AGENT: Final = "OsekkaiRover"  # 個人の値を入れない
MAX_BYTES: Final = 64 * 1024  # 応答はふつう 1 KB ほど。これを超えたら形の違いとして読まない
MSG_UNAVAILABLE: Final = "天気の予報を取れません: "

_TENTH: Final = Decimal("0.1")

Opener = Callable[..., Any]


class _Unavailable(Exception):
    """読み方の途中で、取れなかった種類を伝える（外へは出さない）。"""

    def __init__(self, kind: WeatherProblem) -> None:
        super().__init__(kind.value)
        self.kind = kind


def _coordinate(value: float) -> str:
    """座標を 0.1 度に四捨五入した文字にする（0.5 は 0 から遠い方へ。-0.0 にしない）。

    `f"{value:.1f}"` は 35.65（実際は 35.649999…）を 35.6 にするので、10 進の文字から丸める。
    """
    rounded = Decimal(str(float(value))).quantize(_TENTH, rounding=ROUND_HALF_UP)
    if rounded.is_zero():
        rounded = abs(rounded)
    return f"{rounded:.1f}"


def forecast_url(latitude: float, longitude: float) -> str:
    """問い合わせの URL。座標は 0.1 度に丸めたものだけを入れる（9.2）。"""
    return (
        f"{ENDPOINT}?latitude={_coordinate(latitude)}&longitude={_coordinate(longitude)}"
        f"&daily={','.join(DAILY_FIELDS)}&timezone=Asia%2FTokyo&forecast_days=2"
    )


class OpenMeteoWeather:
    """Open-Meteo から日ごとの予報を取る（`model.WeatherPort`）。例外を外へ出さない。

    `opener` は `urllib.request.urlopen` と同じ形の呼び出し（テストで差し替える）。
    渡さなければ、呼ぶ時に `urllib.request.urlopen` を使う。
    """

    def __init__(self, latitude: float, longitude: float, *, opener: Opener | None = None) -> None:
        # 丸めた URL だけを持つ（丸める前の座標を残さない）
        self._url = forecast_url(latitude, longitude)
        self._opener = opener

    def daily(self, day: date, timeout_s: float) -> WeatherResult:
        try:
            body = self._fetch(timeout_s)
            return _parse(body, day)
        except _Unavailable as exc:
            return _unavailable(exc.kind)
        except Exception as exc:
            # 思いがけない失敗も外へ出さない。型の名前だけを残す
            return _unavailable(WeatherProblem.BAD_RESPONSE, exc)

    def _fetch(self, timeout_s: float) -> bytes:
        opener = self._opener if self._opener is not None else urllib.request.urlopen
        request = urllib.request.Request(
            self._url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            method="GET",
        )
        try:
            with opener(request, timeout=timeout_s) as response:
                body: bytes = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # 誤りの応答の中身は読まない。400 は問い合わせの形の誤り（Open-Meteo の reason）
            exc.close()
            if exc.code == HTTPStatus.BAD_REQUEST:
                raise _Unavailable(WeatherProblem.BAD_RESPONSE) from None
            raise _Unavailable(WeatherProblem.CONNECTION) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise _Unavailable(WeatherProblem.TIMEOUT) from None
            raise _Unavailable(WeatherProblem.CONNECTION) from None
        except TimeoutError:  # socket.timeout も同じ型
            raise _Unavailable(WeatherProblem.TIMEOUT) from None
        except (OSError, HTTPException):
            raise _Unavailable(WeatherProblem.CONNECTION) from None
        if len(body) > MAX_BYTES:
            raise _Unavailable(WeatherProblem.BAD_RESPONSE)
        return body


def _parse(body: bytes, day: date) -> DayForecast:
    """応答から対象の日の予報を読む。読めなければ `_Unavailable` を上げる。"""
    try:
        data = json.loads(body)
    except ValueError:  # JSON の誤りと、UTF-8 でない文字
        raise _Unavailable(WeatherProblem.BAD_RESPONSE) from None
    daily = data.get("daily") if isinstance(data, Mapping) else None
    if not isinstance(daily, Mapping):
        raise _Unavailable(WeatherProblem.BAD_RESPONSE)
    times = _column(daily, "time", None)
    columns = [_column(daily, name, len(times)) for name in DAILY_FIELDS]
    if not all(isinstance(t, str) for t in times):
        raise _Unavailable(WeatherProblem.BAD_RESPONSE)
    target = day.isoformat()
    if target not in times:
        raise _Unavailable(WeatherProblem.STALE)  # 5.10
    i = times.index(target)
    code, t_max, t_min, rain = (column[i] for column in columns)
    condition = Condition.UNKNOWN if code is None else condition_from_wmo(_whole(code))
    if t_max is None or t_min is None:
        # 形の違いを先に見る（文字の気温は欠けでなく形の違い）
        for value in (t_max, t_min):
            if value is not None:
                _number(value)
        raise _Unavailable(WeatherProblem.INCOMPLETE)
    return DayForecast(
        day=day,
        condition=condition,
        t_max=round_half_up(_number(t_max)),
        t_min=round_half_up(_number(t_min)),
        rain_percent=None if rain is None else round_half_up(_number(rain)),
    )


def _column(daily: Mapping[str, Any], name: str, length: int | None) -> Sequence[Any]:
    """日ごとの並びを 1 つ取る。並びでない・日付と長さが違うなら形の違い。"""
    column = daily.get(name)
    if not isinstance(column, list) or (length is not None and len(column) != length):
        raise _Unavailable(WeatherProblem.BAD_RESPONSE)
    return column


def _number(value: object) -> float:
    """有限の数を取る（真偽値・文字・NaN・無限は形の違い）。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _Unavailable(WeatherProblem.BAD_RESPONSE)
    if not math.isfinite(value):
        raise _Unavailable(WeatherProblem.BAD_RESPONSE)
    return float(value)


def _whole(value: object) -> int:
    """天気のコードを整数で取る（71.0 は 71。0.5 のような端数は形の違い）。"""
    number = _number(value)
    if not number.is_integer():
        raise _Unavailable(WeatherProblem.BAD_RESPONSE)
    return int(number)


def _unavailable(kind: WeatherProblem, exc: BaseException | None = None) -> WeatherUnavailable:
    if exc is None:
        logger.warning("%s%s", MSG_UNAVAILABLE, kind.value)
    else:
        logger.warning("%s%s (%s)", MSG_UNAVAILABLE, kind.value, type(exc).__name__)
    return WeatherUnavailable(kind)
