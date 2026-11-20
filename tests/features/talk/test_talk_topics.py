"""好きなものの選び方と、豆知識の頼み方（6.1〜6.5、9.1、9.3）。"""

import hashlib
import re
from datetime import date
from random import Random

import pytest

from osekkai.features.conversation.api import KID_RULES, Turn
from osekkai.features.talk import texts
from osekkai.features.talk.config import Location, TalkConfig
from osekkai.features.talk.model import TalkKind
from osekkai.features.talk.topics import (
    choose_topic,
    fact_request,
    season_word,
    topic_hash,
)

# 送る言葉に入ってはいけない値（外へ出さない値）
HIDDEN_NAME = "みはるん"
HIDDEN_LATITUDE = 35.6789
HIDDEN_LONGITUDE = 139.7654
TOPICS = ("きょうりゅう", "でんしゃ", "うちゅう")
SEEDS = range(200)
TODAY = date(2026, 11, 20)


def _sent_text(system: str, turns: list[Turn]) -> str:
    return system + "\n".join(turn.text for turn in turns)


# --- 選び方 ---


def test_好きなものが2つ以上なら直前と同じものを続けない() -> None:
    for last in TOPICS:
        for seed in SEEDS:
            chosen = choose_topic(TOPICS, topic_hash(last), Random(seed))
            assert chosen is not None
            assert chosen in TOPICS
            assert chosen != last


def test_好きなものが2つのときは必ずもう一方を選ぶ() -> None:
    pair = ("きょうりゅう", "でんしゃ")
    for seed in SEEDS:
        assert choose_topic(pair, topic_hash("でんしゃ"), Random(seed)) == "きょうりゅう"


def test_直前と同じものを除いても乱数でいろいろ選ぶ() -> None:
    chosen = {choose_topic(TOPICS, topic_hash("うちゅう"), Random(seed)) for seed in SEEDS}
    assert chosen == {"きょうりゅう", "でんしゃ"}


def test_好きなものが1つなら直前と同じでも選ぶ() -> None:
    only = ("きょうりゅう",)
    for seed in range(20):
        assert choose_topic(only, topic_hash("きょうりゅう"), Random(seed)) == "きょうりゅう"


def test_好きなものが空なら選ばない() -> None:
    assert choose_topic((), None, Random(0)) is None
    assert choose_topic([], topic_hash("きょうりゅう"), Random(0)) is None


def test_直前がないか一覧にないときはどれでも選ぶ() -> None:
    for last in (None, topic_hash("ろけっと")):
        chosen = {choose_topic(TOPICS, last, Random(seed)) for seed in SEEDS}
        assert chosen == set(TOPICS)


def test_同じ言葉が重なっていて全部が直前と同じでも選ぶ() -> None:
    same = ("でんしゃ", "でんしゃ")
    assert choose_topic(same, topic_hash("でんしゃ"), Random(0)) == "でんしゃ"


def test_hashは16文字の16進で同じ言葉なら同じ() -> None:
    first = topic_hash("きょうりゅう")
    assert re.fullmatch(r"[0-9a-f]{16}", first)
    assert topic_hash("きょうりゅう") == first
    assert topic_hash("でんしゃ") != first
    # sha256 の先頭 16 文字（記録が版をまたいで同じ値になる）
    assert first == hashlib.sha256("きょうりゅう".encode()).hexdigest()[:16]
    assert "きょうりゅう" not in first


# --- 季節 ---


@pytest.mark.parametrize(
    ("month", "season"),
    [
        (1, "ふゆ"),
        (2, "ふゆ"),
        (3, "はる"),
        (5, "はる"),
        (6, "なつ"),
        (8, "なつ"),
        (9, "あき"),
        (11, "あき"),
        (12, "ふゆ"),
    ],
)
def test_季節は月から決める(month: int, season: str) -> None:
    assert season_word(month) == season
    _, turns = fact_request(TalkKind.MORNING, "でんしゃ", date(2026, month, 1))
    assert f"{month}がつ、{season}" in turns[0].text


# --- 頼み方 ---


def test_送る言葉は好きなもの_夜か朝か_月と季節だけ() -> None:
    _, turns = fact_request(TalkKind.NIGHT, "きょうりゅう", TODAY)
    assert turns == [Turn(role="user", text="すきな もの: きょうりゅう\nよる\n11がつ、あき")]
    _, turns = fact_request(TalkKind.MORNING, "でんしゃ", date(2026, 4, 3))
    assert turns == [Turn(role="user", text="すきな もの: でんしゃ\nあさ\n4がつ、はる")]


def test_決まりの文は子ども向けの決まりと豆知識の決まりを含む() -> None:
    for kind in TalkKind:
        system, _ = fact_request(kind, "きょうりゅう", TODAY)
        assert KID_RULES in system
        assert texts.FACT_RULES in system
        assert texts.KID_RULES_HEADING + KID_RULES in system
        assert "4 さい" in system
        assert "2 文以内" in system
        assert "本当のこと" in system
        assert "絵文字・記号" in system
        assert "名前で呼びかけない" in system


def test_夜は落ち着いた豆知識を頼み朝とは違う() -> None:
    night, night_turns = fact_request(TalkKind.NIGHT, "きょうりゅう", TODAY)
    morning, morning_turns = fact_request(TalkKind.MORNING, "きょうりゅう", TODAY)
    assert night != morning
    assert night_turns != morning_turns
    assert "落ち着いて" in night and "寝る前" in night
    assert "落ち着いて" not in morning and "寝る前" not in morning
    assert texts.FACT_KIND_RULES[TalkKind.NIGHT] in night
    assert texts.FACT_KIND_RULES[TalkKind.MORNING] in morning


def test_好きなものの波かっこも壊さずに送る() -> None:
    _, turns = fact_request(TalkKind.MORNING, "{x}ろぼ", TODAY)
    assert "すきな もの: {x}ろぼ" in turns[0].text


def test_呼び名と現在地と天気は送る言葉に入らない() -> None:
    config = TalkConfig(
        enabled=True,
        call_name=HIDDEN_NAME,
        location=Location(latitude=HIDDEN_LATITUDE, longitude=HIDDEN_LONGITUDE),
        topics=list(TOPICS),
    )
    weather_words = (*texts.CONDITION_WORDS.values(), "ど", "かさ", "うわぎ")
    for kind in TalkKind:
        for seed in range(20):
            topic = choose_topic(config.topics, None, Random(seed))
            assert topic is not None
            system, turns = fact_request(kind, topic, TODAY)
            sent = _sent_text(system, turns)
            assert HIDDEN_NAME not in sent
            assert str(HIDDEN_LATITUDE) not in sent
            assert str(HIDDEN_LONGITUDE) not in sent
            assert "35.6" not in sent and "139.7" not in sent
            user = "".join(t.text for t in turns)
            assert texts.DEFAULT_CALL_NAME not in user
            assert texts.NAME_SLOT not in user
            # 送るやりとりには天気の言葉が入らない
            for word in weather_words:
                assert word not in user, word
