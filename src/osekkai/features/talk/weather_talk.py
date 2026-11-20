"""天気の言葉と LCD の表示を組み立てる（5.1〜5.9、7.3）。

言葉はすべて texts の決まった部品から組み立てる（5.8）。夜はあしたの、朝は今日の予報を
受け取り（5.1、5.2）、「あしたは」「きょうは」で話す。純粋な関数で、ログを出さない。
"""

from __future__ import annotations

from typing import Final

from osekkai.core.lcd_text import to_lcd_codes
from osekkai.features.talk import texts
from osekkai.features.talk.config import TalkConfig
from osekkai.features.talk.model import AlertKind, Condition, DayForecast, TalkKind

# 天気・気温・傘・上着・水・注意報で最大 6 文（7.3 の長さの上限）
MAX_WEATHER_SENTENCES: Final = 6
LCD_WIDTH: Final = 16
_MINUS_WORD: Final = "まいなす "  # 氷点下の気温の言い方
# 降る確率が分からないときに傘を勧める天気（5.4）
_WET_CONDITIONS: Final = frozenset({Condition.RAIN, Condition.SNOW, Condition.THUNDER})


def _say_degrees(value: int) -> str:
    """気温を話す形にする。氷点下は「まいなす 3」のように言う。"""
    return f"{_MINUS_WORD}{-value}" if value < 0 else str(value)


def _wants_umbrella(forecast: DayForecast, config: TalkConfig) -> bool:
    """降る確率がしきい値以上なら傘。確率がなければ雨・雪・雷の天気で傘（5.4）。"""
    if forecast.rain_percent is None:
        return forecast.condition in _WET_CONDITIONS
    return forecast.rain_percent >= config.rain_percent


def _alert_sentence(day_word: str, alerts: frozenset[AlertKind]) -> str:
    """注意報・警報の種類を決めた順にやさしく並べ、家の大人に任せる一言で結ぶ（5.7）。"""
    hazards = texts.ALERT_SEPARATOR.join(
        texts.ALERT_WORDS[kind] for kind in texts.ALERT_ORDER if kind in alerts
    )
    return texts.SAY_ALERT.format(day=day_word, hazards=hazards)


def weather_sentences(
    kind: TalkKind,
    forecast: DayForecast,
    alerts: frozenset[AlertKind] | None,
    config: TalkConfig,
) -> list[str]:
    """天気の話の文を、天気 → 気温 → 傘 → 上着 → 水 → 注意報・警報の順に組み立てる。

    分からない天気は天気の文を省いて気温から話す。`alerts` が None（取れなかった）か空
    （出ていない）なら注意報・警報に触れない（5.11）。
    """
    day_word = texts.DAY_WORDS[kind]
    sentences: list[str] = []
    condition_word = texts.CONDITION_WORDS.get(forecast.condition)
    if condition_word is not None:
        sentences.append(texts.SAY_CONDITION.format(day=day_word, condition=condition_word))
    sentences.append(
        texts.SAY_TEMPERATURE.format(
            t_max=_say_degrees(forecast.t_max), t_min=_say_degrees(forecast.t_min)
        )
    )
    if _wants_umbrella(forecast, config):
        sentences.append(texts.SAY_UMBRELLA)
    if forecast.t_min < config.cold_below:
        sentences.append(texts.SAY_COAT)
    if forecast.t_max >= config.hot_at_or_above:
        sentences.append(texts.SAY_WATER)
    if alerts:
        sentences.append(_alert_sentence(day_word, alerts))
    return sentences[:MAX_WEATHER_SENTENCES]


def lcd_line(kind: TalkKind, forecast: DayForecast) -> str:
    """LCD の 1 行目の天気の表示（例「ｱｼﾀ ﾊﾚ 18/9」）。16 セルに収める（5.9）。

    分からない天気は天気の言葉を省く（「ｱｼﾀ 18/9」）。収まらないときも天気の言葉を省き、
    それでも長ければ 16 文字で切る。
    """
    day = texts.LCD_DAYS[kind]
    temps = f"{forecast.t_max}/{forecast.t_min}"
    condition = texts.LCD_CONDITIONS.get(forecast.condition)
    if condition is not None:
        line = f"{day} {condition} {temps}"
        if len(to_lcd_codes(line)) <= LCD_WIDTH:
            return line
    return f"{day} {temps}"[:LCD_WIDTH]
