"""言葉の正規化・判定・登録表（2.1〜2.4、2.6、2.7、3.1、3.2）。

登録表は、機能が受け付ける語句と、当てはまらない言葉の受け手を持つ。
判定のしかた（IntentMatcher）は差し替えられる。差し替えても登録の口は変わらない。
ログには機能の名前だけを出す。言葉と語句は出さない。
"""

from __future__ import annotations

import logging
import threading
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

MSG_NO_PHRASES = "語句が 1 つもありません"
MSG_EMPTY_PHRASE = "空の語句があります（空白・句読点・記号だけの語句は使えません）"

# カタカナ（ァ〜ヶ）をひらがなに移す差
_KATAKANA_FIRST = 0x30A1
_KATAKANA_LAST = 0x30F6
_KANA_OFFSET = 0x60
_LONG_VOWEL = "ー"
# 除く文字の種類: 区切り（空白）・句読点・記号・制御文字など
_DROP_CATEGORIES = ("Z", "P", "S", "C")


def normalize(text: str) -> str:
    """言葉を比べやすい形にする（2.3）。

    NFKC で全角と半角をそろえ、カタカナをひらがなにし、
    空白・句読点・記号と、長音の「ー」を除く。子どもが「だるまさーん」とのばしても、
    「だるまさん」に一致する（語句も同じように直すので、比べ方はそろう）。
    """
    out: list[str] = []
    for char in unicodedata.normalize("NFKC", text):
        code = ord(char)
        if _KATAKANA_FIRST <= code <= _KATAKANA_LAST:
            out.append(chr(code - _KANA_OFFSET))
            continue
        if char == _LONG_VOWEL or unicodedata.category(char).startswith(_DROP_CATEGORIES):
            continue
        out.append(char)
    return "".join(out)


@dataclass(frozen=True)
class Heard:
    """機能に渡す聞き取った言葉。"""

    text: str  # 文字にした言葉（受け取った機能だけが使う。ログに出さない）
    follow_up: bool  # 続けて聞いた言葉なら True


Handler = Callable[[Heard], None]  # すぐに戻る（自分のスレッドに仕事を渡す）


@dataclass(frozen=True)
class WordsRegistration:
    """語句の登録。"""

    owner: str
    priority: int
    phrases: tuple[str, ...]
    handler: Handler
    alive: Callable[[], bool]
    order: int  # 登録の順（小さいほど先）


@dataclass(frozen=True)
class FallbackRegistration:
    """当てはまらない言葉の受け手の登録。"""

    owner: str
    handler: Handler
    alive: Callable[[], bool]
    order: int


class FallbackTaken(Exception):
    """受け手はすでに登録されている（3.2）。"""


class IntentMatcher(Protocol):
    """振り分けの判定のしかた（2.7）。"""

    def match(
        self, text: str, candidates: Sequence[WordsRegistration]
    ) -> WordsRegistration | None: ...


class KeywordMatcher:
    """正規化した語句を含む登録を選ぶ（2.2〜2.4）。

    優先度の高い順、同じなら登録の順で、初めに一致したものを選ぶ。
    """

    def match(self, text: str, candidates: Sequence[WordsRegistration]) -> WordsRegistration | None:
        heard = normalize(text)
        for registration in sorted(candidates, key=lambda r: (-r.priority, r.order)):
            if any(normalize(phrase) in heard for phrase in registration.phrases):
                return registration
        return None


@dataclass(frozen=True)
class Words:
    """語句の登録に振り分けた。"""

    registration: WordsRegistration


@dataclass(frozen=True)
class Fallback:
    """受け手に振り分けた。"""

    registration: FallbackRegistration


@dataclass(frozen=True)
class Nobody:
    """渡す先がない。"""


Route = Words | Fallback | Nobody


