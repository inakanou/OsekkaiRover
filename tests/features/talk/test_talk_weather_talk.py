"""天気の言葉と LCD の表示（5.1〜5.9、7.3）と、天気のコードの分け方・気温の四捨五入。"""

from datetime import date

import pytest

from osekkai.core.lcd_text import to_lcd_codes
from osekkai.features.talk import texts
from osekkai.features.talk.config import TalkConfig
from osekkai.features.talk.model import (
    AlertKind,
    Condition,
    DayForecast,
    TalkKind,
    condition_from_wmo,
    round_half_up,
)
from osekkai.features.talk.weather_talk import (
    MAX_WEATHER_SENTENCES,
    lcd_line,
    weather_sentences,
)

SCARY_WORDS = (
    "きけん",
    "さいがい",
    "しぬ",
    "こわい",
    "あぶない",
    "ひなん",
    "けいほう",
    "ちゅういほう",
)
LCD_WIDTH = 16
DAY = date(2026, 11, 21)
CONFIG = TalkConfig()  # 傘 50% 以上、上着 10 度より低い、水 30 度以上


def _forecast(
    condition: Condition = Condition.SUNNY,
    t_max: int = 18,
    t_min: int = 12,
    rain_percent: int | None = 10,
) -> DayForecast:
    return DayForecast(
        day=DAY, condition=condition, t_max=t_max, t_min=t_min, rain_percent=rain_percent
    )


# --- 天気のコードの分け方（WMO） ---


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, Condition.SUNNY),
        (1, Condition.SUNNY),
        (2, Condition.CLOUDY),
        (3, Condition.CLOUDY),
        (45, Condition.CLOUDY),
        (48, Condition.CLOUDY),
        (51, Condition.RAIN),
        (67, Condition.RAIN),
        (80, Condition.RAIN),
        (82, Condition.RAIN),
        (71, Condition.SNOW),
        (77, Condition.SNOW),
        (85, Condition.SNOW),
        (86, Condition.SNOW),
        (95, Condition.THUNDER),
        (96, Condition.THUNDER),
        (99, Condition.THUNDER),
    ],
)
def test_天気のコードを種類に分ける(code: int, expected: Condition) -> None:
    assert condition_from_wmo(code) is expected


@pytest.mark.parametrize("code", [-1, 4, 44, 46, 49, 50, 68, 70, 78, 79, 83, 84, 87, 94, 100])
def test_表にない天気のコードは分からない(code: int) -> None:
    assert condition_from_wmo(code) is Condition.UNKNOWN


# --- 気温の四捨五入 ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (17.4, 17),
        (17.5, 18),
        (18.5, 19),  # 偶数への丸め（Python の round）にしない
        (0.5, 1),
        (0.4, 0),
        (-0.4, 0),
        (-2.5, -3),  # 0 から遠い方へ（四捨五入）
        (-2.4, -2),
        (9.0, 9),
    ],
)
def test_気温は四捨五入する(value: float, expected: int) -> None:
    assert round_half_up(value) == expected


# --- 文の組み立て ---


def test_夜はあしたの天気と気温を話す() -> None:
    sentences = weather_sentences(TalkKind.NIGHT, _forecast(), frozenset(), CONFIG)
    assert sentences == [
        "あしたは はれ だよ",
        "いちばん あつい ときは 18ど、いちばん さむい ときは 12ど だよ",
    ]


def test_朝は今日の天気と気温を話す() -> None:
    sentences = weather_sentences(TalkKind.MORNING, _forecast(Condition.CLOUDY), None, CONFIG)
    assert sentences[0] == "きょうは くもり だよ"
    assert sentences[1] == "いちばん あつい ときは 18ど、いちばん さむい ときは 12ど だよ"


@pytest.mark.parametrize(
    ("condition", "word"),
    [
        (Condition.SUNNY, "はれ"),
        (Condition.CLOUDY, "くもり"),
        (Condition.RAIN, "あめ"),
        (Condition.SNOW, "ゆき"),
        (Condition.THUNDER, "かみなり"),
    ],
)
def test_天気の言葉(condition: Condition, word: str) -> None:
    sentences = weather_sentences(TalkKind.NIGHT, _forecast(condition), None, CONFIG)
    assert sentences[0] == f"あしたは {word} だよ"


def test_分からない天気は天気の文を省いて気温から話す() -> None:
    sentences = weather_sentences(TalkKind.NIGHT, _forecast(Condition.UNKNOWN), None, CONFIG)
    assert sentences[0].startswith("いちばん あつい ときは")
    assert len(sentences) == 1


