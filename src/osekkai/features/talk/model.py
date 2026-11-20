"""朝と夜のお話の型とポート（5.8、7.1、12.1）。

アダプタ（天気・注意報）はこのモジュールだけを読む。外へのつながりの失敗は、例外でなく
取れなかった結果（`WeatherUnavailable`・`AlertsUnavailable`・`FactUnavailable`）で返す。
取れなかった結果は種類だけを持ち、応答の中身や値を持たない（ログに種類だけを出すため）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from types import MappingProxyType
from typing import Final, Protocol


class TalkKind(Enum):
    """夜のお話か朝のお話か。"""

    NIGHT = "night"
    MORNING = "morning"


class Condition(Enum):
    """天気の種類。分からないときは天気の文を省いて気温から話す。"""

    SUNNY = "sunny"
    CLOUDY = "cloudy"
    RAIN = "rain"
    SNOW = "snow"
    THUNDER = "thunder"
    UNKNOWN = "unknown"


# WMO の天気のコード（Open-Meteo の weather_code）の分け方。表にないコードは UNKNOWN
_WMO_CONDITIONS: Final[Mapping[int, Condition]] = MappingProxyType(
    {
        **dict.fromkeys((0, 1), Condition.SUNNY),
        **dict.fromkeys((2, 3, 45, 48), Condition.CLOUDY),
        **dict.fromkeys((*range(51, 68), *range(80, 83)), Condition.RAIN),
        **dict.fromkeys((*range(71, 78), 85, 86), Condition.SNOW),
        **dict.fromkeys(range(95, 100), Condition.THUNDER),
    }
)


def condition_from_wmo(code: int) -> Condition:
    """WMO の天気のコードを天気の種類に分ける。表にないコードは分からない（UNKNOWN）。

    天気のアダプタは model だけを読むので、分け方をここに置く。
    """
    return _WMO_CONDITIONS.get(code, Condition.UNKNOWN)


def round_half_up(value: float) -> int:
    """気温を四捨五入して整数の度にする（0.5 は 0 から遠い方へ。偶数への丸めにしない）。"""
    return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class DayForecast:
    """1 日分の予報。"""

    day: date
    condition: Condition
    t_max: int  # いちばん高い気温（四捨五入した度）
    t_min: int  # いちばん低い気温（四捨五入した度）
    rain_percent: int | None  # 降水確率の最大。分からなければ None


class AlertKind(Enum):
    """お話で触れる注意報・警報の種類。それ以外（乾燥・濃霧など）は話さない。"""

    HEAVY_RAIN = "heavy_rain"  # 大雨・土砂災害
    HEAVY_SNOW = "heavy_snow"  # 大雪
    STRONG_WIND = "strong_wind"  # 強風・暴風・風雪・暴風雪
    THUNDER = "thunder"  # 雷
    ROUGH_SEA = "rough_sea"  # 波浪・高潮


class WeatherProblem(Enum):
    """予報が取れなかった種類。"""

    CONNECTION = "connection"  # つながらない・HTTP の誤り
    TIMEOUT = "timeout"  # 時間切れ
    BAD_RESPONSE = "bad_response"  # 応答の形が違う
    STALE = "stale"  # お話の日の予報がない（古い）
    INCOMPLETE = "incomplete"  # 気温が欠けている


@dataclass(frozen=True)
class WeatherUnavailable:
    """予報が取れなかった。"""

    kind: WeatherProblem


class AlertsProblem(Enum):
    """注意報・警報が取れなかった種類。"""

    CONNECTION = "connection"  # つながらない・HTTP の誤り
    TIMEOUT = "timeout"  # 時間切れ
    BAD_RESPONSE = "bad_response"  # 応答の形が違う
    STALE = "stale"  # どの報も古い


@dataclass(frozen=True)
class AlertsUnavailable:
    """注意報・警報が取れなかった。"""

    kind: AlertsProblem


class FactProblem(Enum):
    """豆知識が取れなかった種類。"""

    REFUSED = "refused"  # 会話の AI が断った
    FAILED = "failed"  # つながらない・混み合っている・そのほか
    TIMEOUT = "timeout"  # 時間切れ
    EMPTY = "empty"  # 整えたら空になった


@dataclass(frozen=True)
class FactUnavailable:
    """豆知識が取れなかった。"""

    kind: FactProblem


WeatherResult = DayForecast | WeatherUnavailable
AlertsResult = frozenset[AlertKind] | AlertsUnavailable
FactResult = str | FactUnavailable


class WeatherPort(Protocol):
    """日ごとの予報を取る口。例外を外へ出さない。"""

    def daily(self, day: date, timeout_s: float) -> WeatherResult: ...


class AlertPort(Protocol):
    """今出ている注意報・警報の種類を取る口。例外を外へ出さない。"""

    def active(self, timeout_s: float) -> AlertsResult: ...


class FactSource(Protocol):
    """好きなものの豆知識を 1 つ取る口。例外を外へ出さない。"""

    def fact(self, kind: TalkKind, topic: str, today: date, timeout_s: float) -> FactResult: ...


@dataclass(frozen=True)
class Prepared:
    """話す前に用意したもの。取れなかった部分は None（話せる部分だけを話す）。"""

    kind: TalkKind
    weather_day: date
    forecast: DayForecast | None
    alerts: frozenset[AlertKind] | None  # None は取れなかった（触れない）。空は出ていない
    fact: str | None
    topic_hash: str | None  # 豆知識が取れたときの好きなものの hash。文字そのものは持たない