class IntentRegistry:
    """語句と受け手の登録表。ロックで守る。

    `alive()` が偽の登録は、判定やヒントのときに表から外す（2.6）。
    """

    def __init__(self, *, matcher: IntentMatcher | None = None) -> None:
        self._matcher: IntentMatcher = matcher if matcher is not None else KeywordMatcher()
        self._lock = threading.Lock()
        self._words: list[WordsRegistration] = []
        self._fallback: FallbackRegistration | None = None
        self._follow_up: str | None = None
        self._next_order = 0

    def add_words(
        self,
        owner: str,
        priority: int,
        phrases: Sequence[str],
        handler: Handler,
        *,
        alive: Callable[[], bool],
    ) -> WordsRegistration:
        """語句を登録する（2.1）。語句なし・空の語句は ValueError。"""
        if not phrases:
            raise ValueError(MSG_NO_PHRASES)
        if any(not normalize(phrase) for phrase in phrases):
            raise ValueError(MSG_EMPTY_PHRASE)
        with self._lock:
            registration = WordsRegistration(
                owner=owner,
                priority=priority,
                phrases=tuple(phrases),
                handler=handler,
                alive=alive,
                order=self._take_order(),
            )
            self._words.append(registration)
        logger.info("語句を登録しました: %s", owner)
        return registration

    def set_fallback(
        self, owner: str, handler: Handler, *, alive: Callable[[], bool]
    ) -> FallbackRegistration:
        """受け手を登録する（3.1）。生きている受け手があれば FallbackTaken（3.2）。"""
        with self._lock:
            current = self._live_fallback()
            if current is not None:
                logger.warning(
                    "受け手はすでに登録されているので断りました: %s（いまの受け手: %s）",
                    owner,
                    current.owner,
                )
                raise FallbackTaken(owner)
            registration = FallbackRegistration(
                owner=owner, handler=handler, alive=alive, order=self._take_order()
            )
            self._fallback = registration
        logger.info("受け手を登録しました: %s", owner)
        return registration

    def remove(self, registration: WordsRegistration | FallbackRegistration) -> None:
        """登録を外す。もう外れていれば何もしない。"""
        with self._lock:
            if isinstance(registration, WordsRegistration):
                self._words = [r for r in self._words if r is not registration]
            elif self._fallback is registration:
                self._fallback = None

    def route(self, text: str) -> Route:
        """言葉の渡す先を決める（2.2〜2.4、3.3、3.4）。"""
        with self._lock:
            candidates = self._live_words()
            fallback = self._live_fallback()
        chosen = self._matcher.match(text, candidates)
        if chosen is not None:
            return Words(chosen)
        if fallback is not None:
            return Fallback(fallback)
        return Nobody()

    def hints(self) -> tuple[str, ...]:
        """生きている登録の語句（聞き取りのヒントに足す）。"""
        with self._lock:
            return tuple(phrase for r in self._live_words() for phrase in r.phrases)

    def owner_alive(self, owner: str) -> bool:
        """その機能に生きている登録（語句か受け手）があるか。"""
        with self._lock:
            if any(r.owner == owner for r in self._live_words()):
                return True
            fallback = self._live_fallback()
            return fallback is not None and fallback.owner == owner

    def handler_for(self, owner: str) -> Handler | None:
        """その機能の生きている登録の口を返す（続けて聞いた言葉を渡すため。4.3）。

        語句の登録があれば先に登録したものの口、なければ受け手の口。どちらもなければ None。
        """
        with self._lock:
            for registration in self._live_words():
                if registration.owner == owner:
                    return registration.handler
            fallback = self._live_fallback()
            if fallback is not None and fallback.owner == owner:
                return fallback.handler
            return None

    def request_follow_up(self, owner: str) -> None:
        """続けて聞くを求めた機能の名前を記録する（4.1）。

        締め切りは持たない（入口が持つ）。生きている登録がない機能なら何もしない。
        """
        if not self.owner_alive(owner):
            logger.info("登録のない機能の続けて聞くは受けません: %s", owner)
            return
        with self._lock:
            self._follow_up = owner

    def take_follow_up_request(self) -> str | None:
        """続けて聞くを求めた機能の名前を取り出す。1 度取り出すと消える。"""
        with self._lock:
            owner, self._follow_up = self._follow_up, None
            return owner

    # --- 以下はロックを持って呼ぶ ---

    def _take_order(self) -> int:
        order = self._next_order
        self._next_order += 1
        return order

    def _live_words(self) -> list[WordsRegistration]:
        live: list[WordsRegistration] = []
        for registration in self._words:
            if registration.alive():
                live.append(registration)
            else:
                logger.info("止まった機能の語句を外しました: %s", registration.owner)
        self._words = live
        return list(live)

    def _live_fallback(self) -> FallbackRegistration | None:
        fallback = self._fallback
        if fallback is not None and not fallback.alive():
            logger.info("止まった機能の受け手を外しました: %s", fallback.owner)
            self._fallback = None
            return None
        return fallback
