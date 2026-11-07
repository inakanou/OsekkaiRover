"""記録と星（8.1〜8.8、3.5）のテスト。"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from routine_fixtures import ROUTINE_START

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.storage import JsonValue, StateStore
from osekkai.features.routine.records import (
    DayRecordView,
    NullRecordBook,
    RecordBook,
    TaskResult,
)

TODAY = ROUTINE_START.date()
KEEP_DAYS = 90


def _load(store: StateStore, clock: FakeClock, keep_days: int = KEEP_DAYS) -> RecordBook:
    return RecordBook.load(store, keep_days, clock)


def _read(store: StateStore, key: str) -> Any:
    return json.loads((store.directory / f"{key}.json").read_text(encoding="utf-8"))


def _write(store: StateStore, key: str, data: Any) -> None:
    store.directory.mkdir(parents=True, exist_ok=True)
    (store.directory / f"{key}.json").write_text(json.dumps(data), encoding="utf-8")


def _snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    if not directory.exists():
        return {}
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(directory.iterdir())}


# 8.1、8.3: 結果と星


def test_何もないときは星が0で記録もない(routine_store: StateStore, clock: FakeClock) -> None:
    book = _load(routine_store, clock)
    assert book.stars == 0
    assert book.recovered_from_corruption is False
    assert book.outcome(TODAY, "wash") is None
    assert book.ended_at(TODAY, "wash") is None
    assert book.notice_said(TODAY, "wash") is False


def test_できたで星が増え結果と終わった時刻が残る(
    routine_store: StateStore, clock: FakeClock
) -> None:
    book = _load(routine_store, clock)
    clock.advance(300)
    book.record_outcome(TODAY, "wash", TaskResult.DONE)

    assert book.stars == 1
    assert book.outcome(TODAY, "wash") is TaskResult.DONE
    assert book.ended_at(TODAY, "wash") == clock.now()

    again = _load(routine_store, clock)
    assert again.stars == 1
    assert again.outcome(TODAY, "wash") is TaskResult.DONE
    assert again.ended_at(TODAY, "wash") == clock.now()
    assert _read(routine_store, "stars") == {"version": 1, "stars": 1}
    saved = _read(routine_store, "records")
    assert saved["version"] == 1
    assert saved["stars"] == 1
    assert saved["days"][TODAY.isoformat()]["tasks"]["wash"]["result"] == "done"


@pytest.mark.parametrize("result", [TaskResult.SKIPPED, TaskResult.INTERRUPTED])
def test_スキップと中断では星が増えない(
    routine_store: StateStore, clock: FakeClock, result: TaskResult
) -> None:
    book = _load(routine_store, clock)
    book.record_outcome(TODAY, "wash", result)
    assert book.stars == 0
    assert _load(routine_store, clock).outcome(TODAY, "wash") is result


def test_星はできたの数の合計(routine_store: StateStore, clock: FakeClock) -> None:
    book = _load(routine_store, clock)
    book.record_outcome(TODAY, "wash", TaskResult.DONE)
    book.record_outcome(TODAY, "brush", TaskResult.DONE)
    book.record_outcome(TODAY, "dress", TaskResult.SKIPPED)
    book.record_outcome(TODAY + timedelta(days=1), "wash", TaskResult.DONE)
    assert book.stars == 3
    assert _load(routine_store, clock).stars == 3


def test_結果は上書きしない(routine_store: StateStore, clock: FakeClock) -> None:
    book = _load(routine_store, clock)
    book.record_outcome(TODAY, "wash", TaskResult.SKIPPED)
    first_end = book.ended_at(TODAY, "wash")
    clock.advance(600)
    book.record_outcome(TODAY, "wash", TaskResult.DONE)

    assert book.outcome(TODAY, "wash") is TaskResult.SKIPPED
    assert book.ended_at(TODAY, "wash") == first_end
    assert book.stars == 0
    assert _load(routine_store, clock).outcome(TODAY, "wash") is TaskResult.SKIPPED


def test_日ごとに別の記録になる(routine_store: StateStore, clock: FakeClock) -> None:
    book = _load(routine_store, clock)
    book.record_outcome(TODAY, "wash", TaskResult.DONE)
    assert book.outcome(TODAY + timedelta(days=1), "wash") is None


# 8.2、3.5: 予告


def test_予告を言ったことが残る(routine_store: StateStore, clock: FakeClock) -> None:
    book = _load(routine_store, clock)
    book.record_notice(TODAY, "sleep")
    book.record_notice(TODAY, "sleep")

    assert book.notice_said(TODAY, "sleep") is True
    assert book.notice_said(TODAY, "wash") is False
    assert book.notice_said(TODAY + timedelta(days=1), "sleep") is False
    assert book.stars == 0
    again = _load(routine_store, clock)
    assert again.notice_said(TODAY, "sleep") is True
    assert _read(routine_store, "records")["days"][TODAY.isoformat()]["notices"] == ["sleep"]


# 8.6、8.7: 整理


def test_決めた日数より古い日を消しても星は減らない(
    routine_store: StateStore, clock: FakeClock
) -> None:
    book = _load(routine_store, clock, keep_days=7)
    old = TODAY - timedelta(days=8)
    edge = TODAY - timedelta(days=7)
    book.record_outcome(old, "wash", TaskResult.DONE)
    book.record_notice(old, "sleep")
    book.record_outcome(edge, "wash", TaskResult.DONE)
    book.record_outcome(TODAY, "wash", TaskResult.DONE)
    assert book.stars == 3

    book.prune(TODAY)

    assert book.outcome(old, "wash") is None
    assert book.notice_said(old, "sleep") is False
    assert book.outcome(edge, "wash") is TaskResult.DONE
    assert book.outcome(TODAY, "wash") is TaskResult.DONE
    assert book.stars == 3
    again = _load(routine_store, clock, keep_days=7)
    assert again.stars == 3
    assert again.outcome(old, "wash") is None
    assert old.isoformat() not in _read(routine_store, "records")["days"]


# 8.4、8.5: 壊れない記録


def test_壊れた記録は退避して控えから星を戻す(routine_store: StateStore, clock: FakeClock) -> None:
    book = _load(routine_store, clock)
    book.record_outcome(TODAY, "wash", TaskResult.DONE)
    book.record_outcome(TODAY, "brush", TaskResult.DONE)
    (routine_store.directory / "records.json").write_text("{壊れた", encoding="utf-8")

    again = _load(routine_store, clock)

    assert again.stars == 2
    assert again.recovered_from_corruption is True
    assert again.outcome(TODAY, "wash") is None
    assert list(routine_store.directory.glob("records.corrupt-*.json"))
    assert not (routine_store.directory / "records.json").exists()


def test_壊れた記録で控えもなければ星は0(routine_store: StateStore, clock: FakeClock) -> None:
    routine_store.directory.mkdir(parents=True, exist_ok=True)
    (routine_store.directory / "records.json").write_text("[1, 2]", encoding="utf-8")
    book = _load(routine_store, clock)
    assert book.stars == 0
    assert book.recovered_from_corruption is True


def test_壊れた控えでも記録から星を読む(routine_store: StateStore, clock: FakeClock) -> None:
    _write(routine_store, "records", {"version": 1, "stars": 4, "days": {}})
    (routine_store.directory / "stars.json").write_text("壊れた", encoding="utf-8")
    book = _load(routine_store, clock)
    assert book.stars == 4
    assert book.recovered_from_corruption is False


def test_記録だけ書けて控えが古くても星は減らない(
    routine_store: StateStore, clock: FakeClock
) -> None:
    _write(routine_store, "records", {"version": 1, "stars": 5, "days": {}})
    _write(routine_store, "stars", {"version": 1, "stars": 4})
    assert _load(routine_store, clock).stars == 5


def test_控えの方が大きければ控えの星を使う(routine_store: StateStore, clock: FakeClock) -> None:
    _write(routine_store, "records", {"version": 1, "stars": 3, "days": {}})
    _write(routine_store, "stars", {"version": 1, "stars": 6})
    book = _load(routine_store, clock)
    assert book.stars == 6
    book.record_outcome(TODAY, "wash", TaskResult.DONE)
    assert book.stars == 7
    assert _read(routine_store, "records")["stars"] == 7
    assert _read(routine_store, "stars")["stars"] == 7


def test_版が違う記録は読まず別の名前で残す(routine_store: StateStore, clock: FakeClock) -> None:
    day = TODAY.isoformat()
    future = {
        "version": 2,
        "stars": 9,
        "days": {day: {"tasks": {"wash": {"result": "done"}}, "notices": []}},
    }
    _write(routine_store, "records", future)
    _write(routine_store, "stars", {"version": 1, "stars": 2})

    book = _load(routine_store, clock)

    assert book.stars == 2
    assert book.outcome(TODAY, "wash") is None
    assert book.recovered_from_corruption is True
    assert not (routine_store.directory / "records.json").exists()
    assert not list(routine_store.directory.glob("*.corrupt-*.json"))
    kept = list(routine_store.directory.glob("records.other-version-*.json"))
    assert len(kept) == 1
    assert json.loads(kept[0].read_text(encoding="utf-8")) == future

    # 次に書いても、残した記録は上書きされない
    book.record_outcome(TODAY, "wash", TaskResult.DONE)
    assert json.loads(kept[0].read_text(encoding="utf-8")) == future


def test_版が違う控えは読まない(routine_store: StateStore, clock: FakeClock) -> None:
    _write(routine_store, "stars", {"version": 2, "stars": 9})
    assert _load(routine_store, clock).stars == 0


def test_形のおかしい中身は飛ばして読む(routine_store: StateStore, clock: FakeClock) -> None:
    day = TODAY.isoformat()
    other = (TODAY - timedelta(days=1)).isoformat()
    data: dict[str, JsonValue] = {
        "version": 1,
        "stars": "たくさん",
        "days": {
            day: {
                "tasks": {
                    "wash": {"result": "done", "ended_at": ROUTINE_START.isoformat()},
                    "brush": {"result": "なぞ", "ended_at": ROUTINE_START.isoformat()},
                    "dress": {"result": "skipped", "ended_at": "いつか"},
                    "shoes": "done",
                    "naive": {"result": "done", "ended_at": "2026-04-06T06:00:00"},
                },
                "notices": ["sleep", 3, None],
            },
            other: {"tasks": [], "notices": "sleep"},
            "きのう": {"tasks": {}, "notices": []},
        },
    }
    _write(routine_store, "records", data)

    book = _load(routine_store, clock)

    assert book.stars == 0
    assert book.outcome(TODAY, "wash") is TaskResult.DONE
    assert book.ended_at(TODAY, "wash") == ROUTINE_START
    assert book.outcome(TODAY, "brush") is None
    assert book.outcome(TODAY, "dress") is TaskResult.SKIPPED
    assert book.ended_at(TODAY, "dress") is None
    assert book.outcome(TODAY, "shoes") is None
    assert book.ended_at(TODAY, "naive") is None
    assert book.notice_said(TODAY, "sleep") is True
    assert book.notice_said(TODAY - timedelta(days=1), "sleep") is False


def test_負の星や真偽の星は0とみなす(routine_store: StateStore, clock: FakeClock) -> None:
    _write(routine_store, "records", {"version": 1, "stars": -3, "days": {}})
    _write(routine_store, "stars", {"version": 1, "stars": True})
    assert _load(routine_store, clock).stars == 0


def test_書けなくても型の名前だけ記録して続け次にまとめて書く(
    routine_store: StateStore,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    book = _load(routine_store, clock)

    def broken_save(key: str, data: Any) -> None:
        raise PermissionError("書けない wash")

    with monkeypatch.context() as patch:
        patch.setattr(routine_store, "save", broken_save)
        with caplog.at_level(logging.WARNING, logger="osekkai.features.routine"):
            book.record_outcome(TODAY, "wash", TaskResult.DONE)
            book.record_notice(TODAY, "sleep")

    assert book.stars == 1
    assert book.outcome(TODAY, "wash") is TaskResult.DONE
    assert "PermissionError" in caplog.text
    assert "書けない wash" not in caplog.text
    assert not (routine_store.directory / "records.json").exists()

    book.record_outcome(TODAY, "brush", TaskResult.SKIPPED)

    again = _load(routine_store, clock)
    assert again.stars == 1
    assert again.outcome(TODAY, "wash") is TaskResult.DONE
    assert again.outcome(TODAY, "brush") is TaskResult.SKIPPED
    assert again.notice_said(TODAY, "sleep") is True


def test_記録の後に控えを書く(
    routine_store: StateStore, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = _load(routine_store, clock)
    order: list[str] = []
    real_save = routine_store.save

    def spy_save(key: str, data: Any) -> None:
        order.append(key)
        real_save(key, data)

    monkeypatch.setattr(routine_store, "save", spy_save)
    book.record_outcome(TODAY, "wash", TaskResult.DONE)
    assert order == ["records", "stars"]


def test_ログに記録の中身を出さない(
    routine_store: StateStore, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    routine_store.directory.mkdir(parents=True, exist_ok=True)
    (routine_store.directory / "records.json").write_text("{秘密の中身", encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        _load(routine_store, clock)
    assert "秘密の中身" not in caplog.text


def test_記録はリポジトリの外の状態の場所に置く(
    routine_store: StateStore, clock: FakeClock, xdg_dirs: dict[str, Path]
) -> None:
    book = _load(routine_store, clock)
    book.record_outcome(TODAY, "wash", TaskResult.DONE)
    state_home = xdg_dirs["XDG_STATE_HOME"]
    assert (routine_store.directory / "records.json").is_relative_to(state_home)


# 10.2: 試し用の記録


def test_試し用の記録は何も覚えず何も書かない(routine_store: StateStore, clock: FakeClock) -> None:
    real = _load(routine_store, clock)
    real.record_outcome(TODAY, "wash", TaskResult.DONE)
    real.record_notice(TODAY, "sleep")
    before = _snapshot(routine_store.directory)

    null = NullRecordBook.from_store(routine_store)
    null.record_outcome(TODAY, "brush", TaskResult.DONE)
    null.record_notice(TODAY, "brush")
    null.prune(TODAY + timedelta(days=1000))

    assert null.stars == 1
    assert null.recovered_from_corruption is False
    assert null.outcome(TODAY, "wash") is None
    assert null.outcome(TODAY, "brush") is None
    assert null.ended_at(TODAY, "brush") is None
    assert null.notice_said(TODAY, "brush") is False
    assert _snapshot(routine_store.directory) == before


def test_試し用の記録は壊れた記録も退避しない(routine_store: StateStore, clock: FakeClock) -> None:
    _write(routine_store, "stars", {"version": 1, "stars": 3})
    (routine_store.directory / "records.json").write_text("{壊れた", encoding="utf-8")
    before = _snapshot(routine_store.directory)

    null = NullRecordBook.from_store(routine_store)

    assert null.stars == 3
    assert _snapshot(routine_store.directory) == before


def test_試し用の記録は保存先がなくても星0(tmp_path: Path, clock: FakeClock) -> None:
    store = StateStore(tmp_path / "なし", clock)
    null = NullRecordBook.from_store(store)
    assert null.stars == 0
    assert not (tmp_path / "なし").exists()
    assert NullRecordBook(5).stars == 5


def test_どちらの記録も判定の読む口を満たす(routine_store: StateStore, clock: FakeClock) -> None:
    views: list[DayRecordView] = [_load(routine_store, clock), NullRecordBook(0)]
    for view in views:
        assert view.outcome(date(2026, 1, 1), "wash") is None
