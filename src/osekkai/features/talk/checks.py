"""朝と夜のお話の self-check の項目（1.7、1.8、10.4、11.1、11.2）。

- `talk.setup`（ハードなし、ネットワークなし）: 設定で何を話せるかを決まった言葉で知らせる
- `talk.weather`（ハードありの扱い、ネットワークあり）: 予報と注意報を取れるかを知らせる。
  `--no-hardware`（CI）と、サービスが動いていてロックを取れないとき（配置の後の確かめ）は
  基盤が動かさない。手で `--self-check` を実行したときだけ外へ問い合わせる

どちらも だめ（FAILED）にしない。ネットワークの都合や設定の選び方で、配置の確かめ（版を戻す）を
起こさないため。知らせたいことは、よい（OK）に理由を添える。

理由はすべて固定の文言にする。現在地・好きなもの・呼び名・鍵・天気の値を入れず、ログにも出さない。
日課は基盤の設定の報告の「有効かどうか」（基本の `FeatureConfig`）だけを見る
（日課の中身は読まない）。

`talk.setup` の理由は 1 つだけにし、次の順で最も大事なものを選ぶ。

1. 日課が無効（または設定を使えない）: 「日課がないので話さない」（お話は日課の予告に続くため）
2. 現在地も好きなものもない: 「話すことがない」（1.7）
3. 好きなものがあるが、鍵かライブラリがない（10.4）: 現在地があれば「鍵がないので天気だけ」、
   なければ「鍵がないので話すことがない」
4. 片方だけ: 「天気だけ」「豆知識だけ」
5. どちらもあり、鍵も使える: 理由なしの よい
"""

from __future__ import annotations

from typing import Final

import osekkai.features.conversation.api as conversation_api
from osekkai.core.clock import Clock, SystemClock
from osekkai.core.config import FeatureConfig
from osekkai.core.selfcheck import Check, CheckContext, CheckResult, CheckStatus
from osekkai.features.talk.adapters.jma_alerts import JmaAlerts
from osekkai.features.talk.adapters.open_meteo import OpenMeteoWeather
from osekkai.features.talk.config import TalkConfig
from osekkai.features.talk.model import AlertsUnavailable, WeatherUnavailable

__all__ = [
    "FETCH_TIMEOUT_S",
    "REASON_ALERTS_UNAVAILABLE",
    "REASON_CONFIG_UNUSABLE",
    "REASON_DISABLED",
    "REASON_FACTS_ONLY",
    "REASON_NOTHING_TO_TALK",
    "REASON_NO_KEY_NOTHING",
    "REASON_NO_KEY_WEATHER_ONLY",
    "REASON_NO_LOCATION",
    "REASON_NO_ROUTINE",
    "REASON_WEATHER_ONLY",
    "REASON_WEATHER_UNAVAILABLE",
    "TALK_CHECKS",
    "WEATHER_CHECK_TIMEOUT_S",
    "make_clock",
]

FEATURE_NAME: Final = "talk"
ROUTINE_NAME: Final = "routine"

# 固定の理由の文言
REASON_DISABLED: Final = "無効"
REASON_CONFIG_UNUSABLE: Final = "設定を使えない"
REASON_NO_ROUTINE: Final = "日課がないので話さない"
REASON_NOTHING_TO_TALK: Final = "話すことがない"
REASON_NO_KEY_WEATHER_ONLY: Final = "鍵がないので天気だけ"
REASON_NO_KEY_NOTHING: Final = "鍵がないので話すことがない"
REASON_WEATHER_ONLY: Final = "天気だけ"
REASON_FACTS_ONLY: Final = "豆知識だけ"
REASON_NO_LOCATION: Final = "現在地がない"
REASON_WEATHER_UNAVAILABLE: Final = "天気を取れない"
REASON_ALERTS_UNAVAILABLE: Final = "注意報を取れない"
_JOIN: Final = "、"

