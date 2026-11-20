"""話す時の判定（1.3、2.1〜2.6、3.1〜3.8、4.1、8.2、8.5）。

`decide` は、夜と朝のそれぞれについて、何かするときだけ判定を返す（何もしない方は返さない）。
1 つの種類ごとの判定は `decide_kind` で、何もしないときは `Idle` を返す。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from talk_fakes import make_moment, make_status

from osekkai.features.routine.api import RoutineStatus, TaskMoment
from osekkai.features.talk.config import TalkConfig
from osekkai.features.talk.model import TalkKind
from osekkai.features.talk.plan import (
    MISS_AFTER_END,
    PREPARE_LEAD,
    Decision,
    Idle,
    Miss,
    Prepare,
    Speak,
    decide,
    decide_kind,
)

NIGHT = TalkKind.NIGHT
MORNING = TalkKind.MORNING
TODAY = date(2026, 11, 20)
TOMORROW = date(2026, 11, 21)
WAKE = datetime(2026, 11, 20, 6, 30)
BED_START = datetime(2026, 11, 20, 20, 30)  # 予告は 10 分前の 20:20
BED_NOTICE = BED_START - timedelta(minutes=10)
FIRST_START = datetime(2026, 11, 20, 7, 0)  # 予告は 10 分前の 6:50（起床の後）
FIRST_NOTICE = FIRST_START - timedelta(minutes=10)
CONFIG = TalkConfig(enabled=True)


def _never_done(kind: TalkKind) -> bool:
    return False


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 20, hour, minute, second)


def _night(
    now: datetime,
    *,
    bedtime: TaskMoment | None,
    in_task: bool = False,
    asleep: bool = False,
) -> Decision:
    status = make_status(now, in_task=in_task, asleep=asleep, bedtime=bedtime)
    return decide_kind(NIGHT, now, status)


def _morning(
    now: datetime,
    *,
    first: TaskMoment | None,
    wake_at: datetime | None = WAKE,
    in_task: bool = False,
    asleep: bool = False,
) -> Decision:
    status = make_status(now, in_task=in_task, asleep=asleep, wake_at=wake_at, first_task=first)
    return decide_kind(MORNING, now, status)


def test_用意の早さは5分で_諦めは結果から60分() -> None:
    assert timedelta(minutes=5) == PREPARE_LEAD
    assert timedelta(minutes=60) == MISS_AFTER_END


# ---- 夜（2.1〜2.6） ---------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "bedtime", "in_task", "asleep", "expected"),
    [
        # 予告の 5 分前より前は何もしない
        (
            BED_NOTICE - timedelta(minutes=5, seconds=1),
            make_moment(BED_START),
            False,
            False,
            Idle(),
        ),
        # 予告のちょうど 5 分前から用意する
        (
            BED_NOTICE - timedelta(minutes=5),
            make_moment(BED_START),
            False,
            False,
            Prepare(NIGHT, TOMORROW),
        ),
        # 予告の時刻を過ぎても、予告を言い終えていなければ用意のまま（2.1）
        (
            BED_NOTICE + timedelta(seconds=30),
            make_moment(BED_START),
            False,
            False,
            Prepare(NIGHT, TOMORROW),
        ),
        # 予告を言い終えて、日課が最中でなければ話す（2.1）
        (
            BED_NOTICE + timedelta(seconds=30),
            make_moment(BED_START, notice_said=True),
            False,
            False,
            Speak(NIGHT, TOMORROW),
        ),
        # 予告を言い終えても、日課が最中なら待つ（2.3）
        (
            BED_NOTICE + timedelta(seconds=30),
            make_moment(BED_START, notice_said=True),
            True,
            False,
            Prepare(NIGHT, TOMORROW),
        ),
        # 予告の言葉がなければ、予告の時刻で話す（2.2）
        (
            BED_NOTICE,
            make_moment(BED_START, has_notice=False),
            False,
            False,
            Speak(NIGHT, TOMORROW),
        ),
        # 予告の言葉がなくても、予告の時刻より前は話さない
        (
            BED_NOTICE - timedelta(seconds=1),
            make_moment(BED_START, has_notice=False),
            False,
            False,
            Prepare(NIGHT, TOMORROW),
        ),
        # 予告の言葉がなくても、日課が最中なら待つ（2.3）
        (
            BED_NOTICE,
            make_moment(BED_START, has_notice=False),
            True,
            False,
            Prepare(NIGHT, TOMORROW),
        ),
        # 寝るタスクの時刻ちょうどで諦める（2.4、2.5）
        (BED_START, make_moment(BED_START, notice_said=True), False, False, Miss(NIGHT)),
        (BED_START + timedelta(minutes=1), make_moment(BED_START), False, False, Miss(NIGHT)),
        # おやすみ中は諦めも付けず、何もしない（2.5）
        (BED_START + timedelta(minutes=1), make_moment(BED_START), False, True, Idle()),
        (BED_NOTICE, make_moment(BED_START, notice_said=True), False, True, Idle()),
        # 寝るタスクがない日は何もしない（2.6）
        (BED_NOTICE, None, False, False, Idle()),
    ],
    ids=[
        "予告の5分前より前",
        "予告のちょうど5分前",
        "予告の後でも言い終える前",
        "予告を言い終えた",
        "言い終えたが最中",
        "予告の言葉なしで予告の時刻",
        "予告の言葉なしで予告の前",
        "予告の言葉なしで最中",
        "寝るタスクの時刻ちょうど",
        "寝るタスクの時刻の後",
        "おやすみ中_時刻の後",
        "おやすみ中_予告の後",
        "寝るタスクがない",
    ],
)
def test_夜の判定(
    now: datetime, bedtime: TaskMoment | None, in_task: bool, asleep: bool, expected: Decision
) -> None:
    assert _night(now, bedtime=bedtime, in_task=in_task, asleep=asleep) == expected


# ---- 朝（3.1〜3.8） ---------------------------------------------------------

DEFERRED_START = datetime(2026, 11, 20, 6, 35)  # 予告は 6:25（おやすみの間。起床は 6:30）
DEFERRED_ENDED = datetime(2026, 11, 20, 6, 50)


@pytest.mark.parametrize(
    ("now", "first", "wake_at", "in_task", "asleep", "expected"),
    [
        # ---- 予告の道（予告の時刻が起床の後）: 夜と同じ規則（3.1、3.2、3.4）
        (
            FIRST_NOTICE - timedelta(minutes=5, seconds=1),
            make_moment(FIRST_START),
            WAKE,
            False,
            False,
            Idle(),
        ),
        (
            FIRST_NOTICE - timedelta(minutes=5),
            make_moment(FIRST_START),
            WAKE,
            False,
            False,
            Prepare(MORNING, TODAY),
        ),
        (
            FIRST_NOTICE + timedelta(seconds=10),
            make_moment(FIRST_START),
            WAKE,
            False,
            False,
            Prepare(MORNING, TODAY),
        ),
        (
            FIRST_NOTICE + timedelta(seconds=10),
            make_moment(FIRST_START, notice_said=True),
            WAKE,
            False,
            False,
            Speak(MORNING, TODAY),
        ),
        (
            FIRST_NOTICE + timedelta(seconds=10),
            make_moment(FIRST_START, notice_said=True),
            WAKE,
            True,
            False,
            Prepare(MORNING, TODAY),
        ),
        (
            FIRST_NOTICE,
            make_moment(FIRST_START, has_notice=False),
            WAKE,
            False,
            False,
            Speak(MORNING, TODAY),
        ),
        # 予告の時刻が起床時刻ちょうどでも、予告の道（起床の後とみなす）
        (
            FIRST_NOTICE,
            make_moment(FIRST_START, has_notice=False),
            FIRST_NOTICE,
            False,
            False,
            Speak(MORNING, TODAY),
        ),
        # 起床時刻が分からないときは予告の道
        (
            FIRST_NOTICE,
            make_moment(FIRST_START, has_notice=False),
            None,
            False,
            False,
            Speak(MORNING, TODAY),
        ),
        # ---- 後の道: タスクの時刻を過ぎた（3.5）。結果がまだなら用意して待つ
        (FIRST_START, make_moment(FIRST_START), WAKE, True, False, Prepare(MORNING, TODAY)),
        (
            FIRST_START,
            make_moment(FIRST_START, notice_said=True),
            WAKE,
            False,
            False,
            Prepare(MORNING, TODAY),
        ),
        # 結果が付いて、日課が最中でなければ話す（3.3、3.5）
        (
            FIRST_START + timedelta(minutes=20),
            make_moment(FIRST_START, ended_at=FIRST_START + timedelta(minutes=15)),
            WAKE,
            False,
            False,
            Speak(MORNING, TODAY),
        ),
        # 結果が付いても、日課が最中なら待つ（3.4）
        (
            FIRST_START + timedelta(minutes=20),
            make_moment(FIRST_START, ended_at=FIRST_START + timedelta(minutes=15)),
            WAKE,
            True,
            False,
            Prepare(MORNING, TODAY),
        ),
        # 結果からちょうど 60 分で諦める（3.6）
        (
            FIRST_START + timedelta(minutes=75),
            make_moment(FIRST_START, ended_at=FIRST_START + timedelta(minutes=15)),
            WAKE,
            False,
            False,
            Miss(MORNING),
        ),
        (
            FIRST_START + timedelta(minutes=75) - timedelta(seconds=1),
            make_moment(FIRST_START, ended_at=FIRST_START + timedelta(minutes=15)),
            WAKE,
            False,
            False,
            Speak(MORNING, TODAY),
        ),
        # ---- 後の道: 予告がおやすみの間（3.3）。予告の時刻にも起床時刻にも話さない（3.8）
        (_at(6, 25), make_moment(DEFERRED_START), WAKE, False, False, Idle()),
        (_at(6, 25), make_moment(DEFERRED_START, has_notice=False), WAKE, False, False, Idle()),
        (
            WAKE,
            make_moment(DEFERRED_START, has_notice=False),
            WAKE,
            False,
            False,
            Prepare(MORNING, TODAY),
        ),
        (
            WAKE,
            make_moment(DEFERRED_START, notice_said=True),
            WAKE,
            False,
            False,
            Prepare(MORNING, TODAY),
        ),
        # タスクの時刻の 5 分前より前は何もしない
        (
            DEFERRED_START - timedelta(minutes=5, seconds=1),
            make_moment(DEFERRED_START),
            WAKE,
            False,
            False,
            Idle(),
        ),
        (
            DEFERRED_START - timedelta(minutes=5),
            make_moment(DEFERRED_START),
            WAKE,
            False,
            False,
            Prepare(MORNING, TODAY),
        ),
        (
            DEFERRED_ENDED,
            make_moment(DEFERRED_START, ended_at=DEFERRED_ENDED),
            WAKE,
            False,
            False,
            Speak(MORNING, TODAY),
        ),
        (
            DEFERRED_ENDED + MISS_AFTER_END,
            make_moment(DEFERRED_START, ended_at=DEFERRED_ENDED),
            WAKE,
            False,
            False,
            Miss(MORNING),
        ),
        # ---- おやすみ中・タスクのない日は何もしない（3.7、3.8）
        (_at(6, 25), make_moment(DEFERRED_START), WAKE, False, True, Idle()),
        (
            DEFERRED_ENDED + MISS_AFTER_END,
            make_moment(DEFERRED_START, ended_at=DEFERRED_ENDED),
            WAKE,
            False,
            True,
            Idle(),
        ),
        (FIRST_NOTICE, None, WAKE, False, False, Idle()),
    ],
    ids=[
        "予告の道_5分前より前",
        "予告の道_ちょうど5分前",
        "予告の道_言い終える前",
        "予告の道_言い終えた",
        "予告の道_言い終えたが最中",
        "予告の道_予告の言葉なし",
        "予告の道_予告が起床時刻ちょうど",
        "予告の道_起床時刻なし",
        "時刻を過ぎた_最中",
        "時刻を過ぎた_結果なし",
        "時刻を過ぎた_結果あり",
        "時刻を過ぎた_結果ありで最中",
        "時刻を過ぎた_結果から60分ちょうど",
        "時刻を過ぎた_結果から60分の1秒前",
        "おやすみの間_予告の時刻",
        "おやすみの間_予告の言葉なしで予告の時刻",
        "おやすみの間_起床時刻",
        "おやすみの間_予告を言い終えた",
        "おやすみの間_タスクの5分前より前",
        "おやすみの間_タスクのちょうど5分前",
        "おやすみの間_結果あり",
        "おやすみの間_結果から60分ちょうど",
        "おやすみ中",
        "おやすみ中_60分の後",
        "タスクがない",
    ],
)
def test_朝の判定(
    now: datetime,
    first: TaskMoment | None,
    wake_at: datetime | None,
    in_task: bool,
    asleep: bool,
    expected: Decision,
) -> None:
    assert _morning(now, first=first, wake_at=wake_at, in_task=in_task, asleep=asleep) == expected


# ---- 共通（1.3、4.1、8.2、8.5） -----------------------------------------------


def _both_speak_status(now: datetime) -> RoutineStatus:
    """夜も朝も「話す」になる状況（予告の言葉なしで、どちらも予告の時刻を過ぎた）。"""
    return make_status(
        now,
        wake_at=WAKE,
        bedtime=make_moment(_at(22, 5), has_notice=False),
        first_task=make_moment(_at(22, 0), has_notice=False),
    )


NOW_BOTH = _at(21, 55)


def test_夜も朝も話す時は_夜_朝の順に1つずつ返す() -> None:
    result = decide(NOW_BOTH, True, False, _both_speak_status(NOW_BOTH), _never_done, CONFIG)
    assert result == [Speak(NIGHT, TOMORROW), Speak(MORNING, TODAY)]


def test_何もしない方は返さない() -> None:
    now = BED_NOTICE
    status = make_status(now, bedtime=make_moment(BED_START, has_notice=False))
    assert decide(now, True, False, status, _never_done, CONFIG) == [Speak(NIGHT, TOMORROW)]


@pytest.mark.parametrize(
    ("synced", "quiet", "with_status"),
    [
        (False, False, True),  # 時刻が確かでない（8.5）
        (True, True, True),  # 静かな状態（8.2）
        (True, False, False),  # 日課が動いていない
    ],
    ids=["時刻が確かでない", "静か", "日課なし"],
)
def test_話せない状態では夜も朝も何もしない(synced: bool, quiet: bool, with_status: bool) -> None:
    status = _both_speak_status(NOW_BOTH) if with_status else None
    assert decide(NOW_BOTH, synced, quiet, status, _never_done, CONFIG) == []


@pytest.mark.parametrize(
    ("night", "morning", "expected"),
    [
        (True, True, [Speak(NIGHT, TOMORROW), Speak(MORNING, TODAY)]),
        (False, True, [Speak(MORNING, TODAY)]),
        (True, False, [Speak(NIGHT, TOMORROW)]),
        (False, False, []),
    ],
    ids=["両方", "朝だけ", "夜だけ", "どちらも使わない"],
)
def test_使わないと設定した方は何もしない(
    night: bool, morning: bool, expected: list[Decision]
) -> None:
    config = TalkConfig(enabled=True, night=night, morning=morning)
    assert (
        decide(NOW_BOTH, True, False, _both_speak_status(NOW_BOTH), _never_done, config) == expected
    )


@pytest.mark.parametrize(
    ("done_kinds", "expected"),
    [
        (frozenset({NIGHT}), [Speak(MORNING, TODAY)]),
        (frozenset({MORNING}), [Speak(NIGHT, TOMORROW)]),
        (frozenset({NIGHT, MORNING}), []),
    ],
    ids=["夜は済んだ", "朝は済んだ", "どちらも済んだ"],
)
def test_記録で済んだ方は何もしない(
    done_kinds: frozenset[TalkKind], expected: list[Decision]
) -> None:
    asked: list[TalkKind] = []

    def done(kind: TalkKind) -> bool:
        asked.append(kind)
        return kind in done_kinds

    assert decide(NOW_BOTH, True, False, _both_speak_status(NOW_BOTH), done, CONFIG) == expected
    assert sorted(asked, key=lambda k: k.value) == sorted([NIGHT, MORNING], key=lambda k: k.value)


def test_済んだ方は諦めも返さない() -> None:
    now = BED_START
    status = make_status(now, bedtime=make_moment(BED_START))
    assert decide(now, True, False, status, lambda kind: kind is NIGHT, CONFIG) == []
    assert decide(now, True, False, status, _never_done, CONFIG) == [Miss(NIGHT)]


def test_使わない方の記録は問い合わせない() -> None:
    asked: list[TalkKind] = []

    def done(kind: TalkKind) -> bool:
        asked.append(kind)
        return False

    config = TalkConfig(enabled=True, night=False)
    decide(NOW_BOTH, True, False, _both_speak_status(NOW_BOTH), done, config)
    assert asked == [MORNING]


def test_夜の天気の日は次の日_年をまたいでも() -> None:
    now = datetime(2026, 12, 31, 20, 20)
    bedtime = make_moment(datetime(2026, 12, 31, 20, 30), has_notice=False)
    status = make_status(now, bedtime=bedtime)
    assert decide_kind(NIGHT, now, status) == Speak(NIGHT, date(2027, 1, 1))