def test_氷点下の気温はまいなすと話す() -> None:
    sentences = weather_sentences(
        TalkKind.MORNING, _forecast(Condition.SNOW, t_max=-1, t_min=-5), None, CONFIG
    )
    assert sentences[1] == (
        "いちばん あつい ときは まいなす 1ど、いちばん さむい ときは まいなす 5ど だよ"
    )


# --- 傘 ---


def test_降る確率がしきい値ちょうどなら傘を勧める() -> None:
    sentences = weather_sentences(
        TalkKind.NIGHT, _forecast(Condition.CLOUDY, rain_percent=50), None, CONFIG
    )
    assert texts.SAY_UMBRELLA in sentences


def test_降る確率がしきい値より低ければ傘を勧めない() -> None:
    sentences = weather_sentences(
        TalkKind.NIGHT, _forecast(Condition.RAIN, rain_percent=49), None, CONFIG
    )
    assert texts.SAY_UMBRELLA not in sentences


def test_傘のしきい値は設定に従う() -> None:
    config = TalkConfig(rain_percent=30)
    assert texts.SAY_UMBRELLA in weather_sentences(
        TalkKind.NIGHT, _forecast(rain_percent=30), None, config
    )
    assert texts.SAY_UMBRELLA not in weather_sentences(
        TalkKind.NIGHT, _forecast(rain_percent=29), None, config
    )


@pytest.mark.parametrize("condition", [Condition.RAIN, Condition.SNOW, Condition.THUNDER])
def test_確率がないときは雨と雪と雷の天気で傘を勧める(condition: Condition) -> None:
    sentences = weather_sentences(
        TalkKind.NIGHT, _forecast(condition, rain_percent=None), None, CONFIG
    )
    assert texts.SAY_UMBRELLA in sentences


@pytest.mark.parametrize("condition", [Condition.SUNNY, Condition.CLOUDY, Condition.UNKNOWN])
def test_確率がないときはほかの天気で傘を勧めない(condition: Condition) -> None:
    sentences = weather_sentences(
        TalkKind.NIGHT, _forecast(condition, rain_percent=None), None, CONFIG
    )
    assert texts.SAY_UMBRELLA not in sentences


# --- 上着と水 ---


def test_いちばん低い気温がしきい値より低ければ上着を勧める() -> None:
    sentences = weather_sentences(TalkKind.NIGHT, _forecast(t_min=9), None, CONFIG)
    assert texts.SAY_COAT in sentences


def test_いちばん低い気温がしきい値ちょうどなら上着を勧めない() -> None:
    sentences = weather_sentences(TalkKind.NIGHT, _forecast(t_min=10), None, CONFIG)
    assert texts.SAY_COAT not in sentences


def test_いちばん高い気温がしきい値ちょうどなら水を勧める() -> None:
    sentences = weather_sentences(TalkKind.NIGHT, _forecast(t_max=30, t_min=22), None, CONFIG)
    assert texts.SAY_WATER in sentences


def test_いちばん高い気温がしきい値より低ければ水を勧めない() -> None:
    sentences = weather_sentences(TalkKind.NIGHT, _forecast(t_max=29, t_min=22), None, CONFIG)
    assert texts.SAY_WATER not in sentences


def test_上着と水のしきい値は設定に従う() -> None:
    config = TalkConfig(cold_below=0, hot_at_or_above=20)
    sentences = weather_sentences(TalkKind.NIGHT, _forecast(t_max=20, t_min=0), None, config)
    assert texts.SAY_WATER in sentences
    assert texts.SAY_COAT not in sentences
    sentences = weather_sentences(TalkKind.NIGHT, _forecast(t_max=19, t_min=-1), None, config)
    assert texts.SAY_WATER not in sentences
    assert texts.SAY_COAT in sentences


# --- 注意報・警報 ---


@pytest.mark.parametrize("alerts", [None, frozenset()])
def test_注意報がないか取れないときは触れない(alerts: frozenset[AlertKind] | None) -> None:
    sentences = weather_sentences(TalkKind.NIGHT, _forecast(), alerts, CONFIG)
    assert not any(texts.ALERT_CLOSE in s for s in sentences)


