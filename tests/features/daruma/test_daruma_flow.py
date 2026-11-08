"""だるまさんの 1 回の遊びの流れ（始めから勝ち・動いたまで）。

要件 1.2〜1.5、2.1〜2.9、3.1、3.3、9.1。偽の遊びの口・固定の乱数・偽の日課の状況で確かめる。
"""

import logging
import random
from datetime import timedelta

from daruma_fakes import NOW, Call, FakeGameIO, RoundScript, make_rules, make_status

from osekkai.features.daruma import texts
from osekkai.features.daruma.flow import GameFlow, GameResult
from osekkai.features.daruma.rules import EndReason, Rules, pick_chant, pick_pause
from osekkai.features.routine.api import RoutineStatus

NAME = "ﾃｽﾄ"
SAY_GO = texts.say_with_name(texts.SAY_GO, NAME)
SAY_WIN = texts.say_with_name(texts.SAY_WIN, NAME)
SEED = 7
STILL = RoundScript(motion=0.0)
MOVED = RoundScript(motion=0.5)


def _flow(
    *,
    rules: Rules | None = None,
    status: RoutineStatus | None = None,
    seed: int = SEED,
) -> GameFlow:
    return GameFlow(
        rules=rules or make_rules(),
        call_name=NAME,
        rng=random.Random(seed),
        status=lambda: status,
        now=lambda: NOW,
        interrupted=lambda: None,
        log=logging.getLogger("test.daruma.flow"),
    )


def _play(
    io: FakeGameIO,
    *,
    rules: Rules | None = None,
    status: RoutineStatus | None = None,
    seed: int = SEED,
) -> GameResult:
    return _flow(rules=rules, status=status, seed=seed).play(io)


def _after(calls: list[Call], name: str, arg: object) -> list[Call]:
    """`name` の口を `arg` で呼んだ最初の呼び出しより後の呼び出し。"""
    for i, call in enumerate(calls):
        if call.name == name and call.args == (arg,):
            return calls[i + 1 :]
    raise AssertionError(f"{name} がありません")


# 始め（1.2〜1.5）


def test_next_task_near_says_before_phrase_and_does_not_open_camera() -> None:
    io = FakeGameIO([STILL])

    result = _play(io, status=make_status(next_task_in=timedelta(minutes=9, seconds=59)))

    assert result == GameResult(EndReason.NEXT_TASK, 0)
    assert io.spoken == [texts.SAY_NEXT_TASK_BEFORE]
    assert "open_camera" not in io.names
    assert io.rounds_started == 0


def test_in_task_does_not_start() -> None:
    io = FakeGameIO([STILL])

    result = _play(io, status=make_status(in_task=True))

    assert result.reason is EndReason.NEXT_TASK
    assert io.spoken == [texts.SAY_NEXT_TASK_BEFORE]
    assert "open_camera" not in io.names


def test_next_task_far_or_routine_not_running_starts() -> None:
    for status in (None, make_status(next_task_in=timedelta(minutes=10))):
        io = FakeGameIO([MOVED], camera=True)

        _play(io, status=status, rules=make_rules(max_rounds=1))

        assert io.names[0] == "open_camera"
        assert io.rounds_started == 1


def test_no_camera_says_no_camera_and_does_not_start() -> None:
    io = FakeGameIO([STILL], camera=False)

    result = _play(io)

    assert result == GameResult(EndReason.NO_CAMERA, 0)
    assert io.spoken == [texts.SAY_NO_CAMERA]
    assert io.shown == []
    assert io.rounds_started == 0


def test_intro_shows_lcd_says_waits_5s_then_go_then_clears_presses() -> None:
    io = FakeGameIO([MOVED])

    _play(io, rules=make_rules(max_rounds=1))

    assert io.calls[:6] == [
        Call("open_camera", (), True),
        Call("show", texts.LCD_INTRO),
        Call("say", (texts.SAY_INTRO,)),
        Call("rest", (texts.INTRO_WAIT_S,)),
        Call("say", (SAY_GO,)),
        Call("clear_presses"),
    ]


def test_presses_before_go_do_not_count() -> None:
    # 始めの 5 秒の間に押されても、1 回目の勝ちにならない（1.3）
    io = FakeGameIO([STILL], press_during_intro=True)

    result = _play(io, rules=make_rules(max_rounds=1))

    assert result == GameResult(EndReason.MAX_ROUNDS, 1)
    assert "celebrate" not in io.names


# 1 回の流れ（2.1〜2.5）


def test_round_shows_chant_looks_away_then_chants_with_fixed_rng() -> None:
    io = FakeGameIO([STILL])

    _play(io, rules=make_rules(max_rounds=1))

    rng = random.Random(SEED)
    chant = pick_chant(rng)
    pause = pick_pause(rng)
    after_go = _after(io.calls, "say", SAY_GO)[1:]  # 押下を消した後
    assert after_go == [
        Call("show", (chant.lcd, texts.FACE_CHANT.text())),
        Call("look", texts.POSE_AWAY),
        Call("say", (chant.speech,)),
        Call("pressed", (), False),
        Call("wait_press", (pause,), False),
        Call("look", texts.POSE_FACING),
        Call("show", (texts.LCD_WATCH, texts.FACE_WATCH.text())),
        Call("say", (texts.SAY_KORONDA,)),
        Call("motion", (2.5,)),
        Call("pressed", (), False),
    ]


