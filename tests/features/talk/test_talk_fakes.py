"""テスト用の偽物（偽の天気・偽の注意報・偽の会話の AI・日課の状況の作り方）を確かめる（12.1）。"""

import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta

import pytest
from talk_fakes import (
    HOLD_S,
    MSG_SCRIPT_EMPTY,
    AlertsCall,
    ChatCall,
    FakeAlerts,
    FakeChatModel,
    FakeWeather,
    WeatherCall,
    make_moment,
    make_status,
    sunny_forecast,
)

from osekkai.features.conversation.api import (
    ChatModelPort,
    Failed,
    FailureKind,
    Refused,
    Replied,
    Turn,
)
from osekkai.features.routine.api import RoutineStatus, TaskMoment
from osekkai.features.talk.model import (
    AlertKind,
    AlertPort,
    AlertsProblem,
    AlertsUnavailable,
    Condition,
    DayForecast,
    WeatherPort,
    WeatherProblem,
    WeatherUnavailable,
)

DAY = date(2026, 11, 20)
NEXT_DAY = date(2026, 11, 21)
NOW = datetime(2026, 11, 20, 20, 30)
ASK = Turn("user", "まめちしきを ひとつ")


# --- 型（mypy で確かめる） ---


def test_偽物はそれぞれのポートと口の型を満たす() -> None:
    weather: WeatherPort = FakeWeather()
    alerts: AlertPort = FakeAlerts()
    chat: ChatModelPort = FakeChatModel([Replied("はい")])
    assert weather is not None and alerts is not None and chat is not None


# --- 偽の天気 ---


def test_偽の天気は決めなければ聞かれた日のはれの予報を返す() -> None:
    weather = FakeWeather()
    assert weather.daily(DAY, 3.0) == sunny_forecast(DAY)
    assert weather.daily(NEXT_DAY, 3.0) == DayForecast(NEXT_DAY, Condition.SUNNY, 18, 9, 10)


def test_偽の天気は決めた結果を何度でも返す() -> None:
    gone = WeatherUnavailable(WeatherProblem.TIMEOUT)
    weather = FakeWeather(gone)
    assert [weather.daily(DAY, 1.0) for _ in range(3)] == [gone, gone, gone]


def test_偽の天気は台本の結果を順に返し尽きたら知らせる() -> None:
    first = DayForecast(DAY, Condition.RAIN, 12, 7, 80)
    second = WeatherUnavailable(WeatherProblem.CONNECTION)
    weather = FakeWeather(script=[first, second])
    assert weather.daily(DAY, 1.0) == first
    assert weather.daily(DAY, 1.0) == second
    with pytest.raises(AssertionError, match=MSG_SCRIPT_EMPTY):
        weather.daily(DAY, 1.0)


def test_偽の天気は受け取った日と時間切れを覚える() -> None:
    weather = FakeWeather()
    assert not weather.called.is_set()
    weather.daily(DAY, 3.0)
    weather.daily(NEXT_DAY, 2.5)
    assert weather.calls == [WeatherCall(DAY, 3.0), WeatherCall(NEXT_DAY, 2.5)]
    assert weather.call_count == 2
    assert weather.called.is_set()


# --- 偽の注意報 ---


def test_偽の注意報は決めなければ出ていないを返す() -> None:
    assert FakeAlerts().active(3.0) == frozenset()


def test_偽の注意報は決めた結果を何度でも返す() -> None:
    kinds = frozenset({AlertKind.HEAVY_RAIN, AlertKind.THUNDER})
    alerts = FakeAlerts(kinds)
    assert alerts.active(1.0) == kinds
    assert alerts.active(1.0) == kinds


def test_偽の注意報は台本の結果を順に返し尽きたら知らせる() -> None:
    gone = AlertsUnavailable(AlertsProblem.STALE)
    alerts = FakeAlerts(script=[gone, frozenset({AlertKind.HEAVY_SNOW})])
    assert alerts.active(1.0) == gone
    assert alerts.active(1.0) == frozenset({AlertKind.HEAVY_SNOW})
    with pytest.raises(AssertionError, match=MSG_SCRIPT_EMPTY):
        alerts.active(1.0)


def test_偽の注意報は受け取った時間切れと回数を覚える() -> None:
    alerts = FakeAlerts()
    alerts.active(3.0)
    alerts.active(1.5)
    assert alerts.calls == [AlertsCall(3.0), AlertsCall(1.5)]
    assert alerts.call_count == 2
    assert alerts.called.is_set()


# --- 偽の会話の AI ---


