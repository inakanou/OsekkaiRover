"""あいさつの判定と記録（3.1〜3.7、3.9、3.10、4.5）。"""

import json
import logging
from collections.abc import Sequence
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.storage import StateStore
from osekkai.features.presence import texts
from osekkai.features.presence.config import PresenceConfig
from osekkai.features.presence.greeting import (
    SAVE_INTERVAL,
    STORE_KEY,
    UNKNOWN,
    GreetingDecision,
    GreetingKind,
    GreetingPolicy,
    GreetingRecords,
)

JST = timezone(timedelta(hours=9), "JST")
# ログに出てはいけない、目立つ名前と呼び名
HIDDEN_ID = "zzhiddenid"
HIDDEN_NAME = "ひみつのよびな"
OTHER_ID = "zzotherid"


def _at(hour: int, minute: int = 0, second: int = 0, day: int = 10) -> datetime:
    return datetime(2026, 3, day, hour, minute, second, tzinfo=JST)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "presence", FakeClock())


@pytest.fixture
def records(store: StateStore) -> GreetingRecords:
    return GreetingRecords.load(store)


def _first(options: Sequence[str]) -> str:
    return options[0]


def _policy(**overrides: object) -> GreetingPolicy:
    return GreetingPolicy(PresenceConfig.model_validate(overrides), choose=_first)


def _decide(
    policy: GreetingPolicy,
    records: GreetingRecords,
    now: datetime,
    *,
    who: str | None = HIDDEN_ID,
    synced: bool = True,
    looked_for: float = 2.0,
    waiting_recognition: float = 5.0,
    has_known_people: bool = True,
) -> GreetingDecision | None:
    return policy.decide(
        now,
        synced=synced,
        who=who,
        looked_for=looked_for,
        waiting_recognition=waiting_recognition,
        has_known_people=has_known_people,
        records=records,
    )


# --- しないとき ---


def test_時刻が確かでなければあいさつしない(records: GreetingRecords) -> None:
    assert _decide(_policy(), records, _at(12), synced=False) is None


@pytest.mark.parametrize(
    ("now", "greets"),
    [
        (_at(6, 59), False),
        (_at(7, 0), True),  # 始めを含む
        (_at(20, 29), True),
        (_at(20, 30), False),  # 終わりを含まない
        (_at(23, 0), False),
    ],
)
def test_時間帯の外ではあいさつしない(
    records: GreetingRecords, now: datetime, greets: bool
) -> None:
    assert (_decide(_policy(), records, now) is not None) is greets


@pytest.mark.parametrize(
    ("now", "greets"),
    [
        (_at(21, 59), False),
        (_at(22, 0), True),
        (_at(23, 59), True),
        (_at(0, 0), True),
        (_at(5, 59), True),
        (_at(6, 0), False),
        (_at(12), False),
    ],
)
def test_日をまたぐ時間帯(records: GreetingRecords, now: datetime, greets: bool) -> None:
    policy = _policy(greet_hours=["22:00", "06:00"])
    assert (_decide(policy, records, now) is not None) is greets


@pytest.mark.parametrize(("looked_for", "greets"), [(0.0, False), (0.99, False), (1.0, True)])
def test_見続けて1秒未満ならあいさつしない(
    records: GreetingRecords, looked_for: float, greets: bool
) -> None:
    result = _decide(_policy(), records, _at(12), looked_for=looked_for)
    assert (result is not None) is greets


@pytest.mark.parametrize(("waiting", "greets"), [(0.0, False), (2.99, False), (3.0, True)])
def test_覚えた顔があって誰か分からなければ3秒まで待つ(
    records: GreetingRecords, waiting: float, greets: bool
) -> None:
    result = _decide(_policy(), records, _at(12), who=None, waiting_recognition=waiting)
    assert (result is not None) is greets
    if result is not None:
        assert result.who is None


def test_覚えた顔がなければ待たずに知らない人へあいさつする(records: GreetingRecords) -> None:
    result = _decide(
        _policy(), records, _at(12), who=None, waiting_recognition=0.0, has_known_people=False
    )
    assert result == GreetingDecision(GreetingKind.OKAERI, None)


