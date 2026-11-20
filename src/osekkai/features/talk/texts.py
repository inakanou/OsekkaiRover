"""朝と夜のお話の決まった言葉と LCD の文字、呼び名の差し込み、豆知識の決まりの文
（5.7〜5.9、6.3〜6.5、7.1、7.2、7.6、7.7、9.1、9.3）。

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

# 豆知識の決まりの文（6.3、6.5）。外部の会話の AI の system に、子ども向けの決まり
# （conversation の KID_RULES、6.4）と組み合わせて渡す。呼び名・現在地・天気は入れない（9.1、9.3）
FACT_RULES: Final = """\
あなたは、4 さいくらいの子どもに、好きなものの豆知識を 1 つだけ話すロボットです。

豆知識の決まり:
4 さいの子どもでも分かる、やさしい言葉で、2 文以内の短いお話にしてください。
よく知られた本当のことだけを話してください。確かでないことや、作った話は言わないでください。
返事はふつうの文だけにしてください。絵文字・記号・箇条書き・かっこは使わないでください。\
文の終わりには「。」「！」「？」だけを使ってください。
子どもの名前は使わず、子どもに名前で呼びかけないでください。\
{name} のような目印も書かないでください。
あいさつや前置きは言わず、豆知識の文だけを返してください。
"""
# 夜と朝で変える決まり。夜は寝る前に合う、落ち着いた話にする（6.5）
FACT_KIND_RULES: Final[Mapping[TalkKind, str]] = MappingProxyType(
    {
        TalkKind.NIGHT: (
            "いまは夜で、寝る前に聞くお話です。"
            "心が落ち着いて、静かな気持ちになれる話にしてください。"
            "わくわくしすぎる話や、体を動かしたくなる話はしないでください。\n"
        ),
        TalkKind.MORNING: (
            "いまは朝で、一日の始まりに聞くお話です。明るく、楽しい気持ちになれる話にしてください。\n"
        ),
    }
)
FACT_KIND_HEADING: Final = "お話の時間:\n"
KID_RULES_HEADING: Final = "子ども向けの守ること:\n"  # conversation の KID_RULES の前に付ける見出し

# 豆知識を頼むやりとり（9.3）。好きなもの・夜か朝か・月と季節だけを送る
FACT_USER: Final = "すきな もの: {topic}\n{time}\n{month}がつ、{season}"
FACT_TIME_WORDS: Final[Mapping[TalkKind, str]] = MappingProxyType(
    {TalkKind.NIGHT: "よる", TalkKind.MORNING: "あさ"}
)
SEASON_SPRING: Final = "はる"  # 3〜5 月
SEASON_SUMMER: Final = "なつ"  # 6〜8 月
SEASON_AUTUMN: Final = "あき"  # 9〜11 月
SEASON_WINTER: Final = "ふゆ"  # 12〜2 月


def fill_name(text: str, call_name: str) -> str:
    """言葉の `{name}` を呼び名にする。なければ「きみ」。ほかの波かっこはそのまま（7.2）。"""
    name = call_name.strip() or DEFAULT_CALL_NAME
    return text.replace(NAME_SLOT, name)