def test_注意報は種類を決めた順にやさしく並べて家の大人に任せる() -> None:
    alerts = frozenset({AlertKind.ROUGH_SEA, AlertKind.HEAVY_RAIN, AlertKind.STRONG_WIND})
    sentences = weather_sentences(TalkKind.NIGHT, _forecast(), alerts, CONFIG)
    assert sentences[-1] == (
        "あしたは あめが たくさん ふったり、かぜが つよく ふいたり、うみが あれたり"
        " する かも しれないよ。おうちの ひとと いっしょに きを つけようね"
    )


def test_朝の注意報はきょうはで始める() -> None:
    sentences = weather_sentences(
        TalkKind.MORNING, _forecast(), frozenset({AlertKind.THUNDER}), CONFIG
    )
    assert sentences[-1].startswith("きょうは かみなりが なったり する かも しれないよ。")
    assert sentences[-1].endswith("おうちの ひとと いっしょに きを つけようね")


def test_注意報の言い方に怖い言葉がない() -> None:
    every = frozenset(AlertKind)
    for kind in TalkKind:
        for condition in Condition:
            sentences = weather_sentences(
                kind, _forecast(condition, t_max=35, t_min=-5, rain_percent=90), every, CONFIG
            )
            for sentence in sentences:
                for word in SCARY_WORDS:
                    assert word not in sentence


# --- 順と上限（7.3） ---


def test_文は天気_気温_傘_上着_水_注意報の順で最大6文() -> None:
    forecast = _forecast(Condition.THUNDER, t_max=31, t_min=5, rain_percent=80)
    sentences = weather_sentences(TalkKind.NIGHT, forecast, frozenset(AlertKind), CONFIG)
    assert len(sentences) == MAX_WEATHER_SENTENCES == 6
    assert sentences[0] == "あしたは かみなり だよ"
    assert sentences[1].startswith("いちばん あつい ときは 31ど")
    assert sentences[2:5] == [texts.SAY_UMBRELLA, texts.SAY_COAT, texts.SAY_WATER]
    assert sentences[5].endswith(texts.ALERT_CLOSE)


def test_いちばん長いときでも天気の話はおおむね短い() -> None:
    # 1 秒におよそ 6 文字として、天気の話は 30 秒ほど（230 文字）までに収める（7.3）
    forecast = _forecast(Condition.THUNDER, t_max=-10, t_min=-15, rain_percent=100)
    sentences = weather_sentences(TalkKind.MORNING, forecast, frozenset(AlertKind), CONFIG)
    assert len(sentences) <= MAX_WEATHER_SENTENCES
    assert sum(len(s) for s in sentences) <= 230


def test_文はひらがなの決まった言い方だけでできている() -> None:
    forecast = _forecast(Condition.RAIN, t_max=31, t_min=5, rain_percent=80)
    sentences = weather_sentences(TalkKind.NIGHT, forecast, frozenset(AlertKind), CONFIG)
    allowed = set("、。 0123456789")
    for sentence in sentences:
        for ch in sentence:
            assert ch in allowed or "ぁ" <= ch <= "ゟ", sentence


# --- LCD（5.9） ---


def test_LCDは夜ならあしたと天気と気温を出す() -> None:
    assert lcd_line(TalkKind.NIGHT, _forecast(t_max=18, t_min=9)) == "ｱｼﾀ ﾊﾚ 18/9"


def test_LCDは朝なら今日と天気と気温を出す() -> None:
    assert lcd_line(TalkKind.MORNING, _forecast(Condition.RAIN, 12, 7)) == "ｷｮｳ ｱﾒ 12/7"


def test_LCDは氷点下を出す() -> None:
    assert lcd_line(TalkKind.MORNING, _forecast(Condition.SNOW, -1, -8)) == "ｷｮｳ ﾕｷ -1/-8"


def test_LCDは分からない天気の言葉を省く() -> None:
    assert lcd_line(TalkKind.NIGHT, _forecast(Condition.UNKNOWN, 18, 9)) == "ｱｼﾀ 18/9"


def test_LCDは16セルに収まり出せない文字がない() -> None:
    for kind in TalkKind:
        for condition in Condition:
            for t_max, t_min in ((18, 9), (-10, -15), (-50, -50), (45, 30)):
                line = lcd_line(kind, _forecast(condition, t_max, t_min))
                codes = to_lcd_codes(line)
                assert len(codes) <= LCD_WIDTH, line
                assert ord("?") not in codes, line


def test_LCDはありえないほど長い気温でも16セルを超えない() -> None:
    line = lcd_line(TalkKind.MORNING, _forecast(Condition.THUNDER, -100, -100))
    assert len(to_lcd_codes(line)) <= LCD_WIDTH