def test_誰か分かっていれば待たない(records: GreetingRecords) -> None:
    result = _decide(_policy(), records, _at(12), waiting_recognition=0.0)
    assert result == GreetingDecision(GreetingKind.OKAERI, HIDDEN_ID)


@pytest.mark.parametrize(("minutes", "greets"), [(0, False), (19, False), (20, True)])
def test_見えなかった時間が20分未満ならあいさつしない(
    records: GreetingRecords, minutes: int, greets: bool
) -> None:
    now = _at(15)
    records.mark_seen(HIDDEN_ID, now - timedelta(minutes=minutes))
    result = _decide(_policy(), records, now)
    assert (result is not None) is greets
    if result is not None:
        assert result.kind is GreetingKind.HELLO


def test_20分の境目は秒まで見る(records: GreetingRecords) -> None:
    now = _at(15)
    records.mark_seen(HIDDEN_ID, now - timedelta(minutes=20) + timedelta(seconds=1))
    assert _decide(_policy(), records, now) is None


def test_見えなかった時間は設定で変えられる(records: GreetingRecords) -> None:
    now = _at(15)
    records.mark_seen(HIDDEN_ID, now - timedelta(minutes=10))
    assert _decide(_policy(away_minutes=5), records, now) is not None


# --- 何と言うか ---


def test_初めて見た人には朝より前ならおはよう(records: GreetingRecords) -> None:
    assert _decide(_policy(), records, _at(8)) == GreetingDecision(GreetingKind.MORNING, HIDDEN_ID)


@pytest.mark.parametrize(
    ("now", "kind"),
    [(_at(10, 59, 59), GreetingKind.MORNING), (_at(11, 0), GreetingKind.OKAERI)],
)
def test_朝は11時より前(records: GreetingRecords, now: datetime, kind: GreetingKind) -> None:
    result = _decide(_policy(), records, now)
    assert result is not None
    assert result.kind is kind


def test_朝のあいさつはその日に1回(records: GreetingRecords) -> None:
    policy = _policy()
    first = _decide(policy, records, _at(8))
    assert first == GreetingDecision(GreetingKind.MORNING, HIDDEN_ID)
    records.mark_greeted(first, _at(8))
    # 30 分たって、また見つけた（120 分より短い）
    later = _decide(policy, records, _at(8, 30))
    assert later == GreetingDecision(GreetingKind.HELLO, HIDDEN_ID)
    # 次の日の朝は、またおはよう
    records.mark_seen(HIDDEN_ID, _at(9, day=10))
    assert records.morning_done(HIDDEN_ID, _at(8, day=11).date()) is False
    next_day = _decide(policy, records, _at(8, day=11))
    assert next_day == GreetingDecision(GreetingKind.MORNING, HIDDEN_ID)


@pytest.mark.parametrize(
    ("minutes", "kind"),
    [(20, GreetingKind.HELLO), (119, GreetingKind.HELLO), (120, GreetingKind.OKAERI)],
)
def test_120分以上ならおかえり(records: GreetingRecords, minutes: int, kind: GreetingKind) -> None:
    now = _at(15)
    records.mark_seen(HIDDEN_ID, now - timedelta(minutes=minutes))
    assert _decide(_policy(), records, now) == GreetingDecision(kind, HIDDEN_ID)


def test_初めてで朝でなければおかえり(records: GreetingRecords) -> None:
    assert _decide(_policy(), records, _at(15)) == GreetingDecision(GreetingKind.OKAERI, HIDDEN_ID)


def test_朝がすんでいて120分以上ならおかえり(records: GreetingRecords) -> None:
    policy = _policy()
    records.mark_greeted(GreetingDecision(GreetingKind.MORNING, HIDDEN_ID), _at(7))
    assert _decide(policy, records, _at(9, 30)) == GreetingDecision(GreetingKind.OKAERI, HIDDEN_ID)


