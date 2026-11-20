"""朝と夜のお話の決まった言葉と LCD の文字、呼び名の差し込み（5.7〜5.9、7.1、7.2、7.6、7.7）。

話す言葉はひらがな中心で、言葉の切れ目に空白を入れる。怖がらせる言葉は使わない（5.7）。
LCD の文字は半角カタカナで、16 セルに収まる。
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from osekkai.features.talk.model import AlertKind, Condition, TalkKind

DEFAULT_CALL_NAME: Final = "きみ"  # 呼び名がないときの代わり（7.2）
NAME_SLOT: Final = "{name}"

# 始まりと終わりの言葉（7.1）。`{name}` に呼び名を入れる（7.2）
OPENERS: Final[Mapping[TalkKind, str]] = MappingProxyType(
    {
        TalkKind.NIGHT: "{name}、おやすみの まえの おはなし だよ",
        TalkKind.MORNING: "おはよう、{name}。きょうの おはなし だよ",
    }
)
CLOSERS: Final[Mapping[TalkKind, str]] = MappingProxyType(
    {
        TalkKind.NIGHT: "あしたも たのしみだね。おやすみ、{name}",
        TalkKind.MORNING: "きょうも いちにち がんばろうね、{name}",
    }
)

# 天気も豆知識もないときの決まった短い言葉。始まりの言葉は言わない（7.6）
SAY_FALLBACK_NIGHT: Final = "きょうも たのしかったね。おやすみ なさい"
SAY_FALLBACK_MORNING: Final = "おはよう。きょうも いちにち たのしもうね"
FALLBACKS: Final[Mapping[TalkKind, str]] = MappingProxyType(
    {TalkKind.NIGHT: SAY_FALLBACK_NIGHT, TalkKind.MORNING: SAY_FALLBACK_MORNING}
)

# 天気の言い方の部品（5.3〜5.8）。夜はあしたの、朝は今日の天気を話す
DAY_WORDS: Final[Mapping[TalkKind, str]] = MappingProxyType(
    {TalkKind.NIGHT: "あしたは", TalkKind.MORNING: "きょうは"}
)
# 分からない天気（UNKNOWN）は言葉を持たない。天気の文を省いて気温から話す
CONDITION_WORDS: Final[Mapping[Condition, str]] = MappingProxyType(
    {
        Condition.SUNNY: "はれ",
        Condition.CLOUDY: "くもり",
        Condition.RAIN: "あめ",
        Condition.SNOW: "ゆき",
        Condition.THUNDER: "かみなり",
    }
)
SAY_CONDITION: Final = "{day} {condition} だよ"  # 例: あしたは はれ だよ
SAY_TEMPERATURE: Final = "いちばん あつい ときは {t_max}ど、いちばん さむい ときは {t_min}ど だよ"
SAY_UMBRELLA: Final = "かさを もって いこうね"
SAY_COAT: Final = "うわぎを きて いこうね"
SAY_WATER: Final = "おみずを たくさん のもうね"

# 注意報・警報の言い方（5.7）。種類の言葉を ALERT_ORDER の順に ALERT_SEPARATOR でつなぎ、
# SAY_ALERT の {hazards} に入れる。家の大人に任せる一言で結ぶ
ALERT_ORDER: Final = (
    AlertKind.HEAVY_RAIN,
    AlertKind.HEAVY_SNOW,
    AlertKind.STRONG_WIND,
    AlertKind.THUNDER,
    AlertKind.ROUGH_SEA,
)
ALERT_WORDS: Final[Mapping[AlertKind, str]] = MappingProxyType(
    {
        AlertKind.HEAVY_RAIN: "あめが たくさん ふったり",
        AlertKind.HEAVY_SNOW: "ゆきが たくさん ふったり",
        AlertKind.STRONG_WIND: "かぜが つよく ふいたり",
        AlertKind.THUNDER: "かみなりが なったり",
        AlertKind.ROUGH_SEA: "うみが あれたり",
    }
)
ALERT_SEPARATOR: Final = "、"
ALERT_CLOSE: Final = "おうちの ひとと いっしょに きを つけようね"
# 例: あしたは あめが たくさん ふったり、かぜが つよく ふいたり する かも しれないよ。…
SAY_ALERT: Final = "{day} {hazards} する かも しれないよ。" + ALERT_CLOSE

# LCD の文字（5.9、7.7）。天気の表示は「ｱｼﾀ ﾊﾚ 18/9」の形
LCD_TALK: Final = "ｵﾊﾅｼ"  # 天気の話のほかの間
LCD_DAYS: Final[Mapping[TalkKind, str]] = MappingProxyType(
    {TalkKind.NIGHT: "ｱｼﾀ", TalkKind.MORNING: "ｷｮｳ"}
)
LCD_CONDITIONS: Final[Mapping[Condition, str]] = MappingProxyType(
    {
        Condition.SUNNY: "ﾊﾚ",
        Condition.CLOUDY: "ｸﾓﾘ",
        Condition.RAIN: "ｱﾒ",
        Condition.SNOW: "ﾕｷ",
        Condition.THUNDER: "ｶﾐﾅﾘ",
    }
)


def fill_name(text: str, call_name: str) -> str:
    """言葉の `{name}` を呼び名にする。なければ「きみ」。ほかの波かっこはそのまま（7.2）。"""
    name = call_name.strip() or DEFAULT_CALL_NAME
    return text.replace(NAME_SLOT, name)