def test_偽の会話の_AI_は台本の結果を順に返し尽きたら知らせる() -> None:
    chat = FakeChatModel([Replied("きりんは たって ねるよ"), Refused(), Failed(FailureKind.BUSY)])
    assert chat.reply("きまり", [ASK], 3.0) == Replied("きりんは たって ねるよ")
    assert chat.reply("きまり", [ASK], 3.0) == Refused()
    assert chat.reply("きまり", [ASK], 3.0) == Failed(FailureKind.BUSY)
    with pytest.raises(AssertionError, match=MSG_SCRIPT_EMPTY):
        chat.reply("きまり", [ASK], 3.0)


def test_偽の会話の_AI_は受け取った決まりの文とやりとりを覚える() -> None:
    chat = FakeChatModel([Replied("はい"), Replied("うん")])
    turns = [ASK]
    chat.reply("よるの きまり", turns, 3.0)
    turns.append(Turn("assistant", "はい"))  # 渡した後に変えても、覚えたものは変わらない
    chat.reply("あさの きまり", [ASK], 2.0)
    assert chat.calls == [
        ChatCall("よるの きまり", (ASK,), 3.0),
        ChatCall("あさの きまり", (ASK,), 2.0),
    ]
    assert chat.call_count == 2
    assert chat.called.is_set()


# --- 遅れと放し ---


def _call_in_thread(fn: Callable[[], object]) -> tuple[threading.Thread, list[object]]:
    out: list[object] = []
    thread = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    thread.start()
    return thread, out


@pytest.mark.parametrize("which", ["weather", "alerts", "chat"])
def test_止めた偽物は放すまで返さず放すとすぐ返す(which: str) -> None:
    weather = FakeWeather(delay_s=HOLD_S)
    alerts = FakeAlerts(delay_s=HOLD_S)
    chat = FakeChatModel([Replied("はい")], delay_s=HOLD_S)
    calls = {
        "weather": (weather, lambda: weather.daily(DAY, 3.0), sunny_forecast(DAY)),
        "alerts": (alerts, lambda: alerts.active(3.0), frozenset()),
        "chat": (chat, lambda: chat.reply("きまり", [ASK], 3.0), Replied("はい")),
    }
    fake, call, expected = calls[which]
    thread, out = _call_in_thread(call)
    assert fake.called.wait(2.0)
    thread.join(0.05)
    assert thread.is_alive()  # 止まっている
    assert out == []
    fake.release()
    thread.join(2.0)
    assert not thread.is_alive()
    assert out == [expected]


def test_遅れを決めた偽物はその長さだけ待って返す() -> None:
    weather = FakeWeather(delay_s=0.05)
    started = time.monotonic()
    assert weather.daily(DAY, 3.0) == sunny_forecast(DAY)
    assert time.monotonic() - started >= 0.04


def test_放した後の呼び出しは待たない() -> None:
    alerts = FakeAlerts(delay_s=HOLD_S)
    alerts.release()
    started = time.monotonic()
    assert alerts.active(3.0) == frozenset()
    assert time.monotonic() - started < 1.0


def test_遅れのない偽物は待たない() -> None:
    chat = FakeChatModel([Replied("はい")])
    started = time.monotonic()
    chat.reply("きまり", [ASK], 3.0)
    assert time.monotonic() - started < 1.0


# --- 日課の状況の作り方 ---


def test_タスクの時刻の作り方は予告の時刻を予告の分から決める() -> None:
    start = datetime(2026, 11, 20, 21, 0)
    assert make_moment(start) == TaskMoment(
        start=start,
        notice_at=start - timedelta(minutes=10),
        has_notice=True,
        notice_said=False,
        ended_at=None,
    )
    ended = datetime(2026, 11, 20, 21, 5)
    assert make_moment(
        start, notice_min=5, has_notice=False, notice_said=True, ended_at=ended
    ) == TaskMoment(
        start=start,
        notice_at=start - timedelta(minutes=5),
        has_notice=False,
        notice_said=True,
        ended_at=ended,
    )


def test_状況の作り方は決めなければ何もない状況を今の時刻で作る() -> None:
    assert make_status(NOW) == RoutineStatus(
        next_task_at=None,
        in_task=False,
        asleep=False,
        updated_at=NOW,
        wake_at=None,
        bedtime=None,
        first_task=None,
    )


def test_状況の作り方は寝るタスクと朝の最初のタスクと起床時刻を詰める() -> None:
    bedtime = make_moment(datetime(2026, 11, 20, 21, 0))
    first = make_moment(datetime(2026, 11, 20, 7, 0), has_notice=False)
    wake = datetime(2026, 11, 20, 6, 30)
    status = make_status(
        NOW,
        next_task_at=bedtime.start,
        in_task=True,
        asleep=True,
        wake_at=wake,
        bedtime=bedtime,
        first_task=first,
    )
    assert status == RoutineStatus(
        next_task_at=bedtime.start,
        in_task=True,
        asleep=True,
        updated_at=NOW,
        wake_at=wake,
        bedtime=bedtime,
        first_task=first,
    )
