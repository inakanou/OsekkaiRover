"""言葉の正規化・判定・登録表（2.1〜2.4、2.6、2.7、3.1、3.2）。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence

import pytest

from osekkai.voice.intents import (
    Fallback,
    FallbackTaken,
    Heard,
    IntentRegistry,
    KeywordMatcher,
    Nobody,
    Words,
    WordsRegistration,
    normalize,
)


def _handler(heard: Heard) -> None:
    del heard


def _alive() -> bool:
    return True


def _dead() -> bool:
    return False


class _Flag:
    """止める旗の代わり。"""

    def __init__(self) -> None:
        self.stopped = False

    def alive(self) -> bool:
        return not self.stopped


# --- 正規化 ---


def test_カタカナはひらがなになり_空白と句読点と記号は除かれる() -> None:
    assert normalize("ダルマさん しよう！") == "だるまさんしよう"


def test_全角と半角の違いは無視される() -> None:
    assert normalize("ﾀﾞﾙﾏｻﾝ") == normalize("ダルマサン") == "だるまさん"
    assert normalize("ＡＢＣ　１２３") == "ABC123"


def test_長音の記号は除かれ_のばした言い方も一致する() -> None:
    assert normalize("ローバー") == normalize("ﾛｰﾊﾞｰ") == "ろば"
    assert normalize("だるまさん") in normalize("だるまさーん しよう")


def test_句読点と記号はいろいろ除かれる() -> None:
    assert normalize("「あ、い。う！？・え…お」〜") == "あいうえお"


# --- 判定 ---


def _words(owner: str, priority: int, phrases: tuple[str, ...], order: int) -> WordsRegistration:
    return WordsRegistration(
        owner=owner, priority=priority, phrases=phrases, handler=_handler, alive=_alive, order=order
    )


def test_正規化した語句を含む言葉に一致する() -> None:
    reg = _words("daruma", 0, ("だるまさん",), 0)
    assert KeywordMatcher().match("ダルマさん しよう！", [reg]) is reg


def test_どの語句も含まなければ一致しない() -> None:
    reg = _words("daruma", 0, ("だるまさん",), 0)
    assert KeywordMatcher().match("おはよう", [reg]) is None


def test_優先度の高い方が選ばれる() -> None:
    low = _words("low", 1, ("あそぼう",), 0)
    high = _words("high", 5, ("あそぼう",), 1)
    assert KeywordMatcher().match("あそぼう", [low, high]) is high


def test_優先度が同じなら先に登録した方が選ばれる() -> None:
    first = _words("first", 3, ("あそぼう",), 0)
    second = _words("second", 3, ("あそぼう",), 1)
    assert KeywordMatcher().match("あそぼう", [second, first]) is first


# --- 登録表: 語句 ---


def test_登録した語句の言葉はその機能に振り分けられる() -> None:
    registry = IntentRegistry()
    reg = registry.add_words("daruma", 0, ["だるまさん"], _handler, alive=_alive)
    route = registry.route("ダルマさん しよう！")
    assert route == Words(reg)
    assert reg.owner == "daruma"
    assert reg.phrases == ("だるまさん",)


def test_登録表でも優先度と登録の順が守られる() -> None:
    registry = IntentRegistry()
    first = registry.add_words("first", 3, ["あそぼう"], _handler, alive=_alive)
    registry.add_words("second", 3, ["あそぼう"], _handler, alive=_alive)
    assert registry.route("あそぼう") == Words(first)
    high = registry.add_words("high", 9, ["あそ"], _handler, alive=_alive)
    assert registry.route("あそぼう") == Words(high)


def test_語句がないと誤りで_文言に値がない() -> None:
    registry = IntentRegistry()
    with pytest.raises(ValueError) as error:
        registry.add_words("hidden-owner", 0, [], _handler, alive=_alive)
    assert "hidden-owner" not in str(error.value)


@pytest.mark.parametrize("phrase", ["", "　", "！？。", " 、 "])
def test_正規化して空になる語句は誤りで_文言に値がない(phrase: str) -> None:
    registry = IntentRegistry()
    with pytest.raises(ValueError) as error:
        registry.add_words("owner", 0, ["だるまさん", phrase], _handler, alive=_alive)
    message = str(error.value)
    assert message
    assert "だるまさん" not in message
    if phrase.strip():
        assert phrase not in message
    assert registry.hints() == ()


def test_止まった機能の語句は使われず_表から外される() -> None:
    registry = IntentRegistry()
    flag = _Flag()
    registry.add_words("daruma", 9, ["だるまさん"], _handler, alive=flag.alive)
    other = registry.add_words("other", 0, ["だるまさん"], _handler, alive=_alive)
    flag.stopped = True
    assert registry.route("だるまさん") == Words(other)
    # 生き返っても、外された登録は戻らない
    flag.stopped = False
    assert registry.route("だるまさん") == Words(other)
    assert registry.hints() == ("だるまさん",)
    assert not registry.owner_alive("daruma")


def test_外した語句は使われない() -> None:
    registry = IntentRegistry()
    reg = registry.add_words("daruma", 0, ["だるまさん"], _handler, alive=_alive)
    registry.remove(reg)
    assert registry.route("だるまさん") == Nobody()
    # 2 回外しても誤りにならない
    registry.remove(reg)


def test_同じ形の登録でも_外すのは渡した登録だけ() -> None:
    registry = IntentRegistry()
    a = registry.add_words("same", 0, ["あ"], _handler, alive=_alive)
    b = registry.add_words("same", 0, ["あ"], _handler, alive=_alive)
    registry.remove(a)
    assert registry.route("あ") == Words(b)


# --- 登録表: 受け手 ---


def test_一致しない言葉は受け手に渡り_受け手がなければ誰もいない() -> None:
    registry = IntentRegistry()
    registry.add_words("daruma", 0, ["だるまさん"], _handler, alive=_alive)
    assert registry.route("おはよう") == Nobody()
    fallback = registry.set_fallback("chat", _handler, alive=_alive)
    assert registry.route("おはよう") == Fallback(fallback)
    assert fallback.owner == "chat"


def test_受け手の_2_つ目は断られ_記録される(caplog: pytest.LogCaptureFixture) -> None:
    registry = IntentRegistry()
    first = registry.set_fallback("chat", _handler, alive=_alive)
    with caplog.at_level(logging.INFO), pytest.raises(FallbackTaken):
        registry.set_fallback("other", _handler, alive=_alive)
    assert any("other" in record.getMessage() for record in caplog.records)
    assert registry.route("おはよう") == Fallback(first)


def test_止まった受け手は空いている扱いになる() -> None:
    registry = IntentRegistry()
    flag = _Flag()
    registry.set_fallback("chat", _handler, alive=flag.alive)
    flag.stopped = True
    second = registry.set_fallback("other", _handler, alive=_alive)
    assert registry.route("おはよう") == Fallback(second)


def test_止まった受け手には渡らない() -> None:
    registry = IntentRegistry()
    flag = _Flag()
    registry.set_fallback("chat", _handler, alive=flag.alive)
    flag.stopped = True
    assert registry.route("おはよう") == Nobody()
    assert not registry.owner_alive("chat")


def test_外した受け手の後には別の受け手を登録できる() -> None:
    registry = IntentRegistry()
    first = registry.set_fallback("chat", _handler, alive=_alive)
    registry.remove(first)
    assert registry.route("おはよう") == Nobody()
    second = registry.set_fallback("other", _handler, alive=_alive)
    assert registry.route("おはよう") == Fallback(second)


# --- ヒント・生きているか ---


def test_ヒントは生きている登録の語句だけ() -> None:
    registry = IntentRegistry()
    flag = _Flag()
    registry.add_words("a", 0, ["だるまさん", "かくれんぼ"], _handler, alive=_alive)
    registry.add_words("b", 0, ["おにごっこ"], _handler, alive=flag.alive)
    removed = registry.add_words("c", 0, ["しりとり"], _handler, alive=_alive)
    registry.remove(removed)
    flag.stopped = True
    assert registry.hints() == ("だるまさん", "かくれんぼ")


def test_機能が生きているかは_語句と受け手のどちらの登録でも分かる() -> None:
    registry = IntentRegistry()
    assert not registry.owner_alive("a")
    words = registry.add_words("a", 0, ["あ"], _handler, alive=_alive)
    registry.set_fallback("b", _handler, alive=_alive)
    assert registry.owner_alive("a")
    assert registry.owner_alive("b")
    registry.remove(words)
    assert not registry.owner_alive("a")


# --- 判定の差し替え（2.7） ---


class _AlwaysLast:
    """いつも最後の候補を選ぶ判定。"""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def match(self, text: str, candidates: Sequence[WordsRegistration]) -> WordsRegistration | None:
        self.texts.append(text)
        return candidates[-1] if candidates else None


def test_判定を差し替えても登録の口は同じ() -> None:
    matcher = _AlwaysLast()
    registry = IntentRegistry(matcher=matcher)
    registry.add_words("a", 9, ["だるまさん"], _handler, alive=_alive)
    last = registry.add_words("b", 0, ["かくれんぼ"], _handler, alive=_alive)
    assert registry.route("なんでも") == Words(last)
    assert matcher.texts == ["なんでも"]


def test_判定には生きている候補だけが渡る() -> None:
    matcher = _AlwaysLast()
    registry = IntentRegistry(matcher=matcher)
    alive = registry.add_words("a", 0, ["あ"], _handler, alive=_alive)
    registry.add_words("b", 0, ["い"], _handler, alive=_dead)
    assert registry.route("なんでも") == Words(alive)


# --- 続けて聞くの要求 ---


def test_続けて聞くの要求は_1_度だけ取り出せる() -> None:
    registry = IntentRegistry()
    registry.add_words("chat", 0, ["おはなし"], _handler, alive=_alive)
    assert registry.take_follow_up_request() is None
    registry.request_follow_up("chat")
    assert registry.take_follow_up_request() == "chat"
    assert registry.take_follow_up_request() is None


def test_続けて聞くの要求は後のものが残る() -> None:
    registry = IntentRegistry()
    registry.add_words("a", 0, ["あ"], _handler, alive=_alive)
    registry.set_fallback("b", _handler, alive=_alive)
    registry.request_follow_up("a")
    registry.request_follow_up("b")
    assert registry.take_follow_up_request() == "b"


def test_登録のない機能の続けて聞くは何もしない() -> None:
    registry = IntentRegistry()
    registry.request_follow_up("nobody")
    assert registry.take_follow_up_request() is None


# --- 複数のスレッド ---


def test_登録と判定を同時にしても壊れない() -> None:
    registry = IntentRegistry()
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def add(index: int) -> None:
        try:
            start.wait()
            for n in range(100):
                reg = registry.add_words(f"f{index}", n, [f"ご{index}"], _handler, alive=_alive)
                if n % 2:
                    registry.remove(reg)
        except BaseException as error:  # noqa: BLE001 - スレッドの誤りを集める
            errors.append(error)

    def route() -> None:
        try:
            start.wait()
            for _ in range(200):
                registry.route("ご1")
                registry.hints()
        except BaseException as error:  # noqa: BLE001 - スレッドの誤りを集める
            errors.append(error)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(4)]
    threads += [threading.Thread(target=route) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(registry.hints()) == 4 * 50
    route_result = registry.route("ご1")
    assert isinstance(route_result, Words)
    assert route_result.registration.owner == "f1"
    assert route_result.registration.priority == 98


def test_handler_for_は機能の生きている登録の口を返す() -> None:
    registry = IntentRegistry()
    seen: list[str] = []

    def words_handler(heard: Heard) -> None:
        seen.append("words")

    def fallback_handler(heard: Heard) -> None:
        seen.append("fallback")

    registry.add_words("quiz", 0, ("くいず",), words_handler, alive=_alive)
    registry.set_fallback("talk", fallback_handler, alive=_alive)
    registry.add_words("gone", 0, ("ばいばい",), words_handler, alive=_dead)

    assert registry.handler_for("quiz") is words_handler
    assert registry.handler_for("talk") is fallback_handler
    assert registry.handler_for("gone") is None
    assert registry.handler_for("nobody") is None
