"""1 つのタスクの進み方（3.7、4.1〜4.9、4.11〜4.13、5.1〜5.3、6.1、11.1）。

資源には `RoutineIO` だけで触れる。時間は `Clock.monotonic()` で測る。
ログには、タスクの id・結果・例外の型の名前だけを出す（言葉や表示は出さない）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from osekkai.core.arbiter import Lease
from osekkai.core.clock import Clock
from osekkai.core.lcd_text import center
from osekkai.core.types import Face, LeaseRevoked, Resource, SpeechFailed
from osekkai.features.routine.config import TaskConfig
from osekkai.features.routine.records import TaskResult
from osekkai.features.routine.schedule import DueNotice, DueTask, Phase
from osekkai.features.routine.texts import LATER_SPEECH, LCD_LATER, fill_name, lcd_done

logger = logging.getLogger(__name__)

POLL_S = 1.0  # ボタンの待ちを区切る長さ。区切りごとに割り込みと予告を聞く
LATER_S = 30 * 60.0  # あとでの受付の長さ（4.5）
DANCE_JOIN_S = 10.0  # 話し終えた後、ダンスの終わりを待つ上限（実際の秒）


class RoutineIO(Protocol):
    """進み方が使う入出力の口。実体は借りた資源で動かす（3.1 の LeaseIO）。"""

    def show(self, line1: str, line2: str) -> None: ...
    def backlight(self, on: bool) -> None: ...
    def led(self, on: bool) -> None: ...
    def say(self, text: str) -> None: ...  # 話し終わるまで戻らない
    def clear_presses(self) -> None: ...
    def wait_press(self, timeout: float) -> bool: ...  # 最大 timeout 秒
    def look_around(self) -> None: ...
    def nod(self, times: int) -> None: ...
    def sleepy(self) -> None: ...
    def dance_and_say(self, text: str) -> None: ...  # ダンスしながら話す（4.9、4.10）


class LeaseIO:
    """`RoutineIO` を、借りた `Lease` の口で動かす（4.1、4.9、4.10）。

    顔と星の文字は TaskRunner が作る。ここでは 2 行とも LCD の中央に置いて渡す。
    """

    def __init__(self, lease: Lease) -> None:
        self._lease = lease

    def show(self, line1: str, line2: str) -> None:
        self._lease.display.show(center(line1), center(line2))

    def backlight(self, on: bool) -> None:
        self._lease.display.set_backlight(on)

    def led(self, on: bool) -> None:
        self._lease.led.set(on)

    def say(self, text: str) -> None:
        self._lease.voice.say(text)

    def clear_presses(self) -> None:
        self._lease.button.clear()

    def wait_press(self, timeout: float) -> bool:
        return self._lease.button.wait_press(timeout)

    def look_around(self) -> None:
        self._lease.neck.look_around()

    def nod(self, times: int) -> None:
        self._lease.neck.nod(times)

    def sleepy(self) -> None:
        self._lease.neck.sleepy()

    def dance_and_say(self, text: str) -> None:
        """ダンスを短命のスレッドで動かし、このスレッドで話す。話し終えたらダンスを待つ。

        タイヤを回すかは基盤の許可で決まる（4.10）。走行を借りていれば、その口を渡す。
        ダンスの失敗は型の名前だけを記録する。話す側の例外はそのまま外に出す。
        """
        lease = self._lease
        drive = lease.drive if Resource.DRIVE in lease.resources else None

        def dance() -> None:
            try:
                lease.neck.dance(drive)
            except LeaseRevoked as exc:
                logger.info("ダンスを止めました: %s", type(exc).__name__)
            except Exception as exc:
                logger.warning("ダンスできませんでした: %s", type(exc).__name__)

        thread = threading.Thread(target=dance, name="routine-dance", daemon=True)
        thread.start()
        try:
            lease.voice.say(text)
        finally:
            thread.join(DANCE_JOIN_S)
            if thread.is_alive():
                logger.warning("ダンスが時間内に終わりませんでした")


def _remind_s(task: TaskConfig) -> float:
    return task.remind_min * 60.0


@dataclass(frozen=True)
class Timings:
    """催促の間隔とあとでの長さ。試しでは短い時間に差し替える。"""

    remind_s: Callable[[TaskConfig], float] = field(default=_remind_s)
    later_s: float = LATER_S


@dataclass(frozen=True)
class Texts:
    """話す言葉に呼び名を入れる口（1.5、1.6）。呼び名は設定の `call_name`。"""

    call_name: str = ""

    def fill(self, text: str) -> str:
        return fill_name(text, self.call_name)


class TaskStopped(Exception):
    """機能を止めるので、タスクを途中でやめた。結果は記録しない（起動し直したら始め直す。7.7）。"""


class TaskHooks(Protocol):
    """進み方が、待ちの区切りごとに聞く先。"""

    def should_stop(self) -> bool: ...  # 真なら `TaskStopped` で抜ける
    def cut_in(self, phase: Phase) -> DueTask | None: ...
    def due_notice(self) -> DueNotice | None: ...
    def notice_said(self, notice: DueNotice) -> None: ...
    def star_count_after_done(self) -> int: ...  # 「ﾔｯﾀﾈ! ★N」に出す数


@dataclass(frozen=True)
class RunResult:
    """タスクの結果。中断のときは割り込んだタスクも返す。"""

    result: TaskResult
    cut_by: DueTask | None


class TaskRunner:
    """1 つのタスクを、準備から結果まで進める。日課の 1 本のスレッドだけで使う。"""

    def __init__(self, io: RoutineIO, clock: Clock, texts: Texts, timings: Timings) -> None:
        self._io = io
        self._clock = clock
        self._texts = texts
        self._timings = timings
        self._showing: tuple[str, str] | None = None  # 今のタスクの表示（予告の後に戻す）

    def run(self, task: TaskConfig, hooks: TaskHooks, *, on_done: Callable[[], None]) -> RunResult:
        """タスクを進めて結果を返す。

        `on_done` は「できた」と決まった直後、ほめる表示の前に呼ぶ（呼ぶ側が記録して星を増やす）。
        `LeaseRevoked` はそのまま外に出す（呼ぶ側が中断として扱う）。
        待ちの区切りで `hooks.should_stop()` が真なら `TaskStopped` を投げる（結果は返さない）。
        """
        self._showing = None
        result = self._run(task, hooks, on_done)
        self._io.clear_presses()  # 押下を次に持ち越さない（4.12）
        logger.info("タスクを終えました: %s %s", task.id, result.result.value)
        return result

    def _run(self, task: TaskConfig, hooks: TaskHooks, on_done: Callable[[], None]) -> RunResult:
        self._io.backlight(True)
        if task.ready is not None:
            # 準備（4.2）: 声かけの前に話し、押されるまで待つ（時間の上限なし）
            self._show(task.lcd, Face.NORMAL)
            self._io.clear_presses()
            self._say(task.ready)
            waited = self._wait(Phase.READY, None, hooks)
            if isinstance(waited, DueTask):
                return self._interrupted(waited)
        # 声かけ（4.1）と催促（4.3）。声かけは最初と催促を合わせて上限 + 1 回まで（4.13）
        remind_s = self._timings.remind_s(task)
        for _ in range(task.max_reminders + 1):
            self._call(task)
            waited = self._wait(Phase.CALLING, remind_s, hooks)
            if isinstance(waited, DueTask):
                return self._interrupted(waited)
            if waited:
                return self._done(task, hooks, on_done)
        # あとで（4.4〜4.6）
        self._io.led(False)
        self._show(LCD_LATER, Face.NORMAL)
        self._say(LATER_SPEECH, filled=False)
        waited = self._wait(Phase.LATER, self._timings.later_s, hooks)
        if isinstance(waited, DueTask):
            return self._interrupted(waited)
        if waited:
            return self._done(task, hooks, on_done)
        self._io.led(False)
        return RunResult(TaskResult.SKIPPED, None)

    def _call(self, task: TaskConfig) -> None:
        self._show(task.lcd, Face.CALL)
        self._io.look_around()
        self._io.led(True)
        self._io.clear_presses()  # 話す前の押下は捨てる。話している間の押下は数える（4.8）
        self._say(task.call)

    def _done(self, task: TaskConfig, hooks: TaskHooks, on_done: Callable[[], None]) -> RunResult:
        # ほめる間（Phase.PRAISING）は割り込みも予告も聞かない（5.3）
        self._io.led(False)
        on_done()
        self._show(lcd_done(hooks.star_count_after_done()), Face.HAPPY)
        try:
            self._io.dance_and_say(self._texts.fill(task.praise))
        except SpeechFailed as exc:
            logger.warning("ほめる言葉を話せませんでした: %s", type(exc).__name__)
        if not task.sleep:
            self._io.nod(2)
        return RunResult(TaskResult.DONE, None)

    def _interrupted(self, cut_by: DueTask) -> RunResult:
        self._io.led(False)
        return RunResult(TaskResult.INTERRUPTED, cut_by)

    def _wait(self, phase: Phase, seconds: float | None, hooks: TaskHooks) -> bool | DueTask:
        """押されたら真、時間がたったら偽、割り込まれたら割り込んだタスクを返す。

        `seconds` が None なら時間の上限なし。最大 `POLL_S` 秒ずつ区切り、区切りごとに
        止める知らせと割り込みと予告を聞く。予告を話している間も、残りの時間は減る（3.7）。
        止める知らせがあれば `TaskStopped` を投げる。
        """
        start = self._clock.monotonic()
        while True:
            if seconds is None:
                slice_s = POLL_S
            else:
                remaining = seconds - (self._clock.monotonic() - start)
                if remaining <= 0:
                    return False
                slice_s = min(POLL_S, remaining)
            if self._io.wait_press(slice_s):
                return True
            if hooks.should_stop():
                raise TaskStopped()
            cut_by = hooks.cut_in(phase)
            if cut_by is not None:
                return cut_by
            notice = hooks.due_notice()
            if notice is not None:
                self._say_notice(notice, hooks)

    def _say_notice(self, notice: DueNotice, hooks: TaskHooks) -> None:
        showing = self._showing
        self._show(notice.task.lcd, Face.NORMAL)
        self._io.nod(1)
        if notice.task.notice is not None:
            self._say(notice.task.notice)
        hooks.notice_said(notice)
        if showing is not None:
            self._io.show(*showing)
            self._showing = showing

    def _show(self, line1: str, face: Face) -> None:
        self._showing = (line1, face.text())
        self._io.show(*self._showing)

    def _say(self, text: str, *, filled: bool = True) -> None:
        """話す。声が出せなくても、記録して先に進む（画面とボタンで続けられる）。"""
        try:
            self._io.say(self._texts.fill(text) if filled else text)
        except SpeechFailed as exc:
            logger.warning("声を出せませんでした: %s", type(exc).__name__)
