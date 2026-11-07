"""外からの知らせ（NoticeBoard・Notice）のテスト（device-deployment 5.5、7.1）。"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.notice import NOTICE_FILE_NAME, Notice, NoticeBoard, NoticeUnreadable
from shared_fixtures import load_fixture

# 版の知らせの見本の期限（1793833280.0）の 10 秒前
BEFORE_UNTIL = datetime.fromtimestamp(1793833270.0, tz=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(BEFORE_UNTIL)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / NOTICE_FILE_NAME


@pytest.fixture
def board(path: Path, clock: FakeClock) -> NoticeBoard:
    return NoticeBoard(path, clock)


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.name == "osekkai.core.notice" and r.levelname == "WARNING"
    ]


# --- 見本の形（基盤と道具で同じ） ---


def test_版の知らせの見本を読める() -> None:
    notice = Notice.from_dict(load_fixture("notice_version.json"))
    assert notice == Notice(rows=("v0.2.0", "ためし ふだん:さんぷる"), until=1793833280.0)


def test_戻し忘れの知らせの見本は1行目なしで期限もない() -> None:
    notice = Notice.from_dict(load_fixture("notice_reminder.json"))
    assert notice == Notice(rows=(None, "ためしのばん もどしてね"), until=None)


def test_ファイル名は道具と同じ() -> None:
    assert NOTICE_FILE_NAME == "notice.json"


# --- 読むこと ---


def test_ファイルがなければ知らせはない(board: NoticeBoard) -> None:
    assert board.current() is None


def test_期限内の知らせを返す(board: NoticeBoard, path: Path) -> None:
    _write(path, load_fixture("notice_version.json"))
    assert board.current() == Notice(rows=("v0.2.0", "ためし ふだん:さんぷる"), until=1793833280.0)


def test_期限を過ぎると知らせはない(board: NoticeBoard, path: Path, clock: FakeClock) -> None:
    _write(path, load_fixture("notice_version.json"))
    clock.advance(9.5)
    assert board.current() is not None
    clock.advance(0.5)  # ちょうど期限
    assert board.current() is None


def test_期限のない知らせは時間がたっても残る(
    board: NoticeBoard, path: Path, clock: FakeClock
) -> None:
    _write(path, load_fixture("notice_reminder.json"))
    clock.advance(86400 * 30)
    assert board.current() == Notice(rows=(None, "ためしのばん もどしてね"), until=None)


def test_期限は整数でもよい(board: NoticeBoard, path: Path) -> None:
    _write(path, {"schema": 1, "rows": [None, "x"], "until": 1793833280})
    notice = board.current()
    assert notice is not None
    assert notice.until == 1793833280.0


def test_ファイルを消すと知らせもなくなる(board: NoticeBoard, path: Path) -> None:
    _write(path, load_fixture("notice_reminder.json"))
    assert board.current() is not None
    path.unlink()
    assert board.current() is None


# --- 形の誤り ---

_GOOD: dict[str, Any] = {"schema": 1, "rows": ["a", "b"], "until": None}


@pytest.mark.parametrize(
    "data",
    [
        [],
        "x",
        {"schema": 1, "rows": ["a", "b"]},  # until がない
        {**_GOOD, "extra": 1},
        {**_GOOD, "schema": 2},
        {**_GOOD, "schema": True},
        {**_GOOD, "schema": "1"},
        {**_GOOD, "schema": 1.0},
        {**_GOOD, "rows": ["a"]},
        {**_GOOD, "rows": ["a", "b", "c"]},
        {**_GOOD, "rows": "ab"},
        {**_GOOD, "rows": [1, "b"]},
        {**_GOOD, "rows": ["a", ["b"]]},
        {**_GOOD, "until": "1793833280"},
        {**_GOOD, "until": True},
    ],
)
def test_形が違えば読めない(data: Any) -> None:
    with pytest.raises(NoticeUnreadable):
        Notice.from_dict(data)


@pytest.mark.parametrize("until", [float("nan"), float("inf")])
def test_期限が数でなければ読めない(until: float) -> None:
    with pytest.raises(NoticeUnreadable):
        Notice.from_dict({**_GOOD, "until": until})


@pytest.mark.parametrize(
    "text",
    [
        "{",
        "",
        '{"schema": 1, "rows": ["a"], "until": null}',
        '{"schema": 2, "rows": ["a", "b"], "until": null}',
    ],
)
def test_形が違うファイルは知らせなし(board: NoticeBoard, path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    assert board.current() is None


def test_文字の符号が違うファイルは知らせなし(board: NoticeBoard, path: Path) -> None:
    path.write_bytes(b"\xff\xfe\x00")
    assert board.current() is None


def test_形の誤りの警告は続く間は1回だけで中身を出さない(
    board: NoticeBoard, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="osekkai.core.notice")
    hidden = "ひみつのなかみ"
    _write(path, {"schema": 1, "rows": [hidden], "until": None})
    for _ in range(5):
        assert board.current() is None
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert all(hidden not in r.getMessage() for r in caplog.records)


def test_正しく読めた後にまた形が違えば警告し直す(
    board: NoticeBoard, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="osekkai.core.notice")
    path.write_text("{", encoding="utf-8")
    board.current()
    _write(path, _GOOD)
    assert board.current() is not None
    path.write_text("{", encoding="utf-8")
    board.current()
    board.current()
    assert len(_warnings(caplog)) == 2


def test_読めないファイルの警告は1回だけで型の名前だけを出す(
    board: NoticeBoard, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="osekkai.core.notice")
    path.mkdir()  # ファイルの代わりにディレクトリ（読むと OSError）
    for _ in range(3):
        assert board.current() is None
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert str(path) not in warnings[0].getMessage()


def test_期限が大きすぎる整数でも読めないだけで警告は1回(
    board: NoticeBoard, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # float に直せない整数（OverflowError）で、待機画面が止まらないように
    caplog.set_level("DEBUG", logger="osekkai.core.notice")
    path.write_text('{"schema": 1, "rows": ["a", "b"], "until": ' + "9" * 400 + "}", "utf-8")
    for _ in range(3):
        assert board.current() is None
    assert len(_warnings(caplog)) == 1
