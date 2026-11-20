"""朝と夜のお話（talk）のテストで共通に使う偽物（12.1）。

`from talk_fakes import ...` で読む（tests/features/talk が import の道にある）。

偽の天気・偽の注意報・偽の会話の AI は、決めた結果（または台本の結果を順に）返し、
受け取ったものを覚える。`delay_s` を決めると返す前にその長さだけ止まり、`release()` で
早めに放せる。`delay_s=HOLD_S` にすると、放すまで返さない（時間切れのテストを、実際の
長さを待たずに決まった順で進めるため）。`called` は呼ばれると立つ。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Generic, TypeVar

from osekkai.features.conversation.api import ChatResult, Turn
from osekkai.features.routine.api import RoutineStatus, TaskMoment
from osekkai.features.talk.model import (
    AlertsResult,
    Condition,
    DayForecast,
    WeatherResult,
)

T = TypeVar("T")

MSG_SCRIPT_EMPTY = "偽物の台本が尽きました"

# 用意のスレッド（`talk-*`）の終わりを待つ上限（実際の秒）
SETTLE_S = 2.0

# 放すまで返さないときの遅れ。放し忘れでもテストが止まり続けないように上限を置く
HOLD_S = 30.0


def settle_preparation(timeout_s: float = SETTLE_S) -> None:
    """動いている用意のスレッド（名前が `talk-` で始まる）が終わるまで、実際の時間で待つ。

    偽の時計を進める前に呼ぶ。用意のスレッドが動き出す前に時計が進むと、用意の締め切りを
    偽の時計で過ぎてしまい、取れたはずの天気や豆知識が「間に合わなかった」になるため。
    放すまで返さない偽物で止めたスレッドは、上限まで待って先に進む。
    """
    deadline = time.monotonic() + timeout_s
    current = threading.current_thread()
    for thread in threading.enumerate():
        if thread is not current and thread.name.startswith("talk-"):
            thread.join(max(0.0, deadline - time.monotonic()))


def sunny_forecast(day: date) -> DayForecast:
    """決めなかったときの偽の天気の予報（はれ、18 度と 9 度、降水確率 10%）。"""
    return DayForecast(day, Condition.SUNNY, 18, 9, 10)


class _Gate:
    """返す前に止まる仕組みと、呼ばれたことの印。"""

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.called = threading.Event()
        self._released = threading.Event()
        self._lock = threading.Lock()

    def _wait(self) -> None:
        self.called.set()
        if self.delay_s > 0:
            self._released.wait(self.delay_s)

    def release(self) -> None:
        """止まっている呼び出し（とこの後の呼び出し）をすぐ返させる。"""
        self._released.set()


class _Script(Generic[T]):
    """決めた結果を何度でも返すか、台本の結果を順に返す。"""

    def __init__(self, fixed: T, script: Iterable[T] | None) -> None:
        self._fixed = fixed
        self._script = None if script is None else list(script)

    def next(self) -> T:
        if self._script is None:
            return self._fixed
        if not self._script:
            raise AssertionError(MSG_SCRIPT_EMPTY)
        return self._script.pop(0)


@dataclass(frozen=True)
class WeatherCall:
    """偽の天気が受け取った 1 回分。"""

    day: date
    timeout_s: float


class FakeWeather(_Gate):
    """偽の天気（`WeatherPort`）。

    `result` を決めなければ、聞かれた日のはれの予報（`sunny_forecast`）を返す。
    `script` を渡すと、その結果を順に返し、尽きたら `AssertionError` を投げる。
    """

    def __init__(
        self,
        result: WeatherResult | None = None,
        *,
        script: Iterable[WeatherResult] | None = None,
        delay_s: float = 0.0,
    ) -> None:
        super().__init__(delay_s)
        self._results: _Script[WeatherResult | None] = _Script(result, script)
        self.calls: list[WeatherCall] = []

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)

    def daily(self, day: date, timeout_s: float) -> WeatherResult:
        with self._lock:
            self.calls.append(WeatherCall(day, timeout_s))
            result = self._results.next()
        self._wait()
        return sunny_forecast(day) if result is None else result


@dataclass(frozen=True)
class AlertsCall:
    """偽の注意報が受け取った 1 回分。"""

    timeout_s: float


class FakeAlerts(_Gate):
    """偽の注意報（`AlertPort`）。

    `result` を決めなければ、出ていない（空）を返す。
    `script` を渡すと、その結果を順に返し、尽きたら `AssertionError` を投げる。
    """

    def __init__(
        self,
        result: AlertsResult = frozenset(),
        *,
        script: Iterable[AlertsResult] | None = None,
        delay_s: float = 0.0,
    ) -> None:
        super().__init__(delay_s)
        self._results: _Script[AlertsResult] = _Script(result, script)
        self.calls: list[AlertsCall] = []

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)

    def active(self, timeout_s: float) -> AlertsResult:
        with self._lock:
            self.calls.append(AlertsCall(timeout_s))
            result = self._results.next()
        self._wait()
        return result


@dataclass(frozen=True)
class ChatCall:
    """偽の会話の AI が受け取った 1 回分。"""

    system: str
    turns: tuple[Turn, ...]
    timeout_s: float


class FakeChatModel(_Gate):
    """偽の会話の AI（おしゃべりの公開の口の `ChatModelPort`）。

    台本の結果を順に返し、尽きたら `AssertionError` を投げる。
    """

    def __init__(self, script: Iterable[ChatResult], *, delay_s: float = 0.0) -> None:
        super().__init__(delay_s)
        self._script = list(script)
        self.calls: list[ChatCall] = []

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)

    def reply(self, system: str, turns: Sequence[Turn], timeout_s: float) -> ChatResult:
        with self._lock:
            self.calls.append(ChatCall(system, tuple(turns), timeout_s))
            if not self._script:
                raise AssertionError(MSG_SCRIPT_EMPTY)
            result = self._script.pop(0)
        self._wait()
        return result


def make_moment(
    start: datetime,
    *,
    notice_min: int = 10,
    has_notice: bool = True,
    notice_said: bool = False,
    ended_at: datetime | None = None,
) -> TaskMoment:
    """今日のタスクの時刻を作る。予告の時刻は、タスクの時刻から予告の分を引いた時刻。"""
    return TaskMoment(
        start=start,
        notice_at=start - timedelta(minutes=notice_min),
        has_notice=has_notice,
        notice_said=notice_said,
        ended_at=ended_at,
    )


def make_status(
    now: datetime,
    *,
    next_task_at: datetime | None = None,
    in_task: bool = False,
    asleep: bool = False,
    wake_at: datetime | None = None,
    bedtime: TaskMoment | None = None,
    first_task: TaskMoment | None = None,
) -> RoutineStatus:
    """日課の公開の状況を作る。`updated_at` は `now`。決めなかった項目は何もない。"""
    return RoutineStatus(
        next_task_at=next_task_at,
        in_task=in_task,
        asleep=asleep,
        updated_at=now,
        wake_at=wake_at,
        bedtime=bedtime,
        first_task=first_task,
    )
