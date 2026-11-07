"""門の判定（5.1、5.2、5.3、5.6、9.2）。"""

from datetime import date, datetime, time
from pathlib import Path

import pytest

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.storage import StateStore
from osekkai.features.conversation import texts
from osekkai.features.conversation.gate import GateReason, check_gate, within_hours
from osekkai.features.conversation.usage import DailyUsage

DAY = (time(7, 0), time(21, 30))
NIGHT = (time(22, 0), time(6, 0))
NOON = datetime(2026, 11, 8, 12, 0)
LATE = datetime(2026, 11, 8, 23, 0)


def _gate(
    *,
    ready: bool = True,
    synchronized: bool = True,
    now: datetime = NOON,
    hours: tuple[time, time] = DAY,
    usage_reached: bool = False,
) -> GateReason | None:
    return check_gate(
        ready=ready,
        synchronized=synchronized,
        now=now,
        hours=hours,
        usage_reached=usage_reached,
    )


# --- 理由と言葉の表 ---


def test_理由の値は言葉の表のキーと同じ() -> None:
    assert {reason.value for reason in GateReason} == set(texts.GATE_REPLIES)


def test_どの理由でも言葉を引ける() -> None:
    for reason in GateReason:
        assert texts.GATE_REPLIES[reason]


def test_言葉は要件のとおり() -> None:
    assert texts.GATE_REPLIES[GateReason.NOT_SET_UP] == texts.SAY_UNAVAILABLE
    assert texts.GATE_REPLIES[GateReason.CLOCK_UNSYNCED] == texts.SAY_UNAVAILABLE
    assert texts.GATE_REPLIES[GateReason.OUT_OF_HOURS] == texts.SAY_SLEEP_TIME
    assert texts.GATE_REPLIES[GateReason.LIMIT] == texts.SAY_LIMIT


# --- 判定の順 ---


def test_どれでもなければ通す() -> None:
    assert _gate() is None


def test_鍵がなければほかの理由より先() -> None:
    assert (
        _gate(ready=False, synchronized=False, now=LATE, usage_reached=True)
        is GateReason.NOT_SET_UP
    )


def test_時刻が確かでなければ時間帯と上限より先() -> None:
    assert _gate(synchronized=False, now=LATE, usage_reached=True) is GateReason.CLOCK_UNSYNCED


def test_時刻が確かでなければ時間帯の中でも止める() -> None:
    assert _gate(synchronized=False) is GateReason.CLOCK_UNSYNCED


def test_時間帯の外は上限より先() -> None:
    assert _gate(now=LATE, usage_reached=True) is GateReason.OUT_OF_HOURS


def test_上限に達していれば止める() -> None:
    assert _gate(usage_reached=True) is GateReason.LIMIT


def test_時間帯は時刻の部分だけを見る() -> None:
    assert _gate(now=datetime(2026, 11, 8, 7, 0)) is None
    assert _gate(now=datetime(2026, 11, 8, 21, 30)) is GateReason.OUT_OF_HOURS


def test_日をまたぐ帯でも順は同じ() -> None:
    assert _gate(now=LATE, hours=NIGHT) is None
    assert _gate(now=NOON, hours=NIGHT) is GateReason.OUT_OF_HOURS
    assert _gate(now=LATE, hours=NIGHT, usage_reached=True) is GateReason.LIMIT


def test_上限ちょうどで止め_1つ手前は通す(tmp_path: Path, clock: FakeClock) -> None:
    today = date(2026, 1, 1)  # FakeClock の始まりの日
    usage = DailyUsage.load(StateStore(tmp_path / "conversation", clock), clock)
    usage.add_one(today)
    assert _gate(usage_reached=usage.reached(today, 2)) is None  # 1 回目の後、上限 2
    usage.add_one(today)
    assert _gate(usage_reached=usage.reached(today, 2)) is GateReason.LIMIT  # ちょうど 2 回


# --- 時間帯の境目 ---


@pytest.mark.parametrize(
    ("now", "inside"),
    [
        (time(6, 59, 59), False),
        (time(7, 0), True),  # 始めを含む
        (time(12, 0), True),
        (time(21, 29, 59), True),
        (time(21, 30), False),  # 終わりを含まない
        (time(0, 0), False),
    ],
)
def test_昼の帯の境目(now: time, inside: bool) -> None:
    assert within_hours(now, DAY) is inside


@pytest.mark.parametrize(
    ("now", "inside"),
    [
        (time(21, 59, 59), False),
        (time(22, 0), True),  # 始めを含む
        (time(23, 59, 59), True),
        (time(0, 0), True),  # 日をまたぐ
        (time(5, 59, 59), True),
        (time(6, 0), False),  # 終わりを含まない
        (time(12, 0), False),
    ],
)
def test_日をまたぐ帯の境目(now: time, inside: bool) -> None:
    assert within_hours(now, NIGHT) is inside


def test_始めと終わりが同じ帯は空() -> None:
    assert within_hours(time(7, 0), (time(7, 0), time(7, 0))) is False
    assert within_hours(time(12, 0), (time(7, 0), time(7, 0))) is False


def test_既定の設定の帯で判定できる() -> None:
    from osekkai.features.conversation.config import ConversationConfig

    hours = ConversationConfig().hours
    assert within_hours(time(7, 0), hours) is True
    assert within_hours(time(21, 30), hours) is False
