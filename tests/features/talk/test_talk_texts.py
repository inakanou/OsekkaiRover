"""朝と夜のお話の決まった言葉と LCD の文字（5.7、5.8、5.9、7.1、7.2、7.6、7.7）。"""

from osekkai.core.lcd_text import to_lcd_codes
from osekkai.features.talk import texts
from osekkai.features.talk.model import AlertKind, Condition, TalkKind

# 子どもを怖がらせる言葉。お話のどの言葉にも入れない（5.7）
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


def _all_phrases() -> list[str]:
    phrases = [
        *texts.OPENERS.values(),
        *texts.CLOSERS.values(),
        *texts.FALLBACKS.values(),
        *texts.DAY_WORDS.values(),
        *texts.CONDITION_WORDS.values(),
        *texts.ALERT_WORDS.values(),
        texts.SAY_CONDITION,
        texts.SAY_TEMPERATURE,
        texts.SAY_UMBRELLA,
        texts.SAY_COAT,
        texts.SAY_WATER,
        texts.SAY_ALERT,
        texts.ALERT_CLOSE,
    ]
    return phrases


def test_始まりと終わりと代わりの言葉が夜と朝の両方にある() -> None:
    for table in (texts.OPENERS, texts.CLOSERS, texts.FALLBACKS):
        assert set(table) == set(TalkKind)
    assert texts.FALLBACKS[TalkKind.NIGHT] == texts.SAY_FALLBACK_NIGHT
    assert texts.FALLBACKS[TalkKind.MORNING] == texts.SAY_FALLBACK_MORNING


def test_始まりと終わりの言葉は要件の例に沿う() -> None:
    assert "おやすみの まえの おはなし だよ" in texts.OPENERS[TalkKind.NIGHT]
    assert "きょうの おはなし だよ" in texts.OPENERS[TalkKind.MORNING]
    assert "あしたも たのしみだね" in texts.CLOSERS[TalkKind.NIGHT]
    assert "きょうも いちにち がんばろうね" in texts.CLOSERS[TalkKind.MORNING]


def test_始まりの言葉は呼び名の差し込み位置を持つ() -> None:
    for kind in TalkKind:
        assert texts.NAME_SLOT in texts.OPENERS[kind]


def test_代わりの言葉は要件の決まった短い言葉() -> None:
    assert texts.SAY_FALLBACK_NIGHT == "きょうも たのしかったね。おやすみ なさい"
    assert texts.SAY_FALLBACK_MORNING == "おはよう。きょうも いちにち たのしもうね"


def test_呼び名を差し込む() -> None:
    assert texts.fill_name("{name}、おはよう", "はると") == "はると、おはよう"


def test_呼び名がなければきみを入れる() -> None:
    assert texts.fill_name("{name}、おはよう", "") == "きみ、おはよう"
    assert texts.fill_name("{name}、おはよう", "   ") == "きみ、おはよう"


def test_呼び名の差し込みはほかの波かっこを変えない() -> None:
    assert texts.fill_name("{name} {x}", "はると") == "はると {x}"


def test_天気の言葉はすべての天気の種類にある_分からないは除く() -> None:
    known = set(Condition) - {Condition.UNKNOWN}
    assert set(texts.CONDITION_WORDS) == known
    assert set(texts.LCD_CONDITIONS) == known
    assert texts.CONDITION_WORDS[Condition.SUNNY] == "はれ"
    assert texts.CONDITION_WORDS[Condition.CLOUDY] == "くもり"
    assert texts.CONDITION_WORDS[Condition.RAIN] == "あめ"
    assert texts.CONDITION_WORDS[Condition.SNOW] == "ゆき"
    assert texts.CONDITION_WORDS[Condition.THUNDER] == "かみなり"


def test_LCDの天気は半角カタカナ() -> None:
    assert texts.LCD_CONDITIONS[Condition.SUNNY] == "ﾊﾚ"
    assert texts.LCD_CONDITIONS[Condition.CLOUDY] == "ｸﾓﾘ"
    assert texts.LCD_CONDITIONS[Condition.RAIN] == "ｱﾒ"
    assert texts.LCD_CONDITIONS[Condition.SNOW] == "ﾕｷ"
    assert texts.LCD_CONDITIONS[Condition.THUNDER] == "ｶﾐﾅﾘ"
    assert texts.LCD_DAYS[TalkKind.NIGHT] == "ｱｼﾀ"
    assert texts.LCD_DAYS[TalkKind.MORNING] == "ｷｮｳ"
    assert texts.LCD_TALK == "ｵﾊﾅｼ"


def test_夜はあしたは_朝はきょうはで始める() -> None:
    assert texts.DAY_WORDS[TalkKind.NIGHT] == "あしたは"
    assert texts.DAY_WORDS[TalkKind.MORNING] == "きょうは"


def test_注意報の言葉はすべての種類にあり家の大人に任せる言葉で結ぶ() -> None:
    assert set(texts.ALERT_WORDS) == set(AlertKind)
    assert set(texts.ALERT_ORDER) == set(AlertKind)
    assert len(texts.ALERT_ORDER) == len(AlertKind)
    assert texts.ALERT_CLOSE == "おうちの ひとと いっしょに きを つけようね"
    assert texts.ALERT_CLOSE in texts.SAY_ALERT


def test_天気の文の部品は差し込みで組み立てられる() -> None:
    sentence = texts.SAY_CONDITION.format(
        day=texts.DAY_WORDS[TalkKind.NIGHT], condition=texts.CONDITION_WORDS[Condition.SUNNY]
    )
    assert sentence == "あしたは はれ だよ"
    temps = texts.SAY_TEMPERATURE.format(t_max=18, t_min=9)
    assert "18ど" in temps and "9ど" in temps
    hazards = texts.ALERT_SEPARATOR.join(
        texts.ALERT_WORDS[k] for k in (AlertKind.HEAVY_RAIN, AlertKind.STRONG_WIND)
    )
    alert = texts.SAY_ALERT.format(day=texts.DAY_WORDS[TalkKind.MORNING], hazards=hazards)
    assert "あめが たくさん" in alert and "かぜが つよく" in alert


def test_どの言葉にも怖い言葉がない() -> None:
    for phrase in _all_phrases():
        for word in SCARY_WORDS:
            assert word not in phrase, (phrase, word)


def test_LCDの文字は16セルに収まる() -> None:
    assert len(to_lcd_codes(texts.LCD_TALK)) <= LCD_WIDTH
    longest_day = max(texts.LCD_DAYS.values(), key=lambda s: len(to_lcd_codes(s)))
    longest_condition = max(texts.LCD_CONDITIONS.values(), key=lambda s: len(to_lcd_codes(s)))
    # いちばん長い組み合わせ（例: ｱｼﾀ ｶﾐﾅﾘ -20/-20）でも 16 セルに収まる
    worst = f"{longest_day} {longest_condition} -20/-20"
    assert len(to_lcd_codes(worst)) <= LCD_WIDTH
    for text in (*texts.LCD_DAYS.values(), *texts.LCD_CONDITIONS.values()):
        assert ord("?") not in to_lcd_codes(text)  # LCD に出せない文字がない
