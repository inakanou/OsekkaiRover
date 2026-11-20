"""好きなものの選び方と、豆知識の頼み方（6.1〜6.5、9.1、9.3）。

記録には好きなものの文字を残さず、短い hash だけを残す。外部の会話の AI に送るのは、
選んだ好きなもの・夜か朝か・月と季節だけ。呼び名・現在地・天気は受け取りもしない。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date
from random import Random
from typing import Final

from osekkai.features.conversation.api import KID_RULES, Turn
from osekkai.features.talk import texts
from osekkai.features.talk.model import TalkKind

HASH_LENGTH: Final = 16  # 記録に残す hash の長さ（sha256 の先頭）


def topic_hash(topic: str) -> str:
    """好きなものの sha256 の先頭 16 文字。記録に好きなものの文字を残さないため。"""
    return hashlib.sha256(topic.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def choose_topic(topics: Sequence[str], last_hash: str | None, rng: Random) -> str | None:
    """好きなものを 1 つ選ぶ（6.1）。2 つ以上なら直前と同じものを除く（6.2）。空なら None。"""
    if not topics:
        return None
    candidates = list(topics)
    if len(topics) >= 2:
        others = [topic for topic in topics if topic_hash(topic) != last_hash]
        # 全部が直前と同じ（同じ言葉の重なり）なら、除かずに選ぶ
        candidates = others or candidates
    return rng.choice(candidates)


def season_word(month: int) -> str:
    """月から季節の言葉。3〜5 はる、6〜8 なつ、9〜11 あき、12〜2 ふゆ。"""
    if 3 <= month <= 5:
        return texts.SEASON_SPRING
    if 6 <= month <= 8:
        return texts.SEASON_SUMMER
    if 9 <= month <= 11:
        return texts.SEASON_AUTUMN
    return texts.SEASON_WINTER


def fact_system(kind: TalkKind) -> str:
    """豆知識の決まりの文（6.3、6.5）と子ども向けの決まり（6.4）を組み合わせた system。"""
    return (
        texts.FACT_RULES
        + "\n"
        + texts.FACT_KIND_HEADING
        + texts.FACT_KIND_RULES[kind]
        + "\n"
        + texts.KID_RULES_HEADING
        + KID_RULES
    )


def fact_request(kind: TalkKind, topic: str, today: date) -> tuple[str, list[Turn]]:
    """豆知識を頼む system とやりとり（9.3）。送るのは好きなもの・夜か朝か・月と季節だけ。"""
    user = texts.FACT_USER.format(
        topic=topic,
        time=texts.FACT_TIME_WORDS[kind],
        month=today.month,
        season=season_word(today.month),
    )
    return fact_system(kind), [Turn(role="user", text=user)]