def test_知らない人の朝は覚えた人の朝を使わない(records: GreetingRecords) -> None:
    policy = _policy()
    unknown = _decide(policy, records, _at(8), who=None)
    assert unknown == GreetingDecision(GreetingKind.MORNING, None)
    records.mark_greeted(unknown, _at(8))
    assert records.morning_done(UNKNOWN, _at(8).date()) is True
    assert records.morning_done(HIDDEN_ID, _at(8).date()) is False
    assert records.last_seen(HIDDEN_ID) is None
    # 覚えた人には、まだおはようと言える
    known = _decide(policy, records, _at(8, 1))
    assert known == GreetingDecision(GreetingKind.MORNING, HIDDEN_ID)


def test_覚えた人の朝も知らない人の朝を使わない(records: GreetingRecords) -> None:
    policy = _policy()
    records.mark_greeted(GreetingDecision(GreetingKind.MORNING, HIDDEN_ID), _at(8))
    assert records.morning_done(UNKNOWN, _at(8).date()) is False
    assert _decide(policy, records, _at(8, 1), who=None) == GreetingDecision(
        GreetingKind.MORNING, None
    )


def test_見えなかった時間は人ごと(records: GreetingRecords) -> None:
    policy = _policy()
    now = _at(15)
    records.mark_seen(HIDDEN_ID, now - timedelta(minutes=1))
    assert _decide(policy, records, now) is None
    assert _decide(policy, records, now, who=OTHER_ID) is not None
    assert _decide(policy, records, now, who=None, has_known_people=False) is not None


# --- 言葉 ---


def test_覚えた人の言葉には呼び名を入れる() -> None:
    policy = _policy()
    assert policy.phrase(
        GreetingDecision(GreetingKind.MORNING, HIDDEN_ID), HIDDEN_NAME
    ) == texts.fill(texts.MORNING_KNOWN, HIDDEN_NAME)
    assert policy.phrase(
        GreetingDecision(GreetingKind.OKAERI, HIDDEN_ID), HIDDEN_NAME
    ) == texts.fill(texts.OKAERI_KNOWN, HIDDEN_NAME)
    assert policy.phrase(
        GreetingDecision(GreetingKind.HELLO, HIDDEN_ID), HIDDEN_NAME
    ) == texts.fill(texts.HELLO_KNOWN[0], HIDDEN_NAME)


def test_知らない人の言葉には呼び名を入れない() -> None:
    policy = _policy()
    assert policy.phrase(GreetingDecision(GreetingKind.MORNING, None), None) == (
        texts.MORNING_UNKNOWN
    )
    assert policy.phrase(GreetingDecision(GreetingKind.OKAERI, None), None) == (
        texts.OKAERI_UNKNOWN
    )
    assert (
        policy.phrase(GreetingDecision(GreetingKind.HELLO, None), None) == (texts.HELLO_UNKNOWN[0])
    )


def test_呼び名がなければ知らない人の言葉にする() -> None:
    policy = _policy()
    assert policy.phrase(GreetingDecision(GreetingKind.OKAERI, HIDDEN_ID), None) == (
        texts.OKAERI_UNKNOWN
    )


def test_やっほーの選び方は差し替えられる() -> None:
    seen: list[tuple[str, ...]] = []

    def last(options: Sequence[str]) -> str:
        seen.append(tuple(options))
        return options[-1]

    policy = GreetingPolicy(PresenceConfig(), choose=last)
    hello = GreetingDecision(GreetingKind.HELLO, HIDDEN_ID)
    assert policy.phrase(hello, HIDDEN_NAME) == texts.fill(texts.HELLO_KNOWN[-1], HIDDEN_NAME)
    assert seen == [texts.HELLO_KNOWN]
    assert (
        policy.phrase(GreetingDecision(GreetingKind.HELLO, None), None) == (texts.HELLO_UNKNOWN[-1])
    )


def test_既定の選び方は3つのどれか() -> None:
    policy = GreetingPolicy(PresenceConfig())
    hello = GreetingDecision(GreetingKind.HELLO, HIDDEN_ID)
    expected = {texts.fill(t, HIDDEN_NAME) for t in texts.HELLO_KNOWN}
    for _ in range(20):
        assert policy.phrase(hello, HIDDEN_NAME) in expected


# --- 記録 ---


