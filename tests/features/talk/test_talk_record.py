"""1 日 1 回の記録（4.1、4.2、4.3、6.2、11.4）。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.storage import JsonValue, StateStore
from osekkai.features.talk.model import TalkKind
from osekkai.features.talk.record import NullTalkRecord, TalkOutcome, TalkRecord
from osekkai.features.talk.topics import topic_hash

TODAY = date(2026, 1, 1)
NIGHT = TalkKind.NIGHT
MORNING = TalkKind.MORNING
SPOKEN = TalkOutcome.SPOKEN
MISSED = TalkOutcome.MISSED
HIDDEN_TOPIC = "HIDDEN_TOPIC_きょうりゅう"
HIDDEN_TEXT = "HIDDEN_TEXT_なかみ"
DAY = "2026-01-01"


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> StateStore:
    return StateStore(tmp_path / "talk", clock)


def _file(store: StateStore) -> Path:
    return store.directory / "record.json"


def _read(store: StateStore) -> Any:
    return json.loads(_file(store).read_text(encoding="utf-8"))


def _write_raw(store: StateStore, text: str) -> None:
    store.directory.mkdir(parents=True, exist_ok=True)
    _file(store).write_text(text, encoding="utf-8")


def _write(store: StateStore, data: Any) -> None:
    _write_raw(store, json.dumps(data))


class _BrokenStore(StateStore):
    """書くと必ず失敗する保存先。"""

    def save(self, key: str, data: Mapping[str, JsonValue]) -> None:
        raise PermissionError("書けない")


# --- 1 日 1 回（4.1） ---


def test_何もなければどの日もどちらも済んでいない(store: StateStore) -> None:
    record = TalkRecord(store)

    assert record.done(TODAY, NIGHT) is False
    assert record.done(TODAY, MORNING) is False
    assert record.last_topic is None
    assert not _file(store).exists()  # 読むだけでは書かない


def test_記録した種類だけがその日に済む(store: StateStore) -> None:
    record = TalkRecord(store)

    record.mark(TODAY, NIGHT, SPOKEN)

    assert record.done(TODAY, NIGHT) is True
    assert record.done(TODAY, MORNING) is False
    assert record.done(TODAY + timedelta(days=1), NIGHT) is False


def test_諦めた記録も済んだ扱い(store: StateStore) -> None:
    record = TalkRecord(store)

    record.mark(TODAY, MORNING, MISSED)

    assert record.done(TODAY, MORNING) is True


def test_決まった形で保存する(store: StateStore) -> None:
    record = TalkRecord(store)

    record.mark(TODAY, NIGHT, SPOKEN)
    record.mark(TODAY, MORNING, MISSED)

    assert _read(store) == {
        "version": 1,
        "days": {"2026-01-01": {"night": "spoken", "morning": "missed"}},
        "last_topic": None,
    }


# --- 再起動（4.2） ---


def test_読み直しても済んだまま(store: StateStore) -> None:
    TalkRecord(store).mark(TODAY, NIGHT, SPOKEN)

    again = TalkRecord(store)

    assert again.done(TODAY, NIGHT) is True
    assert again.done(TODAY, MORNING) is False


def test_7日より古い日は書くときに消す(store: StateStore) -> None:
    record = TalkRecord(store)
    for back in (10, 8, 7, 1):
        record.mark(TODAY - timedelta(days=back), NIGHT, SPOKEN)

    record.mark(TODAY, NIGHT, SPOKEN)

    assert sorted(_read(store)["days"]) == ["2025-12-25", "2025-12-31", "2026-01-01"]
    assert TalkRecord(store).done(TODAY - timedelta(days=10), NIGHT) is False


# --- 直前の好きなもの（6.2） ---


def test_好きなもののhashだけを残す(store: StateStore) -> None:
    record = TalkRecord(store)

    record.set_last_topic(topic_hash(HIDDEN_TOPIC))

    assert record.last_topic == topic_hash(HIDDEN_TOPIC)
    assert TalkRecord(store).last_topic == topic_hash(HIDDEN_TOPIC)
    text = _file(store).read_text(encoding="utf-8")
    assert HIDDEN_TOPIC not in text
    assert "きょうりゅう" not in text


def test_好きなものを書き換えても日の記録は残る(store: StateStore) -> None:
    record = TalkRecord(store)
    record.mark(TODAY, NIGHT, SPOKEN)

    record.set_last_topic(topic_hash(HIDDEN_TOPIC))

    assert TalkRecord(store).done(TODAY, NIGHT) is True


def test_hashでない値は受け取らない(store: StateStore) -> None:
    record = TalkRecord(store)

    with pytest.raises(ValueError):
        record.set_last_topic(HIDDEN_TOPIC)
    assert not _file(store).exists()


# --- 読めない記録（4.3） ---


def test_壊れたファイルならその日は夜も朝も済んだ扱い(
    store: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    _write_raw(store, "{" + HIDDEN_TEXT)
    caplog.set_level(logging.WARNING)

    record = TalkRecord(store)

    assert record.done(TODAY, NIGHT) is True
    assert record.done(TODAY, MORNING) is True
    assert record.last_topic is None
    # 退避されたファイルは残り、新しい記録で書き直す
    assert list(store.directory.glob("record.corrupt-*.json"))
    assert TalkRecord(store).done(TODAY, NIGHT) is True  # 再起動しても、その日は済んだまま
    assert "record" in caplog.text
    assert "壊れていた" in caplog.text
    assert HIDDEN_TEXT not in caplog.text


def test_読めなかった次の日からは新しい記録で始める(store: StateStore) -> None:
    _write_raw(store, "[]")
    record = TalkRecord(store)
    assert record.done(TODAY, NIGHT) is True

    tomorrow = TODAY + timedelta(days=1)

    assert record.done(tomorrow, NIGHT) is False
    assert record.done(tomorrow, MORNING) is False
    record.mark(tomorrow, NIGHT, SPOKEN)
    assert TalkRecord(store).done(tomorrow, MORNING) is False


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ({"version": 2, "days": {}, "last_topic": None}, "版が違う"),
        ({"days": {}, "last_topic": None}, "版が違う"),
        ({"version": 1, "days": [], "last_topic": None}, "形が違う"),
        ({"version": 1, "last_topic": None}, "形が違う"),
        ({"version": 1, "days": {"きのう": {"night": "spoken"}}, "last_topic": None}, "形が違う"),
        ({"version": 1, "days": {DAY: {"night": "said"}}, "last_topic": None}, "形が違う"),
        ({"version": 1, "days": {DAY: {"noon": "spoken"}}, "last_topic": None}, "形が違う"),
        ({"version": 1, "days": {DAY: "spoken"}, "last_topic": None}, "形が違う"),
        ({"version": 1, "days": {}, "last_topic": HIDDEN_TEXT}, "形が違う"),
        ({"version": 1, "days": {}, "last_topic": 1}, "形が違う"),
        ({"version": 1, "days": {}}, "形が違う"),
    ],
)
def test_形や版が違うならその日は済んだ扱い(
    store: StateStore, caplog: pytest.LogCaptureFixture, data: Any, reason: str
) -> None:
    _write(store, data)
    caplog.set_level(logging.WARNING)

    record = TalkRecord(store)

    assert record.done(TODAY, NIGHT) is True
    assert record.done(TODAY, MORNING) is True
    assert record.done(TODAY + timedelta(days=1), NIGHT) is False
    assert reason in caplog.text
    assert HIDDEN_TEXT not in caplog.text
    assert _read(store)["version"] == 1  # 正しい形で書き直す


def test_読み出しで失敗してもその日は済んだ扱い(
    store: StateStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(key: str) -> None:
        raise PermissionError("読めない")

    monkeypatch.setattr(store, "load", fail)
    caplog.set_level(logging.WARNING)

    record = TalkRecord(store)

    assert record.done(TODAY, NIGHT) is True
    assert "PermissionError" in caplog.text


def test_読めなかった日は最初に問われた日(store: StateStore) -> None:
    _write_raw(store, "{")
    record = TalkRecord(store)
    # 好きなものだけを先に問うても、日付はまだ決めない
    assert record.last_topic is None

    later = TODAY + timedelta(days=3)

    assert record.done(later, NIGHT) is True
    assert record.done(TODAY, NIGHT) is False


# --- 書けないとき ---


def test_書けなければ呼んだ側に知らせて記録しない(tmp_path: Path, clock: FakeClock) -> None:
    record = TalkRecord(_BrokenStore(tmp_path / "talk", clock))

    with pytest.raises(OSError):
        record.mark(TODAY, NIGHT, SPOKEN)

    assert record.done(TODAY, NIGHT) is False


def test_好きなものが書けなくてもログだけで続ける(
    tmp_path: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    record = TalkRecord(_BrokenStore(tmp_path / "talk", clock))
    caplog.set_level(logging.WARNING)

    record.set_last_topic(topic_hash(HIDDEN_TOPIC))

    assert record.last_topic == topic_hash(HIDDEN_TOPIC)
    assert "record" in caplog.text
    assert topic_hash(HIDDEN_TOPIC) not in caplog.text


def test_壊れた記録を書き直せなくてもその日は済んだ扱い(tmp_path: Path, clock: FakeClock) -> None:
    store = _BrokenStore(tmp_path / "talk", clock)
    _write_raw(store, "{")

    record = TalkRecord(store)

    assert record.done(TODAY, NIGHT) is True
    assert record.done(TODAY, MORNING) is True


# --- 試し用（11.4） ---


def test_試し用は何も済んでおらず何も残さない(tmp_path: Path) -> None:
    record = NullTalkRecord()

    record.mark(TODAY, NIGHT, SPOKEN)
    record.set_last_topic(topic_hash(HIDDEN_TOPIC))

    assert record.done(TODAY, NIGHT) is False
    assert record.last_topic is None
    assert list(tmp_path.iterdir()) == []
