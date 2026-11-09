"""走行の判定（要件 4.2〜4.6、5.1〜5.4、5.6、5.7、6.1〜6.3）。

時刻は monotonic の秒をそのまま数で渡す（偽の時計と同じく、テストが時刻を決める）。
"""

from __future__ import annotations

import math

import pytest

from osekkai.features.remote.drive_rules import (
    COMMAND_TIMEOUT_S,
    STEERING,
    Command,
    Danger,
    DangerVoice,
    Decision,
    DriveState,
    LcdState,
    Move,
    decide,
    judge_front,
)
from osekkai.features.remote.ports import SensorReading

SLOW = 20
FAST = 35
STOP_CM = 20.0
REF = 500
BACK_MAX = 2.0

CLEAR = SensorReading(distance_cm=80.0, floor=(900, 900, 900))
OBSTACLE = SensorReading(distance_cm=10.0, floor=(900, 900, 900))
EDGE = SensorReading(distance_cm=80.0, floor=(900, 100, 900))
FORWARD_MOVES = (Move.FWD, Move.LEFT, Move.RIGHT)
BACK_MOVES = (Move.BACK, Move.BACK_LEFT, Move.BACK_RIGHT)


def _decide(
    move: Move | None,
    now: float,
    state: DriveState | None = None,
    *,
    at: float | None = None,
    reading: SensorReading | None = CLEAR,
    fast: bool = False,
    cliff_stop: bool = True,
) -> tuple[Decision, DriveState]:
    command = None if move is None else Command(move, now if at is None else at)
    return decide(
        command,
        now,
        reading,
        DriveState() if state is None else state,
        fast=fast,
        speeds=(SLOW, FAST),
        back_max_s=BACK_MAX,
        stop_cm=STOP_CM,
        cliff_stop=cliff_stop,
        cliff_reference=REF,
    )


def _judge(reading: SensorReading, *, cliff_stop: bool = True) -> Danger:
    return judge_front(reading, stop_cm=STOP_CM, cliff_stop=cliff_stop, cliff_reference=REF)


def _is_stop(decision: Decision) -> bool:
    return decision.speed == 0 and decision.steering == 0.0 and decision.lcd is LcdState.STOPPED


# --- 決まった値 ---


def test_ステアリングは左右に30度() -> None:
    assert STEERING[Move.LEFT] == -30.0
    assert STEERING[Move.BACK_LEFT] == -30.0
    assert STEERING[Move.RIGHT] == 30.0
    assert STEERING[Move.BACK_RIGHT] == 30.0
    assert STEERING[Move.FWD] == 0.0
    assert STEERING[Move.BACK] == 0.0
    assert STEERING[Move.STOP] == 0.0
    assert set(STEERING) == set(Move)


def test_命令の途切れは0_5秒() -> None:
    assert COMMAND_TIMEOUT_S == 0.5


# --- 前の判定 ---


@pytest.mark.parametrize(
    ("reading", "expected"),
    [
        # 距離の境: stop_cm ちょうどは物にしない
        (SensorReading(20.0, (900, 900, 900)), Danger.NONE),
        (SensorReading(19.99, (900, 900, 900)), Danger.OBSTACLE),
        (SensorReading(0.0, (900, 900, 900)), Danger.OBSTACLE),
        # 床の境: 基準ちょうどは段差
        (SensorReading(80.0, (501, 501, 501)), Danger.NONE),
        (SensorReading(80.0, (500, 900, 900)), Danger.EDGE),
        (SensorReading(80.0, (900, 900, 500)), Danger.EDGE),
        (SensorReading(80.0, (0, 0, 0)), Danger.EDGE),
        # 物と段差が同時なら段差
        (SensorReading(5.0, (900, 100, 900)), Danger.EDGE),
        # 読めない値
        (SensorReading(None, (900, 900, 900)), Danger.UNKNOWN),
        (SensorReading(80.0, (None, 900, 900)), Danger.UNKNOWN),
        (SensorReading(80.0, (900, 900, None)), Danger.UNKNOWN),
        (SensorReading(None, (100, 100, 100)), Danger.UNKNOWN),
        # ありえない値
        (SensorReading(math.nan, (900, 900, 900)), Danger.UNKNOWN),
        (SensorReading(-1.0, (900, 900, 900)), Danger.UNKNOWN),
        (SensorReading(80.0, (900, -1, 900)), Danger.UNKNOWN),
        # far は物がない。距離は無限として扱う
        (SensorReading(None, (900, 900, 900), far=True), Danger.NONE),
        (SensorReading(3.0, (900, 900, 900), far=True), Danger.NONE),
        (SensorReading(None, (900, 100, 900), far=True), Danger.EDGE),
        (SensorReading(None, (None, 900, 900), far=True), Danger.UNKNOWN),
    ],
)
def test_前の判定の境(reading: SensorReading, expected: Danger) -> None:
    assert _judge(reading) is expected


