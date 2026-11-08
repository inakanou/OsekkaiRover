"""だるまさんの 1 回の遊びの流れ（GameFlow）と、流れが使う口（GameIO）。

要件 1.2〜1.5、2.1〜2.9、3.1、3.3、9.1。

始めの確かめ → 始めの言葉 → 回の繰り返し、を GameIO の口だけで進める。資源は借りない
（借りるのは run_game）。回の判定と数え方は rules の純粋な関数に任せる。
呼び名は話す直前に差し込む。
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

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
    schedule_blocks,
)
from osekkai.features.routine.api import RoutineStatus


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

    def play(self, io: GameIO) -> GameResult:
        """始めの確かめから、遊びが終わるまで進める。"""
        reason = self._start(io)
        if reason is not None:
            return GameResult(reason, 0)
        tally = Tally()
        while True:
            kind = self._round(io)
            if kind is RoundKind.WIN:
                return GameResult(EndReason.WIN, tally.rounds)
            tally, reason = advance(tally, kind, self._rules)
            if reason is not None:
                return GameResult(reason, tally.rounds)
            # 動いていなければ、待たずに次の回へ（2.9）

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
        self._say(io, texts.SAY_GO)
        # 「いくよー！」までの押下は数えない（1.3）
        io.clear_presses()
        return None

    def _round(self, io: GameIO) -> RoundKind:
        """1 回を進めて、回の結果を返す。"""
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