def test_same_seed_gives_same_chants() -> None:
    def chants(seed: int) -> list[str]:
        io = FakeGameIO([STILL] * 4)
        _play(io, seed=seed, rules=make_rules(max_rounds=4, nobody_rounds=5))
        return [s for s in io.spoken if s in {c.speech for c in texts.CHANTS}]

    first = chants(SEED)
    assert len(first) == 4
    assert first == chants(SEED)


def test_watch_uses_configured_seconds() -> None:
    io = FakeGameIO([STILL])

    _play(io, rules=make_rules(max_rounds=1, watch_seconds=4.0))

    assert Call("motion", (4.0,)) in io.calls


# 勝ち（3.1、3.3）


def _assert_win(io: FakeGameIO, result: GameResult) -> None:
    assert result == GameResult(EndReason.WIN, 0)
    assert io.calls[-2:] == [
        Call("show", (texts.LCD_WIN, texts.FACE_WIN.text())),
        Call("celebrate", (SAY_WIN,)),
    ]
    assert texts.SAY_KORONDA not in io.spoken
    assert "motion" not in io.names


def test_press_while_chanting_wins() -> None:
    io = FakeGameIO([RoundScript(press_while_chanting=True)])

    result = _play(io)

    _assert_win(io, result)
    assert "wait_press" not in io.names


def test_press_in_pause_wins() -> None:
    io = FakeGameIO([RoundScript(press_in_pause=True)])

    result = _play(io)

    _assert_win(io, result)
    assert [c.result for c in io.calls if c.name == "wait_press"] == [True]


def test_win_in_later_round_counts_finished_rounds() -> None:
    io = FakeGameIO([STILL, MOVED, RoundScript(press_in_pause=True)])

    result = _play(io)

    assert result == GameResult(EndReason.WIN, 2)
    assert io.spoken[-1] == SAY_WIN


# 動いた（2.6〜2.9）


def test_press_while_watching_is_moved_not_win() -> None:
    io = FakeGameIO([RoundScript(press_while_watching=True, motion=0.0)])

    result = _play(io, rules=make_rules(max_rounds=1))

    assert result == GameResult(EndReason.MAX_ROUNDS, 1)
    assert "celebrate" not in io.names
    assert texts.SAY_MOVED in io.spoken


def test_moved_shows_nods_says_rests_then_clears_presses() -> None:
    io = FakeGameIO([MOVED])

    _play(io, rules=make_rules(max_rounds=1))

    after_watch = _after(io.calls, "say", texts.SAY_KORONDA)
    assert after_watch == [
        Call("motion", (2.5,)),
        Call("pressed", (), False),
        Call("show", (texts.LCD_MOVED, texts.FACE_MOVED.text())),
        Call("nod"),
        Call("say", (texts.SAY_MOVED,)),
        Call("rest", (texts.MOVED_WAIT_S,)),
        Call("clear_presses"),
    ]


def test_motion_at_threshold_is_still_and_over_is_moved() -> None:
    io = FakeGameIO([RoundScript(motion=0.01), RoundScript(motion=0.011)])

    _play(io, rules=make_rules(max_rounds=2))

    assert io.spoken.count(texts.SAY_MOVED) == 1
    assert io.names.count("nod") == 1


def test_presses_while_resting_after_moved_do_not_count() -> None:
    io = FakeGameIO([RoundScript(motion=0.5, press_while_resting=True), STILL])

    result = _play(io, rules=make_rules(max_rounds=2))

    assert result == GameResult(EndReason.MAX_ROUNDS, 2)
    assert "celebrate" not in io.names
    # 2 回目は唱えた後の押下を見て、押されていない
    second = _after(io.calls, "say", texts.SAY_MOVED)
    assert [c.result for c in second if c.name == "pressed"] == [False, False]


def test_still_goes_to_next_round_without_rest() -> None:
    io = FakeGameIO([STILL, STILL])

    _play(io, rules=make_rules(max_rounds=2))

    rests = [c for c in io.calls if c.name == "rest"]
    assert rests == [Call("rest", (texts.INTRO_WAIT_S,))]  # 始めの 5 秒だけ
    assert "nod" not in io.names
    assert texts.SAY_MOVED not in io.spoken
    # 見終わった後の押下の確かめの次は、すぐ 2 回目の唱え言葉の LCD
    first_watch = _after(io.calls, "motion", 2.5)
    assert first_watch[0].name == "pressed"
    assert first_watch[1].name == "show"
    assert io.rounds_started == 2


def test_rounds_follow_rules_advance() -> None:
    io = FakeGameIO([MOVED] * 3)

    result = _play(io, rules=make_rules(max_rounds=3))

    assert result == GameResult(EndReason.MAX_ROUNDS, 3)
    assert io.rounds_started == 3
