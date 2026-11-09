"""いまラジコンを使えるかの判定と、端末への知らせの文（要件 11.1、11.2、11.3、11.5、11.6）。"""

from datetime import datetime, time
from types import MappingProxyType

import pytest

from osekkai.features.remote.availability import (
    MESSAGES,
    Availability,
    judge_availability,
    message_for,
    within_hours,
)

DAY = (time(9, 0), time(17, 0))
NIGHT = (time(22, 0), time(6, 0))


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 8, hour, minute)


# --- 時間帯 ---


def test_始まりの時刻は含む() -> None:
    assert within_hours(time(9, 0), DAY) is True  # 11.5


def test_終わりの時刻は含まない() -> None:
    assert within_hours(time(17, 0), DAY) is False  # 11.5
    assert within_hours(time(16, 59, 59), DAY) is True


def test_始まりの前は外() -> None:
    assert within_hours(time(8, 59, 59), DAY) is False


@pytest.mark.parametrize(
    ("now", "inside"),
    [
        (time(22, 0), True),
        (time(23, 59), True),
        (time(0, 0), True),
        (time(5, 59), True),
        (time(6, 0), False),
        (time(12, 0), False),
        (time(21, 59), False),
    ],
)
def test_日をまたぐ時間帯(now: time, inside: bool) -> None:
    assert within_hours(now, NIGHT) is inside


def test_始まりと終わりが同じ帯は空() -> None:
    assert within_hours(time(9, 0), (time(9, 0), time(9, 0))) is False


# --- 判定の順 ---


def test_時間帯の中でおやすみでなければ使える() -> None:
    assert judge_availability(synced=True, now=_at(10), quiet=False, hours=DAY) is Availability.OK


def test_時間帯の外は使えない() -> None:
    result = judge_availability(synced=True, now=_at(17), quiet=False, hours=DAY)
    assert result is Availability.OUT_OF_HOURS  # 11.1


def test_おやすみは使えない() -> None:
    result = judge_availability(synced=True, now=_at(10), quiet=True, hours=DAY)
    assert result is Availability.QUIET  # 11.2


def test_おやすみは時間帯の外より先() -> None:
    result = judge_availability(synced=True, now=_at(20), quiet=True, hours=DAY)
    assert result is Availability.QUIET


@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("hour", [10, 20])
def test_時刻が確かでなければ何より先に使えない(quiet: bool, hour: int) -> None:
    result = judge_availability(synced=False, now=_at(hour), quiet=quiet, hours=DAY)
    assert result is Availability.NO_CLOCK  # 11.3


@pytest.mark.parametrize("quiet", [False, True])
def test_時刻がなければ使えない(quiet: bool) -> None:
    result = judge_availability(synced=True, now=None, quiet=quiet, hours=DAY)
    assert result is Availability.NO_CLOCK  # 11.3


def test_日をまたぐ時間帯で判定する() -> None:
    assert judge_availability(synced=True, now=_at(1), quiet=False, hours=NIGHT) is Availability.OK
    result = judge_availability(synced=True, now=_at(6), quiet=False, hours=NIGHT)
    assert result is Availability.OUT_OF_HOURS


def test_使った時間の上限はない() -> None:
    # 何度判定しても、時間帯の中なら使える（11.6）
    results = {
        judge_availability(synced=True, now=_at(9, m), quiet=False, hours=DAY) for m in range(60)
    }
    assert results == {Availability.OK}


# --- 知らせの文 ---


def test_使えないときの知らせは要件の文() -> None:
    assert MESSAGES[Availability.OUT_OF_HOURS] == "いまは ラジコンの じかんじゃないよ"  # 11.1
    assert MESSAGES[Availability.QUIET] == "いまは ラジコンの じかんじゃないよ"  # 11.2
    assert MESSAGES[Availability.NO_CLOCK] == "いまは つかえないよ"  # 11.3
    assert MESSAGES[Availability.BUSY] == (
        "ローバーは いま ほかの ことを しているよ。ちょっと まってね"
    )  # 12.2
    assert MESSAGES[Availability.OTHER_OPERATOR] == "いまは ほかの ひとが そうさ ちゅう"  # 3.2


def test_おやすみの文は時間帯の外と同じ() -> None:
    assert MESSAGES[Availability.QUIET] == MESSAGES[Availability.OUT_OF_HOURS]


def test_使えないものはすべて知らせの文がある() -> None:
    for value in Availability:
        if value is Availability.OK:
            continue
        assert message_for(value) == MESSAGES[value]
        assert message_for(value)


def test_使えるときは知らせがない() -> None:
    assert Availability.OK not in MESSAGES
    assert message_for(Availability.OK) is None


def test_知らせの表は書き換えられない() -> None:
    assert isinstance(MESSAGES, MappingProxyType)
