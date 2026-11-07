"""顔の特徴の帳面と投票（2.1〜2.4、5.4、5.6、5.7、6.1、6.4）。"""

import json
import logging
import math
from pathlib import Path

import pytest

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.storage import StateStore
from osekkai.features.presence.faces import (
    EMBEDDING_SIZE,
    FaceBook,
    Person,
    Voting,
    normalize,
)

# ログに出てはいけない、目立つ名前と呼び名
HIDDEN_ID = "zzhiddenid"
HIDDEN_NAME = "ひみつのよびな"


def _axis(i: int, scale: float = 1.0) -> tuple[float, ...]:
    values = [0.0] * EMBEDDING_SIZE
    values[i] = scale
    return tuple(values)


def _mix(cos: float) -> tuple[float, ...]:
    """軸 0 とのコサインが `cos` になる長さ 1 の特徴。"""
    values = [0.0] * EMBEDDING_SIZE
    values[0] = cos
    values[1] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return tuple(values)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "presence", FakeClock())


def _book(store: StateStore, *people: Person) -> FaceBook:
    book = FaceBook.load(store)
    for person in people:
        book.put(person)
    return book


# --- 特徴と人の形 ---


def test_特徴は長さ1にそろえる() -> None:
    emb = normalize(_axis(3, scale=5.0))
    assert emb[3] == pytest.approx(1.0)
    assert sum(v * v for v in emb) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "values",
    [
        (1.0,) * (EMBEDDING_SIZE - 1),
        (1.0,) * (EMBEDDING_SIZE + 1),
        (math.nan,) + (1.0,) * (EMBEDDING_SIZE - 1),
        (math.inf,) + (1.0,) * (EMBEDDING_SIZE - 1),
        (0.0,) * EMBEDDING_SIZE,
    ],
)
def test_形の誤った特徴は断る(values: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        normalize(values)


def test_人の見本は長さ1にそろえて持つ() -> None:
    person = Person("taro", "たろう", (_axis(0, scale=3.0),))
    assert person.samples[0][0] == pytest.approx(1.0)


@pytest.mark.parametrize("person_id", ["", "Taro", "1taro", "ta-ro", "a" * 33, "たろう"])
def test_使えない名前は断る(person_id: str) -> None:
    with pytest.raises(ValueError) as info:
        Person(person_id, "たろう", (_axis(0),))
    if person_id:
        assert person_id not in str(info.value)


def test_32文字までの名前は使える() -> None:
    assert Person("a" + "b_9" * 10 + "c", "たろう", (_axis(0),)).person_id.startswith("a")


def test_呼び名がないか見本がないと断る() -> None:
    with pytest.raises(ValueError):
        Person("taro", "", (_axis(0),))
    with pytest.raises(ValueError):
        Person("taro", "   ", (_axis(0),))
    with pytest.raises(ValueError):
        Person("taro", "たろう", ())


# --- 比べ方（2.3、2.4） ---


def test_しきい値ちょうどは同じ人_少し下は知らない人(store: StateStore) -> None:
    book = _book(store, Person("taro", "たろう", (_axis(0),)))
    assert book.best_match(_mix(0.5), 0.5) == "taro"
    assert book.best_match(_mix(0.49), 0.5) is None


def test_見本のうち最も似たもので比べる(store: StateStore) -> None:
    book = _book(store, Person("taro", "たろう", (_axis(5), _axis(1), _axis(0))))
    assert book.best_match(_mix(0.9), 0.8) == "taro"


def test_何人もいるとき最も似た人を選ぶ(store: StateStore) -> None:
    book = _book(
        store,
        Person("hanako", "はなこ", (_mix(0.6),)),
        Person("taro", "たろう", (_mix(0.95),)),
        Person("jiro", "じろう", (_axis(7),)),
    )
    assert book.best_match(_axis(0), 0.5) == "taro"
    assert book.best_match(_axis(7, scale=2.0), 0.5) == "jiro"
    assert book.best_match(_axis(9), 0.5) is None


def test_同じ似かたなら名前の順で先の人(store: StateStore) -> None:
    book = _book(
        store,
        Person("taro", "たろう", (_axis(0),)),
        Person("hanako", "はなこ", (_axis(0),)),
    )
    assert book.best_match(_axis(0), 0.5) == "hanako"


def test_覚えた人がいなければ知らない人(store: StateStore) -> None:
    assert FaceBook.load(store).best_match(_axis(0), 0.0) is None


def test_比べる特徴の形が誤っていれば断る(store: StateStore) -> None:
    book = _book(store, Person("taro", "たろう", (_axis(0),)))
    with pytest.raises(ValueError):
        book.best_match((1.0,), 0.5)


# --- 置き換え・消す・保存（5.4、5.6、5.7、6.1） ---


def test_同じ名前は置き換える(store: StateStore) -> None:
    book = _book(store, Person("taro", "たろう", (_axis(0), _axis(1))))
    book.put(Person("taro", "たろちゃん", (_axis(2),)))
    (person,) = book.people()
    assert person.call_name == "たろちゃん"
    assert len(person.samples) == 1
    assert book.best_match(_axis(0), 0.5) is None
    assert book.best_match(_axis(2), 0.5) == "taro"


def test_人の一覧は名前の順(store: StateStore) -> None:
    book = _book(
        store,
        Person("taro", "たろう", (_axis(0),)),
        Person("hanako", "はなこ", (_axis(1),)),
    )
    assert [p.person_id for p in book.people()] == ["hanako", "taro"]


def test_保存して読み直すと同じ人が戻る(store: StateStore) -> None:
    book = _book(
        store,
        Person("taro", "たろう", (_axis(0), _mix(0.3))),
        Person("hanako", "はなこ", (_axis(1),)),
    )
    book.save()
    data = json.loads((store.directory / "faces.json").read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert set(data["people"]) == {"taro", "hanako"}
    assert data["people"]["taro"]["call_name"] == "たろう"
    assert len(data["people"]["taro"]["samples"]) == 2
    assert len(data["people"]["taro"]["samples"][0]) == EMBEDDING_SIZE

    again = FaceBook.load(store)
    assert again.readable
    assert again.people() == book.people()
    assert again.best_match(_axis(1), 0.5) == "hanako"


def test_1人を消して保存する(store: StateStore) -> None:
    book = _book(
        store,
        Person("taro", "たろう", (_axis(0),)),
        Person("hanako", "はなこ", (_axis(1),)),
    )
    book.save()
    assert book.remove("taro") is True
    assert book.remove("taro") is False
    assert book.remove("nobody") is False
    book.save()
    again = FaceBook.load(store)
    assert [p.person_id for p in again.people()] == ["hanako"]
    assert again.best_match(_axis(0), 0.5) is None


def test_すべて消して保存する(store: StateStore) -> None:
    book = _book(store, Person("taro", "たろう", (_axis(0),)))
    book.save()
    book.clear()
    book.save()
    again = FaceBook.load(store)
    assert again.readable
    assert again.people() == ()


def test_保存がなければ空で読める(store: StateStore) -> None:
    book = FaceBook.load(store)
    assert book.readable
    assert book.people() == ()


# --- 壊れた保存（6.4） ---


def _write(store: StateStore, text: str) -> None:
    store.directory.mkdir(parents=True, exist_ok=True)
    (store.directory / "faces.json").write_text(text, encoding="utf-8")


def test_壊れたJSONは空で読めなかったことを覚える(store: StateStore) -> None:
    _write(store, "{こわれた")
    book = FaceBook.load(store)
    assert not book.readable
    assert book.people() == ()
    assert book.best_match(_axis(0), 0.0) is None
    # 基盤が退避している
    assert not (store.directory / "faces.json").exists()
    assert list(store.directory.glob("faces.corrupt-*.json"))
    # 次に読んでも、退避したファイルが残っている間は読めなかったことにする
    assert not FaceBook.load(store).readable


def test_退避の後に保存し直せば読める(store: StateStore) -> None:
    _write(store, "[]")
    book = FaceBook.load(store)
    readable_before = book.readable
    assert not readable_before
    book.put(Person("taro", "たろう", (_axis(0),)))
    book.save()
    assert book.readable
    again = FaceBook.load(store)
    assert again.readable
    assert [p.person_id for p in again.people()] == ["taro"]


def test_ほかの鍵の退避ファイルは関係ない(store: StateStore) -> None:
    store.directory.mkdir(parents=True, exist_ok=True)
    (store.directory / "greetings.corrupt-20260101T000000.json").write_text("x", encoding="utf-8")
    assert FaceBook.load(store).readable


def _valid_people() -> dict[str, object]:
    return {"taro": {"call_name": "たろう", "samples": [list(_axis(0))]}}


def test_版が違えば空で読めなかったことにする(store: StateStore) -> None:
    _write(store, json.dumps({"version": 2, "people": _valid_people()}))
    book = FaceBook.load(store)
    assert not book.readable
    assert book.people() == ()
    # 版の違うファイルは消さない
    assert (store.directory / "faces.json").exists()


def test_版がないときも読めなかったことにする(store: StateStore) -> None:
    _write(store, json.dumps({"people": _valid_people()}))
    assert not FaceBook.load(store).readable


def test_正しい形なら読める(store: StateStore) -> None:
    _write(store, json.dumps({"version": 1, "people": _valid_people()}))
    book = FaceBook.load(store)
    assert book.readable
    assert book.best_match(_axis(0), 0.5) == "taro"


@pytest.mark.parametrize(
    "people",
    [
        [],
        {"Taro": {"call_name": "たろう", "samples": [list(_axis(0))]}},
        {"taro": []},
        {"taro": {"samples": [list(_axis(0))]}},
        {"taro": {"call_name": "", "samples": [list(_axis(0))]}},
        {"taro": {"call_name": 3, "samples": [list(_axis(0))]}},
        {"taro": {"call_name": "たろう"}},
        {"taro": {"call_name": "たろう", "samples": []}},
        {"taro": {"call_name": "たろう", "samples": [[1.0, 0.0]]}},
        {"taro": {"call_name": "たろう", "samples": [["a"] * EMBEDDING_SIZE]}},
        {"taro": {"call_name": "たろう", "samples": [[True] * EMBEDDING_SIZE]}},
        {"taro": {"call_name": "たろう", "samples": [[0.0] * EMBEDDING_SIZE]}},
        {"taro": {"call_name": "たろう", "samples": list(_axis(0))}},
    ],
)
def test_形のおかしい中身は空で読めなかったことにする(store: StateStore, people: object) -> None:
    _write(store, json.dumps({"version": 1, "people": people}))
    book = FaceBook.load(store)
    assert not book.readable
    assert book.people() == ()


def test_1人でも形がおかしければ全員使わない(store: StateStore) -> None:
    people = {**_valid_people(), "hanako": {"call_name": "はなこ", "samples": [[1.0]]}}
    _write(store, json.dumps({"version": 1, "people": people}))
    book = FaceBook.load(store)
    assert not book.readable
    assert book.best_match(_axis(0), 0.5) is None


def test_整数の数も特徴として読める(store: StateStore) -> None:
    sample = [0] * EMBEDDING_SIZE
    sample[0] = 2
    _write(
        store,
        json.dumps(
            {"version": 1, "people": {"taro": {"call_name": "たろう", "samples": [sample]}}}
        ),
    )
    book = FaceBook.load(store)
    assert book.readable
    assert book.people()[0].samples[0][0] == pytest.approx(1.0)


# --- ログ（6.3） ---


def test_ログに名前と呼び名と特徴を出さない(
    store: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        book = _book(store, Person(HIDDEN_ID, HIDDEN_NAME, (_mix(0.123456789),)))
        book.save()
        FaceBook.load(store)
        book.best_match(_axis(0), 0.1)
        book.remove(HIDDEN_ID)
        book.save()
        _write(store, json.dumps({"version": 1, "people": {HIDDEN_ID: {"call_name": HIDDEN_NAME}}}))
        FaceBook.load(store)
        _write(store, json.dumps({"version": 9, "people": {}}))
        FaceBook.load(store)
        _write(store, "{" + HIDDEN_NAME)
        FaceBook.load(store)
    assert caplog.records
    text = caplog.text
    assert HIDDEN_ID not in text
    assert HIDDEN_NAME not in text
    assert "0.12345" not in text


# --- 投票（2.2） ---


def test_3回のうち2回同じ人なら決める() -> None:
    voting = Voting()
    assert voting.add("taro") is None
    assert voting.add("taro") == "taro"


def test_ばらばらなら決めない() -> None:
    voting = Voting()
    assert voting.add("taro") is None
    assert voting.add("hanako") is None
    assert voting.add(None) is None


def test_知らない人も1票になる() -> None:
    voting = Voting()
    assert voting.add(None) is None
    assert voting.add("taro") is None
    # 知らない人が 2 票でも、決めた人はいない
    assert voting.add(None) is None
    # 直近は taro・None・taro
    assert voting.add("taro") == "taro"


def test_直近の3回だけを見る() -> None:
    voting = Voting()
    voting.add("taro")
    assert voting.add("taro") == "taro"
    assert voting.add("hanako") == "taro"
    assert voting.add("hanako") == "hanako"
    assert voting.add(None) == "hanako"
    assert voting.add(None) is None


def test_やり直すと前の票を忘れる() -> None:
    voting = Voting()
    voting.add("taro")
    voting.reset()
    assert voting.add("taro") is None
    assert voting.add("taro") == "taro"