def test_段差の判定を無効にすると距離だけで判定する() -> None:
    assert _judge(EDGE, cliff_stop=False) is Danger.NONE  # 5.7
    assert _judge(SensorReading(80.0, (None, None, None)), cliff_stop=False) is Danger.NONE
    assert _judge(SensorReading(5.0, (100, 100, 100)), cliff_stop=False) is Danger.OBSTACLE
    assert _judge(SensorReading(None, (900, 900, 900)), cliff_stop=False) is Danger.UNKNOWN


# --- 押している間だけ走る ---


@pytest.mark.parametrize("move", FORWARD_MOVES)
def test_前の命令はその向きにゆっくり走る(move: Move) -> None:
    decision, state = _decide(move, 10.0)
    assert decision.speed == SLOW  # 4.5
    assert decision.steering == STEERING[move]  # 4.2
    assert decision.lcd is LcdState.FORWARD
    assert decision.stopped_by is None
    assert state.danger is Danger.NONE


def test_はやいを選ぶと次の命令からはやく走る() -> None:
    decision, state = _decide(Move.FWD, 10.0)
    assert decision.speed == SLOW
    decision, _ = _decide(Move.RIGHT, 10.1, state, fast=True)
    assert decision.speed == FAST  # 4.6
    assert decision.steering == 30.0


def test_命令がないと止まる() -> None:
    decision, _ = _decide(None, 10.0)
    assert _is_stop(decision)  # 4.3
    assert decision.stopped_by is None


def test_とめるの命令で止まりまっすぐに戻す() -> None:
    _, state = _decide(Move.LEFT, 10.0)
    decision, _ = _decide(Move.STOP, 10.1, state)
    assert _is_stop(decision)  # 4.3


def test_0_5秒の途切れで止まる() -> None:
    decision, _ = _decide(Move.FWD, 10.5, at=10.0)
    assert decision.speed == SLOW  # ちょうど 0.5 秒はまだ走る
    decision, _ = _decide(Move.FWD, 10.51, at=10.0)
    assert _is_stop(decision)  # 4.4
    assert decision.stopped_by is None


def test_後退も途切れで止まる() -> None:
    decision, _ = _decide(Move.BACK, 10.6, at=10.0)
    assert _is_stop(decision)


# --- 前の安全 ---


@pytest.mark.parametrize(
    ("reading", "danger"),
    [(OBSTACLE, Danger.OBSTACLE), (EDGE, Danger.EDGE)],
)
@pytest.mark.parametrize("move", FORWARD_MOVES)
def test_1回の危険で止まる(move: Move, reading: SensorReading, danger: Danger) -> None:
    decision, state = _decide(move, 10.0, reading=reading)
    assert _is_stop(decision)  # 5.2
    assert decision.stopped_by is danger
    assert state.danger is danger


def test_読めない値で止まる() -> None:
    decision, state = _decide(Move.FWD, 10.0, reading=SensorReading(None, (900, 900, 900)))
    assert _is_stop(decision)  # 5.4
    assert decision.stopped_by is Danger.UNKNOWN
    assert state.danger is Danger.UNKNOWN


def test_読みがない前の命令は止まる() -> None:
    decision, _ = _decide(Move.FWD, 10.0, reading=None)
    assert _is_stop(decision)  # 5.4
    assert decision.stopped_by is Danger.UNKNOWN


def test_far_は進む() -> None:
    decision, _ = _decide(Move.FWD, 10.0, reading=SensorReading(None, (900, 900, 900), far=True))
    assert decision.speed == SLOW
    assert decision.stopped_by is None


def test_段差の判定を無効にすると段差でも進む() -> None:
    decision, _ = _decide(Move.FWD, 10.0, reading=EDGE, cliff_stop=False)
    assert decision.speed == SLOW  # 5.7
    decision, _ = _decide(Move.FWD, 10.0, reading=OBSTACLE, cliff_stop=False)
    assert decision.stopped_by is Danger.OBSTACLE


def test_危険が続く間は前に進まず後退は受け付ける() -> None:
    _, state = _decide(Move.FWD, 10.0, reading=OBSTACLE)
    decision, state = _decide(Move.FWD, 10.1, state, reading=OBSTACLE)
    assert decision.stopped_by is Danger.OBSTACLE  # 5.3
    # 後退は前の判定を待たない（読みがなくても下がる）
    decision, state = _decide(Move.BACK, 10.2, state, reading=None)
    assert decision.speed == -SLOW
    assert decision.stopped_by is None
    # 危険が消えたら、また前に進める
    decision, state = _decide(Move.FWD, 10.3, state, reading=CLEAR)
    assert decision.speed == SLOW
    assert state.danger is Danger.NONE


# --- 後退の制限 ---


@pytest.mark.parametrize("move", BACK_MOVES)
@pytest.mark.parametrize("fast", [False, True])
def test_後退は速さを選んでもゆっくり(move: Move, fast: bool) -> None:
    decision, state = _decide(move, 10.0, fast=fast)
    assert decision.speed == -SLOW  # 6.1
    assert decision.steering == STEERING[move]
    assert decision.lcd is LcdState.BACK
    assert state.back_since == 10.0


