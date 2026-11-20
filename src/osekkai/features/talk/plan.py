"""話す時の判定（1.3、2.1〜2.6、3.1〜3.8、4.1、8.2、8.5）。

時計も記録も読まない純粋な関数。今の時刻・時刻が確かか・静かか・日課の状況・済んだかを
受け取り、夜と朝のそれぞれについて「何もしない・用意する・話す・諦める」を決める。

- `decide_kind` は 1 つの種類の判定を返す。何もしないときは `Idle`
- `decide` は、使うと設定され、まだ済んでいない種類だけを判定し、何かするもの
  （`Prepare`・`Speak`・`Miss`）だけを夜・朝の順に返す。種類ごとに多くても 1 つ。
  何もしないときは空の一覧
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from osekkai.features.routine.api import RoutineStatus, TaskMoment
from osekkai.features.talk.config import TalkConfig
from osekkai.features.talk.model import TalkKind

PREPARE_LEAD: Final = timedelta(minutes=5)  # 予告（後の道ではタスク）の時刻の何分前から用意するか
MISS_AFTER_END: Final = timedelta(minutes=60)  # 朝の最初のタスクの結果から何分で諦めるか（3.6）


@dataclass(frozen=True)
class Idle:
    """何もしない。"""


@dataclass(frozen=True)
class Prepare:
    """話す準備（天気・注意報・豆知識の取り寄せ）を始める、または待つ。"""

    kind: TalkKind
    weather_day: date


@dataclass(frozen=True)
class Speak:
    """今話す。"""

    kind: TalkKind
    weather_day: date


@dataclass(frozen=True)
class Miss:
    """その日のその種類は話さない（締め切りを過ぎた）。"""

    kind: TalkKind


Decision = Idle | Prepare | Speak | Miss


def decide(
    now: datetime,
    synced: bool,
    quiet: bool,
    status: RoutineStatus | None,
    done: Callable[[TalkKind], bool],
    config: TalkConfig,
) -> list[Decision]:
    """夜と朝の判定のうち、何かするものだけを夜・朝の順に返す。

    時刻が確かでない（8.5）・静かな状態（8.2）・日課が動いていないときは空。
    使わないと設定した方（1.3）と記録で済んだ方（4.1）は返さない。済んだかは、使う方だけ問う。
    """
    if not synced or quiet or status is None:
        return []
    decisions: list[Decision] = []
    for kind, used in ((TalkKind.NIGHT, config.night), (TalkKind.MORNING, config.morning)):
        if not used or done(kind):
            continue
        decision = decide_kind(kind, now, status)
        if not isinstance(decision, Idle):
            decisions.append(decision)
    return decisions


def decide_kind(kind: TalkKind, now: datetime, status: RoutineStatus) -> Decision:
    """1 つの種類の判定。設定・記録・時刻の確かさ・静かさは見ない（`decide` が見る）。"""
    if kind is TalkKind.NIGHT:
        return _night(now, status)
    return _morning(now, status)


def _night(now: datetime, status: RoutineStatus) -> Decision:
    """夜: 予告に続けて話し、寝るタスクの時刻で諦める（2.1〜2.6）。天気はあした。"""
    bedtime = status.bedtime
    if bedtime is None or status.asleep:
        return Idle()
    if now >= bedtime.start:
        return Miss(TalkKind.NIGHT)
    return _after_notice(
        TalkKind.NIGHT, now, bedtime, status.in_task, now.date() + timedelta(days=1)
    )


def _morning(now: datetime, status: RoutineStatus) -> Decision:
    """朝: 予告の道（3.1、3.2、3.4）か、最初のタスクの後の道（3.3、3.5、3.6）。天気はきょう。

    起床時刻そのものは契機にしない（3.8）。予告の時刻が起床時刻より前なら後の道になる。
    """
    first = status.first_task
    if first is None or status.asleep:
        return Idle()
    today = now.date()
    wake_at = status.wake_at
    notice_after_wake = wake_at is None or first.notice_at >= wake_at
    if notice_after_wake and now < first.start:
        return _after_notice(TalkKind.MORNING, now, first, status.in_task, today)
    if first.ended_at is None:
        if now >= first.start - PREPARE_LEAD:
            return Prepare(TalkKind.MORNING, today)
        return Idle()
    if now >= first.ended_at + MISS_AFTER_END:
        return Miss(TalkKind.MORNING)
    if status.in_task:
        return Prepare(TalkKind.MORNING, today)
    return Speak(TalkKind.MORNING, today)


def _after_notice(
    kind: TalkKind, now: datetime, moment: TaskMoment, in_task: bool, weather_day: date
) -> Decision:
    """予告に続けて話す規則。予告を言い終えて（言葉がなければ予告の時刻で）最中でなければ話す。

    話す前でも、予告の時刻の 5 分前からは用意する。それより前は何もしない。
    """
    ready = now >= moment.notice_at and (not moment.has_notice or moment.notice_said)
    if ready and not in_task:
        return Speak(kind, weather_day)
    if now >= moment.notice_at - PREPARE_LEAD:
        return Prepare(kind, weather_day)
    return Idle()