def test_記録は再起動の後も引き継ぐ(store: StateStore) -> None:
    records = GreetingRecords.load(store)
    records.mark_greeted(GreetingDecision(GreetingKind.MORNING, HIDDEN_ID), _at(8))
    records.mark_seen(HIDDEN_ID, _at(8, 5))
    records.mark_greeted(GreetingDecision(GreetingKind.MORNING, None), _at(8, 6))
    records.save()

    again = GreetingRecords.load(store)
    assert again.last_seen(HIDDEN_ID) == _at(8, 5)
    assert again.morning_done(HIDDEN_ID, _at(8).date()) is True
    assert again.last_seen(UNKNOWN) == _at(8, 6)
    assert again.morning_done(UNKNOWN, _at(8).date()) is True
    # 再起動の後も、すぐにはあいさつしない。朝ももう言わない
    policy = _policy()
    assert _decide(policy, again, _at(8, 10)) is None
    assert _decide(policy, again, _at(9)) == GreetingDecision(GreetingKind.HELLO, HIDDEN_ID)


def test_保存の形(store: StateStore) -> None:
    records = GreetingRecords.load(store)
    records.mark_greeted(GreetingDecision(GreetingKind.MORNING, HIDDEN_ID), _at(8))
    data = json.loads((store.directory / f"{STORE_KEY}.json").read_text(encoding="utf-8"))
    assert data == {
        "version": 1,
        "last_seen": {HIDDEN_ID: _at(8).isoformat()},
        "morning": {HIDDEN_ID: "2026-03-10"},
    }


def test_あいさつしたらすぐ保存する(store: StateStore) -> None:
    records = GreetingRecords.load(store)
    records.mark_greeted(GreetingDecision(GreetingKind.HELLO, HIDDEN_ID), _at(15))
    assert GreetingRecords.load(store).last_seen(HIDDEN_ID) == _at(15)
    assert GreetingRecords.load(store).morning_done(HIDDEN_ID, _at(15).date()) is False


def test_見ている間の記録は間を空けて保存する(store: StateStore) -> None:
    records = GreetingRecords.load(store)
    records.mark_seen(HIDDEN_ID, _at(15))  # 初めては保存する
    assert GreetingRecords.load(store).last_seen(HIDDEN_ID) == _at(15)
    records.mark_seen(HIDDEN_ID, _at(15, 0, 30))
    assert records.last_seen(HIDDEN_ID) == _at(15, 0, 30)  # 手元はすぐ変わる
    assert GreetingRecords.load(store).last_seen(HIDDEN_ID) == _at(15)
    later = _at(15) + timedelta(seconds=SAVE_INTERVAL)
    records.mark_seen(HIDDEN_ID, later)
    assert GreetingRecords.load(store).last_seen(HIDDEN_ID) == later


def test_見ている間の記録は手元で更新する(records: GreetingRecords) -> None:
    records.mark_seen(HIDDEN_ID, _at(15))
    records.mark_seen(HIDDEN_ID, _at(15, 30))
    assert records.last_seen(HIDDEN_ID) == _at(15, 30)


def test_1人の記録を消す(store: StateStore) -> None:
    records = GreetingRecords.load(store)
    records.mark_greeted(GreetingDecision(GreetingKind.MORNING, HIDDEN_ID), _at(8))
    records.mark_greeted(GreetingDecision(GreetingKind.MORNING, OTHER_ID), _at(8))
    records.forget(HIDDEN_ID)
    for loaded in (records, GreetingRecords.load(store)):
        assert loaded.last_seen(HIDDEN_ID) is None
        assert loaded.morning_done(HIDDEN_ID, _at(8).date()) is False
        assert loaded.last_seen(OTHER_ID) == _at(8)
        assert loaded.morning_done(OTHER_ID, _at(8).date()) is True


def test_すべての記録を消す(store: StateStore) -> None:
    records = GreetingRecords.load(store)
    records.mark_greeted(GreetingDecision(GreetingKind.MORNING, HIDDEN_ID), _at(8))
    records.mark_greeted(GreetingDecision(GreetingKind.MORNING, None), _at(8))
    records.forget(None)
    for loaded in (records, GreetingRecords.load(store)):
        assert loaded.last_seen(HIDDEN_ID) is None
        assert loaded.last_seen(UNKNOWN) is None
        assert loaded.morning_done(UNKNOWN, _at(8).date()) is False


