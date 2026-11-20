"""PC 用の偽の天気・偽の注意報・偽の豆知識（12.1、12.2）。

端末の偽物（PC で偽のハード）で動かすときに使う。外へは何も問い合わせず、決まった予報と、
注意報なしと、渡された好きなものの言葉を入れた決まった 1 文の豆知識を返す。
"""

from __future__ import annotations

from datetime import date
from typing import Final

from ..model import AlertsResult, Condition, DayForecast, FactResult, TalkKind, WeatherResult

# 決まった予報：はれ、いちばん高い気温 18 度、いちばん低い気温 9 度、降水確率 10%
T_MAX: Final = 18
T_MIN: Final = 9
RAIN_PERCENT: Final = 10

# 決まった豆知識の後ろの部分。好きなものの言葉は足し算でつなぐ（波かっこを含む言葉でも
# 壊れないよう、format にかけない）。記号と絵文字は使わない
_FACT_TAIL: Final = "には ふしぎな ことが いっぱい あるんだって"


class CannedWeather:
    """どの日を頼まれても、その日の決まった予報を返す偽の天気。"""

    def daily(self, day: date, timeout_s: float) -> WeatherResult:
        return DayForecast(day, Condition.SUNNY, T_MAX, T_MIN, RAIN_PERCENT)


class CannedAlerts:
    """注意報・警報が何も出ていないと返す偽の注意報。"""

    def active(self, timeout_s: float) -> AlertsResult:
        return frozenset()


class CannedFacts:
    """好きなものの言葉を入れた決まった 1 文を返す偽の豆知識。"""

    def fact(self, kind: TalkKind, topic: str, today: date, timeout_s: float) -> FactResult:
        return topic + _FACT_TAIL