# 天気の項目の時間（基盤が待つ長さ）。予報と注意報を順に取るので、1 回ずつはその半分より短くする
WEATHER_CHECK_TIMEOUT_S: Final = 25.0
FETCH_TIMEOUT_S: Final = 10.0

OK: Final = CheckResult(CheckStatus.OK)


class _NotSynced:
    """時刻の同期を問わない（確かめは今日の日付と注意報の今の時刻だけを使う）。"""

    def is_synchronized(self) -> bool:
        return False


def make_clock() -> Clock:
    """天気の確かめで使う時計。テストで差し替える。"""
    return SystemClock(_NotSynced())


def _notice(reason: str) -> CheckResult:
    """版を戻さずに知らせる（よいに理由を添える）。"""
    return CheckResult(CheckStatus.OK, reason)


def _config(ctx: CheckContext) -> TalkConfig | CheckResult:
    """お話の設定。無効か使えなければ、飛ばす結果を返す。"""
    config = ctx.report.features.get(FEATURE_NAME)
    if not isinstance(config, TalkConfig):
        # 設定の誤りは基盤の core.config が知らせる
        return CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)
    if not config.enabled:
        return CheckResult(CheckStatus.SKIPPED, REASON_DISABLED)
    return config


def _routine_enabled(ctx: CheckContext) -> bool:
    """日課が有効か。基本の `FeatureConfig` の `enabled` だけを見る。"""
    routine = ctx.report.features.get(ROUTINE_NAME)
    return isinstance(routine, FeatureConfig) and routine.enabled


def _key_usable(ctx: CheckContext) -> bool:
    """会話の AI の鍵とライブラリが使えるか。鍵の値は読んでも返さない。"""
    if not conversation_api.library_available():
        return False
    return conversation_api.setup_problem(ctx.paths) is None


def _check_setup(ctx: CheckContext) -> CheckResult:
    config = _config(ctx)
    if isinstance(config, CheckResult):
        return config
    if not _routine_enabled(ctx):
        return _notice(REASON_NO_ROUTINE)
    has_weather = config.location is not None
    has_topics = bool(config.topics)
    if not has_weather and not has_topics:
        return _notice(REASON_NOTHING_TO_TALK)  # 1.7
    if has_topics and not _key_usable(ctx):
        # 10.4。豆知識を話さない
        return _notice(REASON_NO_KEY_WEATHER_ONLY if has_weather else REASON_NO_KEY_NOTHING)
    if not has_topics:
        return _notice(REASON_WEATHER_ONLY)
    if not has_weather:
        return _notice(REASON_FACTS_ONLY)
    return OK


def _check_weather(ctx: CheckContext) -> CheckResult:
    config = _config(ctx)
    if isinstance(config, CheckResult):
        return config
    location = config.location
    if location is None:
        return CheckResult(CheckStatus.SKIPPED, REASON_NO_LOCATION)
    clock = make_clock()
    reasons: list[str] = []
    # 結果の中身（予報の値・注意報の種類）は見ずに捨てる
    weather = OpenMeteoWeather(location.latitude, location.longitude)
    if isinstance(weather.daily(clock.now().date(), FETCH_TIMEOUT_S), WeatherUnavailable):
        reasons.append(REASON_WEATHER_UNAVAILABLE)
    area = config.warning_area
    if area is not None:
        alerts = JmaAlerts(area.office, area.city, clock.now)
        if isinstance(alerts.active(FETCH_TIMEOUT_S), AlertsUnavailable):
            reasons.append(REASON_ALERTS_UNAVAILABLE)
    return _notice(_JOIN.join(reasons)) if reasons else OK


# 機能の申告（`FeatureSpec.checks`）にそのまま渡す項目の一覧
TALK_CHECKS: Final[tuple[Check, ...]] = (
    Check(f"{FEATURE_NAME}.setup", _check_setup),
    Check(
        f"{FEATURE_NAME}.weather",
        _check_weather,
        timeout_s=WEATHER_CHECK_TIMEOUT_S,
        hardware=True,  # 外へ問い合わせるので、手で実行したときだけ動かす
    ),
)
