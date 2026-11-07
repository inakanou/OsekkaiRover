"""ほかの機能が使う登録の口（2.1、2.6、3.1、3.2、4.1）。"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from osekkai.voice import api
from osekkai.voice.intents import Fallback, Nobody, Words


@pytest.fixture(autouse=True)
def _empty_registry() -> Iterator[None]:
    api._reset_for_tests()
    yield
    api._reset_for_tests()


class _FakeFeature:
    """口だけを使う偽の機能。run の始めに登録し、finally で外す形をまねる。"""

    def __init__(self, owner: str, phrases: tuple[str, ...]) -> None:
        self.owner = owner
        self.phrases = phrases
        self.stop = threading.Event()
        self.heard: list[api.Heard] = []

    def on_heard(self, heard: api.Heard) -> None:
        self.heard.append(heard)

    def register(self) -> api.Registration:
        return api.register_words(
            self.owner, 0, self.phrases, self.on_heard, alive=lambda: not self.stop.is_set()
        )


def test_機能が口から登録すると入口の表で振り分けられる() -> None:
    feature = _FakeFeature("daruma", ("だるまさんがころんだ", "だるまさんが転んだ"))
    feature.register()

    route = api.registry().route("だるまさんが転んだ")

    assert isinstance(route, Words)
    assert route.registration.owner == "daruma"
    route.registration.handler(api.Heard(text="x", follow_up=False))
    assert len(feature.heard) == 1


def test_remove_で外れ_2回呼んでもよい() -> None:
    feature = _FakeFeature("daruma", ("だるま",))
    registration = feature.register()

    registration.remove()
    registration.remove()

    assert isinstance(api.registry().route("だるま"), Nobody)
    assert api.registry().hints() == ()


def test_止める旗が立つと外れる() -> None:
    feature = _FakeFeature("daruma", ("だるま",))
    feature.register()

    feature.stop.set()

    assert isinstance(api.registry().route("だるま"), Nobody)


def test_語句なしと空の語句は誤り() -> None:
    with pytest.raises(ValueError):
        api.register_words("a", 0, (), lambda h: None, alive=lambda: True)
    with pytest.raises(ValueError):
        api.register_words("a", 0, ("だるま", "、 "), lambda h: None, alive=lambda: True)


def test_受け手を登録すると当てはまらない言葉が渡る() -> None:
    received: list[api.Heard] = []
    registration = api.register_fallback("talk", received.append, alive=lambda: True)

    route = api.registry().route("きょうは はれ")

    assert isinstance(route, Fallback)
    assert route.registration.owner == "talk"

    registration.remove()
    assert isinstance(api.registry().route("きょうは はれ"), Nobody)


def test_受け手の2つ目は断る() -> None:
    api.register_fallback("talk", lambda h: None, alive=lambda: True)

    with pytest.raises(api.FallbackTaken):
        api.register_fallback("other", lambda h: None, alive=lambda: True)


def test_受け手を外せば別の機能が受け手になれる() -> None:
    first = api.register_fallback("talk", lambda h: None, alive=lambda: True)
    first.remove()

    api.register_fallback("other", lambda h: None, alive=lambda: True)

    route = api.registry().route("なにか")
    assert isinstance(route, Fallback)
    assert route.registration.owner == "other"


def test_続けて聞くの要求を入口の側から取り出せる() -> None:
    feature = _FakeFeature("daruma", ("だるま",))
    feature.register()

    api.listen_again("daruma")

    assert api.registry().take_follow_up_request() == "daruma"
    assert api.registry().take_follow_up_request() is None


def test_登録のない機能の続けて聞くは何もしない() -> None:
    api.listen_again("nobody")

    assert api.registry().take_follow_up_request() is None


def test_テスト用に空にすると前の登録は残らない() -> None:
    feature = _FakeFeature("daruma", ("だるま",))
    old = feature.register()
    api.register_fallback("talk", lambda h: None, alive=lambda: True)

    api._reset_for_tests()

    assert isinstance(api.registry().route("だるま"), Nobody)
    # 空にした後でも、前の登録の remove() は誤りにならない
    old.remove()
    api.register_fallback("other", lambda h: None, alive=lambda: True)


def test_前の表の登録を外しても新しい表の登録は残る() -> None:
    old = _FakeFeature("daruma", ("だるま",)).register()
    api._reset_for_tests()
    _FakeFeature("daruma", ("だるま",)).register()

    old.remove()

    assert isinstance(api.registry().route("だるま"), Words)


def test_口の読み込みは登録表と標準ライブラリだけ() -> None:
    import ast
    from pathlib import Path

    source = Path(api.__file__).read_text(encoding="utf-8")
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    for name in names:
        if name.startswith("osekkai"):
            assert name == "osekkai.voice.intents"
