"""だるまさんの 1 回の遊びの流れ（GameFlow）と、流れが使う口（GameIO）。

要件 1.2〜1.5、2.1〜2.9、3.1、3.3、4.1〜4.7、5.3、6.3、7.5、9.1。

始めの確かめ → 始めの言葉 → 回の繰り返し → 終わりの言葉、を GameIO の口だけで進める。
資源は借りない（借りるのは run_game）。回の判定と数え方は rules の純粋な関数に任せる。
呼び名は話す直前に差し込む。ログには終わり方の種類・回の数・例外の型の名前だけを出す。
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final, Protocol

from osekkai.core.types import DeviceUnavailable, LeaseRevoked, SpeechFailed
from osekkai.features.daruma import texts
from osekkai.features.daruma.rules import (
    EndReason,
    RoundKind,
    Rules,
    Tally,
    advance,
    judge_watch,
    pick_chant,
    pick_pause,
    rounds_left,
    schedule_blocks,
)
from osekkai.features.routine.api import RoutineStatus

# 終わり方と、話して終える言葉。話さない終わり方（BUSY・PREEMPTED・QUIET・STOPPED・
# ERROR）は入れない。勝ち（WIN）は回の中で踊りながら話す（3.3）。始める前の次の予定は
# SAY_NEXT_TASK_BEFORE を始めの確かめが直接話す（1.5）
END_SAYS: Final[Mapping[EndReason, str]] = MappingProxyType(
    {
        EndReason.MAX_ROUNDS: texts.SAY_MAX_ROUNDS,  # 4.1（ローバーの勝ち）
        EndReason.NEXT_TASK: texts.SAY_NEXT_TASK,  # 4.3
        EndReason.NO_CAMERA: texts.SAY_NO_CAMERA,  # 1.4
        EndReason.CAMERA_LOST: texts.SAY_CAMERA_LOST,  # 4.6
    }
)


class CameraLost(Exception):
    """遊びの途中でカメラが使えなくなった（4.6）。"""


class GameIO(Protocol):
    """流れが使う口。実体は借りた資源とカメラで動かす（LeaseGameIO）。"""

    def show(self, line1: str, line2: str) -> None: ...

    def say(self, text: str) -> None:
        """話し終わるまで戻らない。LeaseRevoked・SpeechFailed を投げうる。"""
        ...

    def look(self, pan: float, tilt: float) -> None: ...

    def home(self) -> None:
        """首を正面に戻す。"""
        ...

    def nod(self) -> None:
        """1 回うなずく（2.7）。"""
        ...

    def celebrate(self, text: str) -> None:
        """首だけで踊りながら話す（3.3）。"""
        ...

    def clear_presses(self) -> None: ...

    def pressed(self) -> bool:
        """待ち行列に押下があるか（消費しない）。"""
        ...

    def wait_press(self, seconds: float) -> bool:
        """押下を待つ。押されたら直ちに True。"""
        ...

    def open_camera(self) -> bool:
        """カメラを借りる。借りられなければ False（1.4）。"""
        ...

    def motion(self, seconds: float) -> float:
        """`seconds` 秒見た動きの量（0〜1）。カメラの誤りは CameraLost。"""
        ...

    def rest(self, seconds: float) -> None:
        """待つ。中断・止める旗で早く戻る。"""
        ...


@dataclass(frozen=True)
class GameResult:
    """1 回の遊びの終わり方と、終えた回の数。"""

    reason: EndReason
    rounds: int


class GameFlow:
    """1 回の遊びの流れ。機能のスレッド（か道具）から 1 つずつ呼ぶ。"""

    def __init__(
        self,
        rules: Rules,
        call_name: str,
        rng: random.Random,
        status: Callable[[], RoutineStatus | None],
        now: Callable[[], datetime],
        interrupted: Callable[[], EndReason | None],
        log: logging.Logger,
    ) -> None:
        """`status` は日課の状況、`now` は日課と同じ時計の時差つきの時刻を返す口。"""
        self._rules = rules
        self._call_name = call_name
        self._rng = rng
        self._status = status
        self._now = now
        self._interrupted = interrupted
        self._log = log
        self._tally = Tally()

    def play(self, io: GameIO) -> GameResult:
        """始めの確かめから、遊びが終わるまで進める。終わり方と終えた回の数を返す。"""
        self._tally = Tally()
        guarded = _GuardedIO(io, self._check)
        try:
            reason = self._play(guarded)
        except _Interrupted as stop:
            # 中断・おやすみ・止める旗。話している一言は言い終えている（4.4、4.5）
            reason = stop.reason
        except LeaseRevoked:
            reason = EndReason.PREEMPTED  # 4.4（話さない）
        except (SpeechFailed, DeviceUnavailable) as exc:
            # 例外の文は出さない（7.5）
            self._log.warning("遊びの口の誤りで終えます: %s", type(exc).__name__)
            reason = EndReason.ERROR  # 5.3
        rounds = self._tally.rounds
        self._log.debug("遊びの流れを終えました: %s（%d 回）", reason.value, rounds)
        return GameResult(reason, rounds)

    def _play(self, io: GameIO) -> EndReason:
        """終わりの言葉まで話して、首を正面に戻す（4.7）。"""
        reason = self._run(io)
        io.home()
        return reason

    def _run(self, io: GameIO) -> EndReason:
        """始めから回の繰り返しまで進め、終わりの言葉を話して終わり方を返す。"""
        try:
            reason = self._start(io)
            if reason is not None:
                return reason
            while True:
                if self._next_task_near():  # 4.3
                    reason = EndReason.NEXT_TASK
                    break
                kind = self._round(io)
                if kind is RoundKind.WIN:
                    return EndReason.WIN
                self._tally, reason = advance(self._tally, kind, self._rules)
                if reason is not None:  # 4.1
                    break
                # 動いていなければ、待たずに次の回へ（2.9）
        except CameraLost:
            reason = EndReason.CAMERA_LOST  # 4.6
        self._say(io, END_SAYS[reason])
        return reason

    def _start(self, io: GameIO) -> EndReason | None:
        """始めの確かめと始めの言葉。始めないなら終わり方を返す。"""
        # 先に次の予定を見る。近ければカメラを開かずに断る（1.5）
        if self._next_task_near():
            self._say(io, texts.SAY_NEXT_TASK_BEFORE)
            return EndReason.NEXT_TASK
        if not io.open_camera():
            self._say(io, texts.SAY_NO_CAMERA)  # 1.4
            return EndReason.NO_CAMERA
        io.show(*texts.LCD_INTRO)  # 1.2
        self._say(io, texts.SAY_INTRO)
        io.rest(texts.INTRO_WAIT_S)  # 1.3
        self._say(io, texts.say_go(self._rules.max_rounds))
        # 「いくよー！」までの押下は数えない（1.3）
        io.clear_presses()
        return None

    def _round(self, io: GameIO) -> RoundKind:
        """1 回を進めて、回の結果を返す。"""
        # 最後の 3 回は、唱える前に残りの回数を話す（4.2）
        left = texts.SAY_ROUNDS_LEFT.get(rounds_left(self._tally, self._rules))
        if left is not None:
            self._say(io, left)
        chant = pick_chant(self._rng)  # 2.1
        io.show(chant.lcd, texts.FACE_CHANT.text())
        io.look(*texts.POSE_AWAY)  # 2.2
        self._say(io, chant.speech)
        # 唱えている間と、その後の間の押下は勝ち（2.3、3.1）
        if io.pressed() or io.wait_press(pick_pause(self._rng)):
            io.show(texts.LCD_WIN, texts.FACE_WIN.text())  # 3.3
            io.celebrate(texts.say_with_name(texts.SAY_WIN, self._call_name))
            return RoundKind.WIN
        io.look(*texts.POSE_FACING)  # 2.4
        io.show(texts.LCD_WATCH, texts.FACE_WATCH.text())
        self._say(io, texts.SAY_KORONDA)
        amount = io.motion(self._rules.watch_seconds)  # 2.5
        # 間が終わった後の押下は、見ている間の押下として動いたにする（2.6）
        kind = judge_watch(amount, io.pressed(), self._rules.threshold)
        if kind is RoundKind.MOVED:
            io.show(texts.LCD_MOVED, texts.FACE_MOVED.text())  # 2.7
            io.nod()
            self._say(io, texts.SAY_MOVED)
            io.rest(texts.MOVED_WAIT_S)
            # スタートに戻る途中の押下は数えない（2.8）
            io.clear_presses()
        return kind

    def _next_task_near(self) -> bool:
        return schedule_blocks(self._status(), self._now(), self._rules.end_before_task)

    def _say(self, io: GameIO, template: str) -> None:
        """呼び名を差し込んで話す。"""
        io.say(texts.say_with_name(template, self._call_name))

    def _check(self) -> None:
        """中断・おやすみ・止める旗があれば、次の手順をせずに終える（4.4、4.5）。"""
        reason = self._interrupted()
        if reason is not None:
            raise _Interrupted(reason)


class _Interrupted(Exception):
    """手順の前に中断に気づいた。話さずに終える。"""

    def __init__(self, reason: EndReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class _GuardedIO:
    """各手順の前に中断を確かめてから、本物の口を呼ぶ（4.4、4.5）。"""

    def __init__(self, io: GameIO, check: Callable[[], None]) -> None:
        self._io = io
        self._check = check

    def show(self, line1: str, line2: str) -> None:
        self._check()
        self._io.show(line1, line2)

    def say(self, text: str) -> None:
        self._check()
        self._io.say(text)

    def look(self, pan: float, tilt: float) -> None:
        self._check()
        self._io.look(pan, tilt)

    def home(self) -> None:
        self._check()
        self._io.home()

    def nod(self) -> None:
        self._check()
        self._io.nod()

    def celebrate(self, text: str) -> None:
        self._check()
        self._io.celebrate(text)

    def clear_presses(self) -> None:
        self._check()
        self._io.clear_presses()

    def pressed(self) -> bool:
        self._check()
        return self._io.pressed()

    def wait_press(self, seconds: float) -> bool:
        self._check()
        return self._io.wait_press(seconds)

    def open_camera(self) -> bool:
        self._check()
        return self._io.open_camera()

    def motion(self, seconds: float) -> float:
        self._check()
        return self._io.motion(seconds)

    def rest(self, seconds: float) -> None:
        self._check()
        self._io.rest(seconds)
