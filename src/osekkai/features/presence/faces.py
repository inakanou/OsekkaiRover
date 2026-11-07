"""顔の特徴の帳面と投票（2.1〜2.4、5.4、5.6、5.7、6.1、6.4）。

帳面は、人ごとの名前（識別子）・呼び名・見本の特徴を持つ。
比べ方は、人ごとの見本の最大のコサインで、しきい値以上で最も高い人を選ぶ。
保存は基盤の保存先の `faces.json` に原子的に書く。顔の画像は持たない。
名前・呼び名・特徴は、ログに出さない（6.3）。ログには人数と決まった理由だけを出す。
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from osekkai.core.storage import JsonValue, StateStore

logger = logging.getLogger(__name__)

Embedding = tuple[float, ...]  # 128 個、長さ 1 にそろえたもの

EMBEDDING_SIZE: Final = 128
STORE_KEY: Final = "faces"
FORMAT_VERSION: Final = 1
VOTE_WINDOW: Final = 3  # 直近の 3 回
VOTE_NEEDED: Final = 2  # のうち 2 回

PERSON_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")

# 誤りの文言には、値を入れない
MSG_EMBEDDING_SIZE = f"顔の特徴は {EMBEDDING_SIZE} 個の数にしてください"
MSG_EMBEDDING_VALUE = "顔の特徴に使えない数があります"
MSG_EMBEDDING_ZERO = "顔の特徴がすべて 0 です"
MSG_PERSON_ID = "名前は a-z で始まり、a-z 0-9 _ だけの 32 文字までにしてください"
MSG_CALL_NAME = "呼び名がありません"
MSG_SAMPLES = "顔の特徴が 1 つもありません"


def normalize(values: Sequence[object]) -> Embedding:
    """特徴を長さ 1 にそろえる。数が 128 個でない・使えない数・すべて 0 なら `ValueError`。"""
    if len(values) != EMBEDDING_SIZE:
        raise ValueError(MSG_EMBEDDING_SIZE)
    floats: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(MSG_EMBEDDING_VALUE)
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(MSG_EMBEDDING_VALUE)
        floats.append(number)
    norm = math.sqrt(math.fsum(v * v for v in floats))
    if not math.isfinite(norm):
        raise ValueError(MSG_EMBEDDING_VALUE)
    if norm == 0.0:
        raise ValueError(MSG_EMBEDDING_ZERO)
    return tuple(v / norm for v in floats)


def _dot(a: Embedding, b: Embedding) -> float:
    return math.fsum(x * y for x, y in zip(a, b, strict=True))


@dataclass(frozen=True)
class Person:
    """覚えた 1 人。見本は作るときに長さ 1 にそろえる。"""

    person_id: str  # [a-z][a-z0-9_]*、32 文字まで
    call_name: str
    samples: tuple[Embedding, ...]

    def __post_init__(self) -> None:
        if not PERSON_ID.fullmatch(self.person_id):
            raise ValueError(MSG_PERSON_ID)
        if not self.call_name.strip():
            raise ValueError(MSG_CALL_NAME)
        if not self.samples:
            raise ValueError(MSG_SAMPLES)
        object.__setattr__(self, "samples", tuple(normalize(s) for s in self.samples))

    def best_cosine(self, emb: Embedding) -> float:
        """見本のうち、最も似たもののコサイン。`emb` は長さ 1 にそろえたもの。"""
        return max(_dot(sample, emb) for sample in self.samples)


class _Unreadable(Exception):
    """保存の形がおかしい。理由は決まった文言だけ。"""


class FaceBook:
    """覚えた人の帳面。"""

    def __init__(
        self, store: StateStore, people: Iterable[Person] = (), *, readable: bool = True
    ) -> None:
        self._store = store
        self._people: dict[str, Person] = {p.person_id: p for p in people}
        self._readable = readable

    @classmethod
    def load(cls, store: StateStore) -> FaceBook:
        """読む。版が違う・壊れている・形がおかしいときは空で、読めなかったことを覚える（6.4）。

        基盤の `StateStore.load` は壊れた JSON を `faces.corrupt-*.json` に退避して None を返す。
        None のときは、退避したファイルがあるかで壊れていたかを見分ける。
        """
        data = store.load(STORE_KEY)
        if data is None:
            if any(store.directory.glob(f"{STORE_KEY}.corrupt-*.json")):
                logger.warning("覚えた顔の保存が壊れていたので使いません")
                return cls(store, readable=False)
            return cls(store)
        try:
            people = _parse(data)
        except _Unreadable as e:
            logger.warning("覚えた顔の保存を読めないので使いません: %s", e)
            return cls(store, readable=False)
        logger.info("覚えた顔を読みました: %d 人", len(people))
        return cls(store, people)

    @property
    def readable(self) -> bool:
        """保存が読めたか。読めないものが残っていれば False（保存し直すと True）。"""
        return self._readable

    def people(self) -> tuple[Person, ...]:
        """覚えた人（名前の順）。"""
        return tuple(self._people[pid] for pid in sorted(self._people))

    def best_match(self, emb: Embedding, threshold: float) -> str | None:
        """最も似た人の名前。しきい値に届く人がいなければ None（2.3、2.4）。

        似かたが同じなら、名前の順で先の人にする。
        """
        target = normalize(emb)
        best_id: str | None = None
        best_score = -math.inf
        for person in self.people():
            score = person.best_cosine(target)
            if score >= threshold and score > best_score:
                best_id = person.person_id
                best_score = score
        return best_id

    def put(self, person: Person) -> None:
        """人を足す。同じ名前の人は置き換える（5.4）。"""
        self._people[person.person_id] = person

    def remove(self, person_id: str) -> bool:
        """1 人を消す。いなければ False（5.6）。"""
        return self._people.pop(person_id, None) is not None

    def clear(self) -> None:
        """すべて消す（5.7）。"""
        self._people.clear()

    def save(self) -> None:
        """保存先に原子的に書く（6.1）。失敗したら例外を投げ、前の内容は残る。"""
        people: dict[str, JsonValue] = {}
        for person in self.people():
            samples: list[JsonValue] = [list(s) for s in person.samples]
            people[person.person_id] = {"call_name": person.call_name, "samples": samples}
        self._store.save(STORE_KEY, {"version": FORMAT_VERSION, "people": people})
        self._readable = True
        logger.info("覚えた顔を保存しました: %d 人", len(people))


def _parse(data: dict[str, JsonValue]) -> list[Person]:
    if data.get("version") != FORMAT_VERSION or isinstance(data.get("version"), bool):
        raise _Unreadable("版が違います")
    raw_people = data.get("people")
    if not isinstance(raw_people, dict):
        raise _Unreadable("形がおかしいです")
    people: list[Person] = []
    for person_id, entry in raw_people.items():
        if not isinstance(entry, dict):
            raise _Unreadable("形がおかしいです")
        call_name = entry.get("call_name")
        raw_samples = entry.get("samples")
        if not isinstance(call_name, str) or not isinstance(raw_samples, list):
            raise _Unreadable("形がおかしいです")
        samples: list[Embedding] = []
        for raw in raw_samples:
            if not isinstance(raw, list):
                raise _Unreadable("形がおかしいです")
            try:
                samples.append(normalize(raw))
            except ValueError:
                raise _Unreadable("顔の特徴の形がおかしいです") from None
        try:
            people.append(Person(person_id, call_name, tuple(samples)))
        except ValueError:
            raise _Unreadable("形がおかしいです") from None
    return people


class Voting:
    """直近の 3 回のうち 2 回が同じ人なら、その人に決める（2.2）。知らない人（None）も 1 票。"""

    def __init__(self) -> None:
        self._votes: deque[str | None] = deque(maxlen=VOTE_WINDOW)

    def add(self, who: str | None) -> str | None:
        """1 票足して、決まった人を返す。決まらないか、知らない人が多ければ None。"""
        self._votes.append(who)
        top, count = Counter(self._votes).most_common(1)[0]
        if count >= VOTE_NEEDED:
            return top
        return None

    def reset(self) -> None:
        self._votes.clear()
