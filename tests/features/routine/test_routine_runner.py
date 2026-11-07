"""1 つのタスクの進み方（3.7、4.1〜4.9、4.11〜4.13、5.1〜5.3、6.1、11.1）のテスト。"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pytest
from routine_fixtures import build_config, task_dict
from scripted_io import ScriptedCut, ScriptedHooks, ScriptedIO

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.types import Face, LeaseRevoked
from osekkai.features.routine.config import TaskConfig
from osekkai.features.routine.records import TaskResult
from osekkai.features.routine.runner import (
    POLL_S,
    RunResult,
    TaskRunner,
    TaskStopped,
    Texts,
    Timings,
)
from osekkai.features.routine.schedule import DueNotice, DueTask, Phase
from osekkai.features.routine.texts import LATER_SPEECH, LCD_LATER, lcd_done

MONDAY = date(2026, 4, 6)
CALL_NAME = "テスト"
CALL = f"{CALL_NAME} かおを あらおう"
PRAISE = f"すごいね {CALL_NAME}"
READY = f"{CALL_NAME} じゅんびしてね"
NOTICE = f"もうすぐ {CALL_NAME} おでかけ"
REMIND_S = 5 * 60.0
LATER_S = 30 * 60.0

WAKE = {"weekday": "06:00", "holiday": "07:00"}  # 寝るタスクに要る起床時刻
WAIT_PHASES = frozenset({Phase.CALLING, Phase.LATER})


def make_task(**fields: Any) -> TaskConfig:
    base: dict[str, Any] = {
        "call": "{name} かおを あらおう",
        "praise": "すごいね {name}",
    }
    base.update(fields)
    return build_config([task_dict("wash", **base)], wake=WAKE).tasks[0]


def other_due(task_id: str = "leave", **fields: Any) -> DueTask:
    task = build_config([task_dict(task_id, at="07:30", lcd="ﾃﾞｶｹﾙ", **fields)], wake=WAKE).tasks[0]
    return DueTask(task, MONDAY)


def notice_of() -> DueNotice:
    task = build_config(
        [task_dict("leave", at="07:30", lcd="ﾃﾞｶｹﾙ", notice="もうすぐ {name} おでかけ")]
    ).tasks[0]
    return DueNotice(task, MONDAY)


class Run:
    """1 回の進み方の結果と、呼ばれた操作。"""

    def __init__(
        self,
        clock: FakeClock,
        task: TaskConfig,
        *,
        io: ScriptedIO | None = None,
        hooks: ScriptedHooks | None = None,
        timings: Timings | None = None,
    ) -> None:
        self.io = io or ScriptedIO(clock)
        self.hooks = hooks or ScriptedHooks(clock)
        self.runner = TaskRunner(self.io, clock, Texts(CALL_NAME), timings or Timings())
        self.task = task

    def go(self) -> RunResult:
        return self.runner.run(self.task, self.hooks, on_done=self._done)

    def _done(self) -> None:
        self.io.calls.append(("on_done",))


def calls_said(io: ScriptedIO, text: str) -> list[float]:
    return [at for at, said in io.said_at if said == text]


# --- 声かけ・催促・あとで・スキップ ---


def test_始めは_バックライト_表示_見回し_LED_押下を捨てて_声かけの順(clock: FakeClock) -> None:
    run = Run(clock, make_task())
    run.io.press_at(10)
    run.go()
    assert run.io.calls[:6] == [
        ("backlight", True),
        ("show", "ｶｵ ｱﾗｳ", Face.CALL.text()),
        ("look_around",),
        ("led", True),
        ("clear_presses",),
        ("say", CALL),
    ]


@pytest.mark.parametrize("max_reminders", [0, 2, 3])
def test_押されないと_催促を上限まで_その後あとで_30分でスキップ(
    clock: FakeClock, max_reminders: int
) -> None:
    run = Run(clock, make_task(max_reminders=max_reminders))
    result = run.go()
    assert result == RunResult(TaskResult.SKIPPED, None)
    calls = calls_said(run.io, CALL)
    assert len(calls) == max_reminders + 1  # 4.13
    assert calls == [i * REMIND_S for i in range(max_reminders + 1)]
    assert calls_said(run.io, LATER_SPEECH) == [(max_reminders + 1) * REMIND_S]
    assert run.io.elapsed() == (max_reminders + 1) * REMIND_S + LATER_S


def test_既定の上限では_声かけは3回(clock: FakeClock) -> None:
    run = Run(clock, make_task())
    run.go()
    assert len(calls_said(run.io, CALL)) == 3
    assert run.io.count("look_around") == 3


def test_催促は_表示と見回しとLEDのあとに声かけ(clock: FakeClock) -> None:
    run = Run(clock, make_task(max_reminders=1))
    run.io.press_at(REMIND_S + 10)
    run.go()
    second = [i for i, c in enumerate(run.io.calls) if c == ("say", CALL)][1]
    assert run.io.calls[second - 4 : second + 1] == [
        ("show", "ｶｵ ｱﾗｳ", Face.CALL.text()),
        ("look_around",),
        ("led", True),
        ("clear_presses",),
        ("say", CALL),
    ]


def test_あとでは_LEDを消して_アトデネを出して話す(clock: FakeClock) -> None:
    run = Run(clock, make_task(max_reminders=0))
    run.go()
    at = run.io.calls.index(("say", LATER_SPEECH))
    assert run.io.calls[at - 2 : at + 1] == [
        ("led", False),
        ("show", LCD_LATER, Face.NORMAL.text()),
        ("say", LATER_SPEECH),
    ]


def test_スキップは_LEDを消して終わる(clock: FakeClock) -> None:
    run = Run(clock, make_task(max_reminders=0))
    run.go()
    assert run.io.calls[-2:] == [("led", False), ("clear_presses",)]


def test_あとでの間の押下は_できた(clock: FakeClock) -> None:
    run = Run(clock, make_task(max_reminders=0))
    run.io.press_at(REMIND_S + LATER_S - 1)
    assert run.go() == RunResult(TaskResult.DONE, None)


def test_催促の間隔とあとでの長さを差し替えられる(clock: FakeClock) -> None:
    timings = Timings(remind_s=lambda task: 10.0, later_s=20.0)
    run = Run(clock, make_task(), timings=timings)
    assert run.go().result is TaskResult.SKIPPED
    assert calls_said(run.io, CALL) == [0.0, 10.0, 20.0]
    assert run.io.elapsed() == 50.0


def test_既定の催促の間隔は_タスクの設定の分(clock: FakeClock) -> None:
    run = Run(clock, make_task(remind_min=2, max_reminders=1))
    run.go()
    assert calls_said(run.io, CALL) == [0.0, 120.0]


def test_ボタンの待ちは_1秒以下ずつに区切る(clock: FakeClock) -> None:
    timings = Timings(remind_s=lambda task: 2.5, later_s=3.5)
    run = Run(clock, make_task(), timings=timings)
    run.go()
    waits = [float(str(c[1])) for c in run.io.calls if c[0] == "wait_press"]
    assert waits
    assert all(0 < w <= POLL_S for w in waits)
    assert run.io.elapsed() == pytest.approx(3 * 2.5 + 3.5)


# --- できた ---


def test_話している間の押下は_できたに数える(clock: FakeClock) -> None:
    io = ScriptedIO(clock, speech_s=4.0)
    io.press_while_saying.add(CALL)
    run = Run(clock, make_task(), io=io)
    assert run.go() == RunResult(TaskResult.DONE, None)
    assert run.io.elapsed() < 10  # 待たずに、すぐできたになる


def test_催促を話している間の押下も_できたに数える(clock: FakeClock) -> None:
    io = ScriptedIO(clock, speech_s=4.0)
    run = Run(clock, make_task(), io=io)
    io.press_at(4.0 + REMIND_S + 1.0)  # 2 回目の声かけの途中
    assert run.go().result is TaskResult.DONE
    assert len(calls_said(io, CALL)) == 2


def test_始める前の押下は_数えない(clock: FakeClock) -> None:
    run = Run(clock, make_task(max_reminders=0))
    run.io.press_at(0)
    assert run.go().result is TaskResult.SKIPPED


def test_できたら_LEDを消し_記録の後に星の数と喜んだ顔_ダンスでほめて_2回うなずく(
    clock: FakeClock,
) -> None:
    hooks = ScriptedHooks(clock, stars=11)
    run = Run(clock, make_task(), hooks=hooks)

    def done() -> None:
        run.io.calls.append(("on_done",))
        hooks.stars = 12  # 記録の呼び出しで星が増える

    run.io.press_at(30)
    result = run.runner.run(run.task, hooks, on_done=done)
    assert result == RunResult(TaskResult.DONE, None)
    at = run.io.calls.index(("on_done",))
    assert run.io.calls[at - 1 : at + 5] == [
        ("led", False),
        ("on_done",),
        ("show", lcd_done(12), Face.HAPPY.text()),
        ("dance_and_say", PRAISE),
        ("nod", 2),
        ("clear_presses",),
    ]
    assert lcd_done(12).endswith("12")


def test_寝るタスクは_ほめた後にうなずかない(clock: FakeClock) -> None:
    run = Run(clock, make_task(at="21:00", sleep=True))
    run.io.press_at(5)
    assert run.go().result is TaskResult.DONE
    assert run.io.count("nod") == 0
    assert run.io.count("dance_and_say") == 1


def test_呼び名を言葉に入れる(clock: FakeClock) -> None:
    run = Run(clock, make_task())
    run.io.press_at(5)
    run.go()
    assert run.io.spoken() == [CALL, PRAISE]


# --- 準備 ---


def test_準備は_声かけの前に話し_押されるまで待ってから声かけ(clock: FakeClock) -> None:
    run = Run(clock, make_task(ready="{name} じゅんびしてね"))
    run.io.press_at(5000)  # 準備の待ちに時間の上限はない
    run.io.press_at(5010)
    assert run.go().result is TaskResult.DONE
    assert run.io.spoken() == [READY, CALL, PRAISE]
    assert calls_said(run.io, CALL) == [5000.0]
    ready = run.io.calls.index(("say", READY))
    assert run.io.calls[: ready + 1] == [
        ("backlight", True),
        ("show", "ｶｵ ｱﾗｳ", Face.NORMAL.text()),
        ("clear_presses",),
        ("say", READY),
    ]
    first_call = run.io.calls.index(("say", CALL))
    assert run.io.count("backlight") == 1
    assert run.io.calls[first_call - 4 : first_call + 1] == [
        ("show", "ｶｵ ｱﾗｳ", Face.CALL.text()),
        ("look_around",),
        ("led", True),
        ("clear_presses",),
        ("say", CALL),
    ]
    assert Phase.READY in run.hooks.phases
    assert run.hooks.phases.index(Phase.READY) < run.hooks.phases.index(Phase.CALLING)


def test_準備の待ちに時刻のタスクが来ると中断(clock: FakeClock) -> None:
    due = other_due()  # 割り込めないタスク。準備の待ちには割り込む（5.2）
    hooks = ScriptedHooks(clock, cuts=[ScriptedCut(50, due, frozenset({Phase.READY}))])
    run = Run(clock, make_task(ready="{name} じゅんびしてね"), hooks=hooks)
    assert run.go() == RunResult(TaskResult.INTERRUPTED, due)
    assert CALL not in run.io.spoken()
    assert run.io.calls[-2:] == [("led", False), ("clear_presses",)]
    assert 50 <= run.io.elapsed() <= 50 + POLL_S


@pytest.mark.parametrize("ready", [None, "{name} じゅんびしてね"])
def test_止める知らせがあると_待ちの区切りで_結果を返さずに抜ける(
    clock: FakeClock, ready: str | None
) -> None:
    hooks = ScriptedHooks(clock)
    hooks.stop_after = 100.0  # 準備の待ち（上限なし）の中でも、催促の待ちの中でも止める
    run = Run(clock, make_task(ready=ready), hooks=hooks)
    with pytest.raises(TaskStopped):
        run.go()
    assert ("on_done",) not in run.io.calls
    assert 100 <= run.io.elapsed() <= 100 + POLL_S


# --- 割り込み ---


def test_催促中に割り込めるタスクが来ると中断(clock: FakeClock) -> None:
    due = other_due(firm=True)
    hooks = ScriptedHooks(clock, cuts=[ScriptedCut(400, due, WAIT_PHASES)])
    run = Run(clock, make_task(), hooks=hooks)
    assert run.go() == RunResult(TaskResult.INTERRUPTED, due)
    assert len(calls_said(run.io, CALL)) == 2
    assert run.io.calls[-2:] == [("led", False), ("clear_presses",)]
    assert 400 <= run.io.elapsed() <= 400 + POLL_S
    assert set(hooks.phases) == {Phase.CALLING}


def test_あとでの間にも割り込める(clock: FakeClock) -> None:
    due = other_due(firm=True)
    hooks = ScriptedHooks(clock, cuts=[ScriptedCut(REMIND_S + 60, due, WAIT_PHASES)])
    run = Run(clock, make_task(max_reminders=0), hooks=hooks)
    assert run.go() == RunResult(TaskResult.INTERRUPTED, due)
    assert Phase.LATER in hooks.phases


def test_ほめる間は割り込まない(clock: FakeClock) -> None:
    due = other_due(sleep=True)
    io = ScriptedIO(clock, speech_s=5.0)
    hooks = ScriptedHooks(clock, cuts=[ScriptedCut(0, due, frozenset({Phase.PRAISING}))])
    run = Run(clock, make_task(), io=io, hooks=hooks)
    io.press_at(20)
    assert run.go() == RunResult(TaskResult.DONE, None)
    assert Phase.PRAISING not in hooks.phases
    assert io.count("nod") == 1


# --- 予告 ---


def test_待ちの間の予告を話して記録し_今のタスクの表示に戻して待ちを続ける(
    clock: FakeClock,
) -> None:
    notice = notice_of()
    io = ScriptedIO(clock, speech_s=10.0)
    hooks = ScriptedHooks(clock, notices=[(100, notice)])
    run = Run(clock, make_task(max_reminders=1), io=io, hooks=hooks)
    io.press_at(400)
    assert run.go().result is TaskResult.DONE
    assert hooks.said == [notice]
    at = io.calls.index(("say", NOTICE))
    assert io.calls[at - 2 : at + 2] == [
        ("show", "ﾃﾞｶｹﾙ", Face.NORMAL.text()),
        ("nod", 1),
        ("say", NOTICE),
        ("show", "ｶｵ ｱﾗｳ", Face.CALL.text()),
    ]
    assert io.spoken() == [CALL, NOTICE, CALL, PRAISE]
    # 予告の間も待ちの残りは減る（声かけの話し終わり 10 秒 + 間隔 300 秒）
    assert calls_said(io, CALL) == [0.0, 10.0 + REMIND_S]


def test_あとでの間の予告の後は_アトデネの表示に戻す(clock: FakeClock) -> None:
    notice = notice_of()
    hooks = ScriptedHooks(clock, notices=[(REMIND_S + 10, notice)])
    run = Run(clock, make_task(max_reminders=0), hooks=hooks)
    run.go()
    at = run.io.calls.index(("say", NOTICE))
    assert run.io.calls[at + 1] == ("show", LCD_LATER, Face.NORMAL.text())


def test_予告を話している間の押下は_できたに数える(clock: FakeClock) -> None:
    notice = notice_of()
    io = ScriptedIO(clock, speech_s=4.0)
    io.press_while_saying.add(NOTICE)
    hooks = ScriptedHooks(clock, notices=[(20, notice)])
    run = Run(clock, make_task(), io=io, hooks=hooks)
    assert run.go().result is TaskResult.DONE
    assert io.elapsed() < 40


# --- 終わりの後始末と失敗 ---


def test_タスクの後に押下が残らない(clock: FakeClock) -> None:
    io = ScriptedIO(clock, speech_s=5.0)
    run = Run(clock, make_task(), io=io)
    for at in (20, 21, 22, 23):  # できた後、ほめている間にも押す
        io.press_at(at)
    assert run.go().result is TaskResult.DONE
    assert io.calls[-1] == ("clear_presses",)
    assert io.past_presses() == 0


def test_声が出せなくても_型の名前を記録して先に進む(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    io = ScriptedIO(clock)
    io.fail_speech.update({CALL, PRAISE})
    run = Run(clock, make_task(), io=io)
    io.press_at(30)
    with caplog.at_level(logging.DEBUG, logger="osekkai.features.routine"):
        assert run.go().result is TaskResult.DONE
    assert io.count("nod") == 1
    assert "SpeechFailed" in caplog.text
    for hidden in (CALL_NAME, CALL, PRAISE, "ｶｵ ｱﾗｳ"):
        assert hidden not in caplog.text


def test_資源を取り上げられたら_そのまま外に出す(clock: FakeClock) -> None:
    io = ScriptedIO(clock)
    io.revoke_after = 100
    run = Run(clock, make_task(), io=io)
    with pytest.raises(LeaseRevoked):
        run.go()


def test_ログにはタスクのidと結果だけを出す(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    run = Run(clock, make_task(max_reminders=0))
    with caplog.at_level(logging.DEBUG, logger="osekkai.features.routine"):
        run.go()
    assert "wash" in caplog.text
    assert "skipped" in caplog.text
    for hidden in (CALL_NAME, CALL, LATER_SPEECH, LCD_LATER, "ｶｵ ｱﾗｳ"):
        assert hidden not in caplog.text