def test_ない人の記録を消しても何もしない(records: GreetingRecords) -> None:
    records.forget(HIDDEN_ID)
    assert records.last_seen(HIDDEN_ID) is None


def test_保存がなければ空(records: GreetingRecords) -> None:
    assert records.last_seen(HIDDEN_ID) is None
    assert records.morning_done(HIDDEN_ID, _at(8).date()) is False


def test_壊れた保存は空として扱う(store: StateStore) -> None:
    store.directory.mkdir(parents=True)
    (store.directory / f"{STORE_KEY}.json").write_text("{not json", encoding="utf-8")
    records = GreetingRecords.load(store)
    assert records.last_seen(HIDDEN_ID) is None


@pytest.mark.parametrize(
    "data",
    [
        {"version": 2, "last_seen": {}, "morning": {}},
        {"version": True, "last_seen": {}, "morning": {}},
        {"version": 1, "last_seen": [], "morning": {}},
        {"version": 1, "last_seen": {}, "morning": []},
        {"version": 1, "last_seen": {HIDDEN_ID: 5}, "morning": {}},
        {"version": 1, "last_seen": {HIDDEN_ID: "きのう"}, "morning": {}},
        {"version": 1, "last_seen": {HIDDEN_ID: "2026-03-10T08:00:00"}, "morning": {}},  # 時差なし
        {"version": 1, "last_seen": {}, "morning": {HIDDEN_ID: "あさ"}},
        {"version": 1, "last_seen": {}, "morning": {HIDDEN_ID: 3}},
    ],
)
def test_形のおかしい保存は空として扱う(
    store: StateStore, data: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    store.save(STORE_KEY, data)  # type: ignore[arg-type]
    with caplog.at_level(logging.DEBUG):
        records = GreetingRecords.load(store)
    assert records.last_seen(HIDDEN_ID) is None
    assert records.morning_done(HIDDEN_ID, _at(8).date()) is False
    assert HIDDEN_ID not in caplog.text
    # 次の保存で上書きする
    records.mark_greeted(GreetingDecision(GreetingKind.HELLO, OTHER_ID), _at(15))
    assert GreetingRecords.load(store).last_seen(OTHER_ID) == _at(15)


def test_保存に失敗しても見ている間の記録は止まらない(
    store: StateStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    records = GreetingRecords.load(store)

    def fail(*_: object) -> None:
        raise OSError("disk")

    monkeypatch.setattr(store, "save", fail)
    with caplog.at_level(logging.WARNING):
        records.mark_seen(HIDDEN_ID, _at(15))
        records.mark_greeted(GreetingDecision(GreetingKind.HELLO, HIDDEN_ID), _at(15, 1))
    assert records.last_seen(HIDDEN_ID) == _at(15, 1)
    assert "OSError" in caplog.text
    with pytest.raises(OSError):
        records.forget(None)


def test_ログに名前と時刻を出さない(store: StateStore, caplog: pytest.LogCaptureFixture) -> None:
    policy = _policy()
    with caplog.at_level(logging.DEBUG):
        records = GreetingRecords.load(store)
        decision = _decide(policy, records, _at(8))
        assert decision is not None
        records.mark_greeted(decision, _at(8))
        records.mark_seen(HIDDEN_ID, _at(8, 30))
        policy.phrase(decision, HIDDEN_NAME)
        records.save()
        GreetingRecords.load(store)
        records.forget(HIDDEN_ID)
    for hidden in (HIDDEN_ID, HIDDEN_NAME, "2026-03-10", "08:00", "おはよう"):
        assert hidden not in caplog.text


def test_時間帯の設定はそのまま使う(records: GreetingRecords) -> None:
    policy = _policy(greet_hours=["09:00", "10:00"])
    assert _decide(policy, records, _at(8, 59)) is None
    assert _decide(policy, records, _at(9, 0)) is not None
    assert PresenceConfig.model_validate({"greet_hours": ["09:00", "10:00"]}).greet_hours == (
        time(9, 0),
        time(10, 0),
    )