def test_後退は2秒で止まり離すまで後退しない() -> None:
    _, state = _decide(Move.BACK, 10.0)
    decision, state = _decide(Move.BACK, 11.9, state)
    assert decision.speed == -SLOW
    decision, state = _decide(Move.BACK, 12.0, state)
    assert _is_stop(decision)  # 6.2
    assert decision.stopped_by is None
    assert state.back_latched is True
    decision, state = _decide(Move.BACK, 12.4, state)
    assert _is_stop(decision)
    assert state.back_latched is True


def test_止まった後退は左右の後ろに変えても下がらない() -> None:
    _, state = _decide(Move.BACK, 10.0)
    _, state = _decide(Move.BACK_LEFT, 12.0, state)
    assert state.back_latched is True
    decision, state = _decide(Move.BACK_RIGHT, 12.1, state)
    assert _is_stop(decision)  # 6.2（後ろのボタンはどれも同じ扱い）


def test_後ろの向きを変えても後退の時間は続けて数える() -> None:
    _, state = _decide(Move.BACK, 10.0)
    _, state = _decide(Move.BACK_LEFT, 11.0, state)
    assert state.back_since == 10.0
    decision, _ = _decide(Move.BACK_RIGHT, 12.0, state)
    assert _is_stop(decision)


@pytest.mark.parametrize("release", [None, Move.STOP])
def test_押し直すとまた下がる(release: Move | None) -> None:
    _, state = _decide(Move.BACK, 10.0)
    _, state = _decide(Move.BACK, 12.0, state)
    assert state.back_latched is True
    decision, state = _decide(release, 12.1, state)
    assert _is_stop(decision)
    assert state.back_latched is False  # 6.3
    assert state.back_since is None
    decision, state = _decide(Move.BACK, 12.2, state)
    assert decision.speed == -SLOW  # 6.3
    assert state.back_since == 12.2


def test_途切れでも止まった後退が解ける() -> None:
    _, state = _decide(Move.BACK, 10.0)
    _, state = _decide(Move.BACK, 12.0, state)
    _, state = _decide(Move.BACK, 13.0, state, at=12.0)  # 途切れ
    assert state.back_latched is False
    assert state.back_since is None


def test_前から後ろに変えると後退の時間を数え直す() -> None:
    _, state = _decide(Move.BACK, 10.0)
    _, state = _decide(Move.FWD, 11.5, state)
    assert state.back_since is None
    decision, state = _decide(Move.BACK, 11.6, state)
    assert decision.speed == -SLOW
    assert state.back_since == 11.6
    decision, state = _decide(Move.BACK, 13.5, state)
    assert decision.speed == -SLOW  # 新しく数えて 1.9 秒
    decision, _ = _decide(Move.BACK, 13.6, state)
    assert _is_stop(decision)


def test_危険で止まった前の命令でも後退の時間を空にする() -> None:
    _, state = _decide(Move.BACK, 10.0)
    _, state = _decide(Move.FWD, 11.5, state, reading=OBSTACLE)
    assert state.back_since is None
    decision, state = _decide(Move.BACK, 11.6, state)
    assert decision.speed == -SLOW
    assert state.back_since == 11.6


def test_止まった後退は前の命令で解ける() -> None:
    _, state = _decide(Move.BACK, 10.0)
    _, state = _decide(Move.BACK, 12.0, state)
    _, state = _decide(Move.FWD, 12.1, state)
    assert state.back_latched is False
    decision, _ = _decide(Move.BACK, 12.2, state)
    assert decision.speed == -SLOW


def test_止まっている間は後退の時間を持たない() -> None:
    _, state = _decide(Move.BACK, 10.0)
    _, state = _decide(None, 10.1, state)
    assert state == DriveState()


# --- 危険の声の間隔 ---


def test_危険の声は10秒に1回() -> None:
    voice = DangerVoice()
    assert DangerVoice.INTERVAL_S == 10.0
    assert voice.should_speak(100.0) is True  # 5.6
    assert voice.should_speak(100.1) is False
    assert voice.should_speak(109.99) is False
    assert voice.should_speak(110.0) is True
    assert voice.should_speak(115.0) is False
    assert voice.should_speak(120.0) is True


def test_危険の声は時刻0でも初めは話す() -> None:
    assert DangerVoice().should_speak(0.0) is True


# --- 止まる側に倒す（ありえない時刻と値） ---


@pytest.mark.parametrize("at", [10.1, math.nan, math.inf])
def test_命令の時刻が今より先かありえない値なら止まる(at: float) -> None:
    decision, _ = _decide(Move.FWD, 10.0, at=at)
    assert _is_stop(decision)


def test_後退を始めた時刻が今より先なら止めて後退しない() -> None:
    decision, after = _decide(Move.BACK, 10.0, DriveState(back_since=11.0))
    assert _is_stop(decision)
    assert after.back_latched


def test_物がない印がないのに距離が無限なら読めないとする() -> None:
    reading = SensorReading(distance_cm=math.inf, floor=(900, 900, 900))
    assert _judge(reading) is Danger.UNKNOWN
    assert _judge(reading, cliff_stop=False) is Danger.UNKNOWN
