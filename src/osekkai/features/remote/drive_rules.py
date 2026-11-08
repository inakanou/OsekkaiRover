"""走行の判定（要件 4.2〜4.6、5.1〜5.4、5.6、5.7、6.1〜6.3）。

命令・途切れ・速さ・前の安全・後退の制限・危険の声の間隔を決める。
ハードに触らない。時刻は呼ぶ側が monotonic の秒で渡す。

- 前に進む命令（FWD・LEFT・RIGHT）は、毎回 1 回の読みで前を判定する。間引かない
- 後ろのボタン（BACK・BACK_LEFT・BACK_RIGHT）は 1 つの仲間として扱う。
  向きを変えても後退の時間は続けて数え、2 秒で止めた印も残る。
  印は、離す（命令がない・STOP・途切れ）か、前に進む命令で解ける
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import Final

from .ports import SensorReading


class Move(Enum):
    """端末から届く走る命令の向き。"""

    FWD = "fwd"
    LEFT = "left"
    RIGHT = "right"
    BACK = "back"
    BACK_LEFT = "back_left"
    BACK_RIGHT = "back_right"
    STOP = "stop"


FORWARD_MOVES: Final = frozenset({Move.FWD, Move.LEFT, Move.RIGHT})
BACK_MOVES: Final = frozenset({Move.BACK, Move.BACK_LEFT, Move.BACK_RIGHT})

# 向きごとのステアリングの角度（4.2）。負は左
STEERING: Final[Mapping[Move, float]] = MappingProxyType(
    {
        Move.FWD: 0.0,
        Move.LEFT: -30.0,
        Move.RIGHT: 30.0,
        Move.BACK: 0.0,
        Move.BACK_LEFT: -30.0,
        Move.BACK_RIGHT: 30.0,
        Move.STOP: 0.0,
    }
)
COMMAND_TIMEOUT_S: Final = 0.5  # 4.4


@dataclass(frozen=True)
class Command:
    """最後に届いた走る命令。"""

    move: Move
    at: float  # monotonic の秒


class Danger(Enum):
    """前の判定。"""

    NONE = "none"
    OBSTACLE = "obstacle"  # 物
    EDGE = "edge"  # 段差
    UNKNOWN = "unknown"  # 読めない・ありえない値


class LcdState(Enum):
    """走行の LCD の出し分け（4.7）。危険で止めたときの出し方は stopped_by で決める。"""

    STOPPED = "stopped"
    FORWARD = "forward"
    BACK = "back"


@dataclass(frozen=True)
class DriveState:
    """判定の間で持ち越す状態。"""

    back_since: float | None = None  # 後退を始めた時刻
    back_latched: bool = False  # 2 秒で止めて、離されるまで後退しない
    danger: Danger = Danger.NONE  # 前に進む命令での最後の判定


@dataclass(frozen=True)
class Decision:
    """この時の走り方。"""

    steering: float
    speed: int  # 0 は止まる。負は後退
    stopped_by: Danger | None  # 危険で止めたとき
    lcd: LcdState


_STOP: Final = Decision(steering=0.0, speed=0, stopped_by=None, lcd=LcdState.STOPPED)


def _within(elapsed: float, limit: float) -> bool:
    """経った時間が 0 以上で上限以下か。NaN と負は偽。"""
    return 0.0 <= elapsed <= limit


def _bad_number(value: float | None) -> bool:
    return value is None or math.isnan(value) or value < 0


def judge_front(
    reading: SensorReading, *, stop_cm: float, cliff_stop: bool, cliff_reference: int
) -> Danger:
    """1 回の読みから前の危険を決める（5.2、5.4、5.7）。

    読めない値・ありえない値は UNKNOWN。物と段差が同時なら EDGE（落ちる方を先に知らせる）。
    段差の判定が無効なら、床の値は見ない（距離だけで判定する）。
    `far` は物がないので、距離を無限として扱う。
    """
    if reading.far:
        distance = math.inf
    elif reading.distance_cm is None or _bad_number(reading.distance_cm):
        return Danger.UNKNOWN
    elif math.isinf(reading.distance_cm):
        return Danger.UNKNOWN  # 物がない印のない無限はありえない値（5.4）
    else:
        distance = reading.distance_cm
    if cliff_stop:
        if any(_bad_number(value) for value in reading.floor):
            return Danger.UNKNOWN
        if any(value is not None and value <= cliff_reference for value in reading.floor):
            return Danger.EDGE
    if distance < stop_cm:
        return Danger.OBSTACLE
    return Danger.NONE


def decide(
    command: Command | None,
    now: float,
    reading: SensorReading | None,
    state: DriveState,
    *,
    fast: bool,
    speeds: tuple[int, int],
    back_max_s: float,
    stop_cm: float,
    cliff_stop: bool,
    cliff_reference: int,
) -> tuple[Decision, DriveState]:
    """命令と前の読みから、この時の走り方と次の状態を決める。

    `speeds` は（ゆっくり, はやい）。`reading` は前に進む命令のときだけ使い、
    None なら読めないとして止まる。
    """
    # 1. 命令がない・とめる・途切れ → 止まる。後退の印を解く（4.3、4.4、6.3）
    # 時刻が今より先・ありえない値のときも止まる側に倒す（比べられない NaN は範囲に入らない）
    if (
        command is None
        or command.move is Move.STOP
        or not _within(now - command.at, COMMAND_TIMEOUT_S)
    ):
        return _STOP, DriveState()

    move = command.move
    steering = STEERING[move]

    # 2. 前に進む命令: 毎回 1 回の読みで判定する（5.2〜5.4）。後退の時間と印を空にする
    if move in FORWARD_MOVES:
        danger = (
            Danger.UNKNOWN
            if reading is None
            else judge_front(
                reading, stop_cm=stop_cm, cliff_stop=cliff_stop, cliff_reference=cliff_reference
            )
        )
        next_state = DriveState(danger=danger)
        if danger is not Danger.NONE:
            return replace(_STOP, stopped_by=danger), next_state
        # 4. 選んだ速さで、決まった角度で走る（4.2、4.5、4.6）
        speed = speeds[1] if fast else speeds[0]
        return Decision(steering, speed, None, LcdState.FORWARD), next_state

    # 3. 後退の命令: 前の判定を待たない。いつもゆっくり、2 秒まで（6.1、6.2）
    if state.back_latched:
        return _STOP, replace(state, danger=Danger.NONE)
    back_since = now if state.back_since is None else state.back_since
    elapsed = now - back_since
    if not (0.0 <= elapsed < back_max_s):  # 先の時刻・ありえない値も止める
        return _STOP, DriveState(back_since=back_since, back_latched=True)
    return Decision(steering, -speeds[0], None, LcdState.BACK), DriveState(back_since=back_since)


class DangerVoice:
    """危険で止まったときの声の間隔（5.6）。画面の知らせはこれと関係なく毎回出す。"""

    INTERVAL_S: Final = 10.0

    def __init__(self) -> None:
        self._last: float | None = None

    def should_speak(self, now: float) -> bool:
        """いま話してよいか。話してよいときは、その時刻を覚える。"""
        if self._last is not None and now - self._last < self.INTERVAL_S:
            return False
        self._last = now
        return True
