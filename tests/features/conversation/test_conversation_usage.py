"""1 日の回数の記録（5.3〜5.6、8.1）。"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from osekkai.adapters.fake.clock import JST, FakeClock
from osekkai.core.storage import StateStore
from osekkai.features.conversation.usage import DailyUsage

TODAY = date(2026, 1, 1)  # FakeClock の始まりの日
LIMIT = 3


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> StateStore:
    return StateStore(tmp_path / "conversation", clock)


def _file(store: StateStore) -> Path:
    return store.directory / "usage.json"


def _read(store: StateStore) -> Any:
    return json.loads(_file(store).read_text(encoding="utf-8"))


def _write_raw(store: StateStore, text: str) -> None:
    store.directory.mkdir(parents=True, exist_ok=True)
    _file(store).write_text(text, encoding="utf-8")


def _write(store: StateStore, data: Any) -> None:
    _write_raw(store, json.dumps(data))


# --- 数えて、保存する ---


def test_何もなければ0から数える(store: StateStore, clock: FakeClock) -> None:
    usage = DailyUsage.load(store, clock)

    assert usage.reached(TODAY, LIMIT) is False
    assert not _file(store).exists()  # 読むだけでは書かない


def test_1つ足すとその日の回数を返して保存する(store: StateStore, clock: FakeClock) -> None:
    usage = DailyUsage.load(store, clock)

    assert usage.add_one(TODAY) == 1
    assert usage.add_one(TODAY) == 2
    assert _read(store) == {"version": 1, "date": "2026-01-01", "count": 2, "blocked": False}


def test_上限ちょうどで達したとみなす(store: StateStore, clock: FakeClock) -> None:
    usage = DailyUsage.load(store, clock)
    for _ in range(LIMIT - 1):
        usage.add_one(TODAY)
    assert usage.reached(TODAY, LIMIT) is False

    usage.add_one(TODAY)

    assert usage.reached(TODAY, LIMIT) is True


def test_読み直しで回数を引き継ぐ(store: StateStore, clock: FakeClock) -> None:
    # 5.5: 起動し直しても数え直さない
    usage = DailyUsage.load(store, clock)
    for _ in range(LIMIT):
        usage.add_one(TODAY)

    again = DailyUsage.load(store, clock)

    assert again.reached(TODAY, LIMIT) is True
    assert again.add_one(TODAY) == LIMIT + 1


def test_日付が変わると0から数える(store: StateStore, clock: FakeClock) -> None:
    usage = DailyUsage.load(store, clock)
    for _ in range(LIMIT):
        usage.add_one(TODAY)
    tomorrow = TODAY + timedelta(days=1)

    assert usage.reached(tomorrow, LIMIT) is False
    assert usage.add_one(tomorrow) == 1
    assert _read(store) == {"version": 1, "date": "2026-01-02", "count": 1, "blocked": False}


def test_前の日の記録を読んでも今日は0から(store: StateStore, clock: FakeClock) -> None:
    _write(store, {"version": 1, "date": "2025-12-31", "count": 99, "blocked": False})

    usage = DailyUsage.load(store, clock)

    assert usage.reached(TODAY, LIMIT) is False
    assert usage.add_one(TODAY) == 1


def test_前の日が読めなかった日でも今日は0から(store: StateStore, clock: FakeClock) -> None:
    _write(store, {"version": 1, "date": "2025-12-31", "count": 0, "blocked": True})

    usage = DailyUsage.load(store, clock)

    assert usage.reached(TODAY, LIMIT) is False


def test_読めなかった日は再起動しても達したまま(store: StateStore, clock: FakeClock) -> None:
    _write(store, {"version": 1, "date": "2026-01-01", "count": 0, "blocked": True})

    usage = DailyUsage.load(store, clock)

    assert usage.reached(TODAY, LIMIT) is True


# --- 保存に失敗しても守る ---


def test_保存に失敗しても記憶の中で数える(
    store: StateStore, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = DailyUsage.load(store, clock)

    def broken(key: str, data: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", broken)

    for _ in range(LIMIT):
        usage.add_one(TODAY)

    assert usage.reached(TODAY, LIMIT) is True


# --- 読めない記録（5.6） ---

_UNREADABLE = {
    "壊れた": "{こわれ",
    "辞書でない": "[1, 2]",
    "版が違う": json.dumps({"version": 2, "date": "2026-01-01", "count": 0, "blocked": False}),
    "版がない": json.dumps({"date": "2026-01-01", "count": 0, "blocked": False}),
    "日付が文字でない": json.dumps({"version": 1, "date": 20260101, "count": 0, "blocked": False}),
    "日付が読めない": json.dumps({"version": 1, "date": "きのう", "count": 0, "blocked": False}),
    "回数が負": json.dumps({"version": 1, "date": "2026-01-01", "count": -1, "blocked": False}),
    "回数が真偽": json.dumps({"version": 1, "date": "2026-01-01", "count": True, "blocked": False}),
    "回数が小数": json.dumps({"version": 1, "date": "2026-01-01", "count": 1.5, "blocked": False}),
    "blocked がない": json.dumps({"version": 1, "date": "2026-01-01", "count": 0}),
    "blocked が文字": json.dumps({"version": 1, "date": "2026-01-01", "count": 0, "blocked": "no"}),
}


@pytest.mark.parametrize("text", list(_UNREADABLE.values()), ids=list(_UNREADABLE))
def test_読めない記録は達したとみなし読めなかった日を書く(
    store: StateStore, clock: FakeClock, text: str
) -> None:
    _write_raw(store, text)

    usage = DailyUsage.load(store, clock)

    assert usage.reached(TODAY, LIMIT) is True
    assert _read(store) == {"version": 1, "date": "2026-01-01", "count": 0, "blocked": True}
    # 書き直した後も、読み直して達したまま
    assert DailyUsage.load(store, clock).reached(TODAY, LIMIT) is True


def test_壊れた記録は基盤が退避する(store: StateStore, clock: FakeClock) -> None:
    _write_raw(store, "{こわれ")

    DailyUsage.load(store, clock).reached(TODAY, LIMIT)

    assert list(store.directory.glob("usage.corrupt-*.json"))


def test_読み出しの誤りでも達したとみなす(
    store: StateStore, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(store, {"version": 1, "date": "2026-01-01", "count": 0, "blocked": False})

    def broken(key: str) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(store, "load", broken)

    usage = DailyUsage.load(store, clock)

    assert usage.reached(TODAY, LIMIT) is True
    assert _read(store)["blocked"] is True


def test_読めなかった日を書けなくても記憶の中で達したとみなす(
    store: StateStore, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_raw(store, "{こわれ")

    def broken(key: str, data: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", broken)

    usage = DailyUsage.load(store, clock)

    assert usage.reached(TODAY, LIMIT) is True
    # 足しても読めなかった日のまま
    usage.add_one(TODAY)
    assert usage.reached(TODAY, 1000) is True


# --- ログ（5.6、8.1） ---

_SECRET_DATE = "1999-09-09"


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"version": 7, "date": _SECRET_DATE, "count": 4242, "blocked": False}),
        json.dumps({"version": 1, "date": _SECRET_DATE, "count": "ひみつ", "blocked": False}),
        "{ひみつ 4242",
    ],
    ids=["版が違う", "形が違う", "壊れた"],
)
def test_ログには記録の名前と理由の種類だけ(
    store: StateStore, clock: FakeClock, caplog: pytest.LogCaptureFixture, text: str
) -> None:
    _write_raw(store, text)

    with caplog.at_level(logging.DEBUG):
        DailyUsage.load(store, clock).reached(TODAY, LIMIT)

    usage_logs = [r for r in caplog.records if r.name.endswith("conversation.usage")]
    assert usage_logs
    for record in usage_logs:
        message = record.getMessage()
        assert "usage" in message
        assert "ひみつ" not in message
        assert "4242" not in message
        assert _SECRET_DATE not in message
        assert "7" not in message
    assert "ひみつ" not in caplog.text
    assert "4242" not in caplog.text


def test_保存の失敗のログは例外の型の名前だけ(
    store: StateStore,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    usage = DailyUsage.load(store, clock)

    def broken(key: str, data: object) -> None:
        raise OSError("ひみつの場所")

    monkeypatch.setattr(store, "save", broken)

    with caplog.at_level(logging.DEBUG):
        usage.add_one(TODAY)

    assert "OSError" in caplog.text
    assert "ひみつ" not in caplog.text


# --- 時刻が確かでない起動（5.5、5.6） ---
# 読むのは確かな日付が来たとき（reached / add_one）。load では読まない

_STALE = datetime(2000, 1, 1, 9, 0, tzinfo=JST)  # 同期の前の古い時刻
_DAY1 = date(2026, 3, 3)  # 同期の後の本当の日付


def _unsynced() -> FakeClock:
    return FakeClock(_STALE, synchronized=False)


def test_確かな日付が来るまでファイルに触らない(store: StateStore) -> None:
    _write_raw(store, "{こわれ")

    DailyUsage.load(store, _unsynced())

    assert _file(store).read_text(encoding="utf-8") == "{こわれ"
    assert not list(store.directory.glob("usage.corrupt-*.json"))


def test_同期の前に起動して読めなければ確かな日付の日に達する(store: StateStore) -> None:
    _write_raw(store, "{こわれ")

    usage = DailyUsage.load(store, _unsynced())

    assert usage.reached(_DAY1, LIMIT) is True
    assert _read(store) == {"version": 1, "date": "2026-03-03", "count": 0, "blocked": True}
    # 次の日は 0 から
    assert usage.reached(_DAY1 + timedelta(days=1), LIMIT) is False


def test_同期の前に起動して読めずに足しても読めなかった日のまま(store: StateStore) -> None:
    _write_raw(store, "{こわれ")

    usage = DailyUsage.load(store, _unsynced())
    usage.add_one(_DAY1)

    assert usage.reached(_DAY1, 1000) is True
    assert _read(store)["date"] == "2026-03-03"
    assert _read(store)["blocked"] is True


def test_確かな日付の前に何度起動し直しても読めない記録は残る(store: StateStore) -> None:
    _write_raw(store, "{こわれ")
    for _ in range(3):
        DailyUsage.load(store, _unsynced())

    # 同期の後に起動し直して、初めて読む
    usage = DailyUsage.load(store, FakeClock(datetime(2026, 3, 3, 9, 0, tzinfo=JST)))

    assert usage.reached(_DAY1, LIMIT) is True


def test_前の日の読めなかった日の記録は同期の前に起動しても今日を止めない(
    store: StateStore,
) -> None:
    _write(store, {"version": 1, "date": "2026-03-02", "count": 0, "blocked": True})

    usage = DailyUsage.load(store, _unsynced())

    assert usage.reached(_DAY1, LIMIT) is False
    assert usage.add_one(_DAY1) == 1
    assert _read(store) == {"version": 1, "date": "2026-03-03", "count": 1, "blocked": False}


def test_読めなかった次の日は同期の前に何度起動しても止めない(store: StateStore) -> None:
    _write_raw(store, "{こわれ")
    assert DailyUsage.load(store, _unsynced()).reached(_DAY1, LIMIT) is True

    day2 = _DAY1 + timedelta(days=1)
    for _ in range(3):
        usage = DailyUsage.load(store, _unsynced())
        assert usage.reached(day2, LIMIT) is False
    assert _read(store)["blocked"] is True  # 読むだけでは書き換えない
    assert _read(store)["date"] == "2026-03-03"


def test_同期の前に読めた記録は日付で0に戻る(store: StateStore) -> None:
    _write(store, {"version": 1, "date": "2000-01-01", "count": 99, "blocked": False})

    usage = DailyUsage.load(store, _unsynced())

    assert usage.reached(_DAY1, LIMIT) is False


def test_あるかを見るときの誤りも読めないとみなす(
    store: StateStore,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store.directory.mkdir(parents=True, exist_ok=True)
    usage = DailyUsage.load(store, clock)

    def broken(self: Path) -> bool:
        raise PermissionError("denied")

    # 保存も同じ誤りで失敗するので、記憶の中で達したとみなすことを見る
    monkeypatch.setattr(Path, "exists", broken)
    with caplog.at_level(logging.WARNING):
        reached = usage.reached(TODAY, LIMIT)
    monkeypatch.undo()

    assert reached is True
    assert "usage (PermissionError)" in caplog.text
    assert usage.reached(TODAY, 1000) is True
