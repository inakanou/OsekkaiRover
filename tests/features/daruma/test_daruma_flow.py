"""だるまさんの 1 回の遊びの流れ（始めから勝ち・動いた・終わり方・中断まで）。

要件 1.2〜1.5、2.1〜2.9、3.1、3.3、4.1〜4.7、5.3、6.3、7.5、9.1。
偽の遊びの口・固定の乱数・偽の日課の状況で確かめる。
"""

import logging
import random
from collections.abc import Callable
from datetime import timedelta

import pytest
from daruma_fakes import NOW, Call, FakeGameIO, RoundScript, make_rules, make_status

from osekkai.core.types import DeviceUnavailable, LeaseRevoked, SpeechFailed
from osekkai.features.daruma import texts
from osekkai.features.daruma.flow import END_SAYS, CameraLost, GameFlow, GameResult
from osekkai.features.daruma.rules import EndReason, Rules, pick_chant, pick_pause
from osekkai.features.routine.api import RoutineStatus

NAME = "ﾃｽﾄ"
SAY_GO = texts.say_with_name(texts.say_go(10), NAME)
LAST = texts.SAY_ROUNDS_LEFT[1]  # さいごの いっかい
SAY_WIN = texts.say_with_name(texts.SAY_WIN, NAME)
SEED = 7
STILL = RoundScript(motion=0.0)
MOVED = RoundScript(motion=0.5)


def _flow(
    *,
    rules: Rules | None = None,
    status: RoutineStatus | None = None,
    seed: int = SEED,
    status_fn: Callable[[], RoutineStatus | None] | None = None,
    interrupted: Callable[[], EndReason | None] = lambda: None,
) -> GameFlow:
    return GameFlow(
        rules=rules or make_rules(),
        call_name=NAME,
        rng=random.Random(seed),
        status=status_fn or (lambda: status),
        now=lambda: NOW,
        interrupted=interrupted,
        log=logging.getLogger("test.daruma.flow"),
    )


def _play(
    io: FakeGameIO,
    *,
    rules: Rules | None = None,
    status: RoutineStatus | None = None,
    seed: int = SEED,
    status_fn: Callable[[], RoutineStatus | None] | None = None,
    interrupted: Callable[[], EndReason | None] = lambda: None,
) -> GameResult:
    flow = _flow(
        rules=rules, status=status, seed=seed, status_fn=status_fn, interrupted=interrupted
    )
    return flow.play(io)


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
        Call("say", (texts.say_with_name(texts.say_go(1), NAME),)),
        Call("clear_presses"),
    ]


@pytest.mark.parametrize("max_rounds", [1, 10, 50])
def test_go_says_configured_rounds(max_rounds: int) -> None:
    io = FakeGameIO([RoundScript(press_while_chanting=True)])

    _play(io, rules=make_rules(max_rounds=max_rounds))

    go = texts.say_with_name(texts.say_go(max_rounds), NAME)
    assert io.spoken[1] == go  # 1.3
    assert go.startswith(f"{max_rounds}かいの うちに ")


def test_presses_before_go_do_not_count() -> None:
    # 始めの 5 秒の間に押されても、1 回目の勝ちにならない（1.3）
    io = FakeGameIO([STILL], press_during_intro=True)

    result = _play(io, rules=make_rules(max_rounds=1))

    assert result == GameResult(EndReason.MAX_ROUNDS, 1)
    assert "celebrate" not in io.names


# 1 回の流れ（2.1〜2.5）


def test_round_shows_chant_looks_away_then_chants_with_fixed_rng() -> None:
    io = FakeGameIO([STILL] * 10)

    _play(io)  # 1 回目は残りの回数を話さない

    rng = random.Random(SEED)
    chant = pick_chant(rng)
    pause = pick_pause(rng)
    after_go = _after(io.calls, "say", SAY_GO)[1:]  # 押下を消した後
    assert after_go[:10] == [
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
        _play(io, seed=seed, rules=make_rules(max_rounds=4))
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
    assert io.calls[-3:] == [
        Call("show", (texts.LCD_WIN, texts.FACE_WIN.text())),
        Call("celebrate", (SAY_WIN,)),
        Call("home"),  # 話し終えたら首を正面に（4.7）
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
    assert after_watch[:7] == [
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
    # 見終わった後の押下の確かめの次は、すぐ 2 回目の残りの回数と唱え言葉の LCD
    first_watch = _after(io.calls, "motion", 2.5)
    assert first_watch[0].name == "pressed"
    assert first_watch[1] == Call("say", (LAST,))
    assert first_watch[2].name == "show"
    assert io.rounds_started == 2


def test_rounds_follow_rules_advance() -> None:
    io = FakeGameIO([MOVED] * 3)

    result = _play(io, rules=make_rules(max_rounds=3))

    assert result == GameResult(EndReason.MAX_ROUNDS, 3)
    assert io.rounds_started == 3


# 終わり方（4.1〜4.3、4.6、4.7）


class _Flag:
    """中断の確かめの偽物。`reason` を立てると、それを返す。"""

    def __init__(self) -> None:
        self.reason: EndReason | None = None

    def __call__(self) -> EndReason | None:
        return self.reason

    def raise_on(self, text: str, reason: EndReason) -> Callable[[str], None]:
        """`text` を話し終えたときに旗を立てる口を返す。"""

        def hook(spoken: str) -> None:
            if spoken == text:
                self.reason = reason

        return hook


def _chant_speeches() -> set[str]:
    return {chant.speech for chant in texts.CHANTS}


def test_end_says_covers_spoken_endings_only() -> None:
    assert END_SAYS == {
        EndReason.MAX_ROUNDS: texts.SAY_MAX_ROUNDS,
        EndReason.NEXT_TASK: texts.SAY_NEXT_TASK,
        EndReason.NO_CAMERA: texts.SAY_NO_CAMERA,
        EndReason.CAMERA_LOST: texts.SAY_CAMERA_LOST,
    }


def test_ends_after_max_rounds_with_rover_wins_phrase_then_home() -> None:
    io = FakeGameIO([MOVED] * 10)

    result = _play(io)

    assert result == GameResult(EndReason.MAX_ROUNDS, 10)
    assert io.rounds_started == 10
    assert io.calls[-2:] == [Call("say", (texts.SAY_MAX_ROUNDS,)), Call("home")]
    assert "ローバーの かち" in texts.SAY_MAX_ROUNDS  # 4.1


def test_still_rounds_do_not_end_early() -> None:
    # 止まり続けても、だれもいないとして終えない。10 回でローバーの勝ち（4.1）
    io = FakeGameIO([STILL] * 10)

    result = _play(io)

    assert result == GameResult(EndReason.MAX_ROUNDS, 10)
    assert io.spoken[-1] == texts.SAY_MAX_ROUNDS


def _left_says_before_chants(io: FakeGameIO) -> list[str | None]:
    """回ごとに、唱え言葉の直前に話した残りの回数の言葉（なければ None）。"""
    left = set(texts.SAY_ROUNDS_LEFT.values())
    found: list[str | None] = []
    pending: str | None = None
    for text in io.spoken:
        if text in left:
            pending = text
        elif text in _chant_speeches():
            found.append(pending)
            pending = None
    return found


def test_rounds_left_said_only_for_last_three_rounds() -> None:
    io = FakeGameIO([STILL] * 10)

    _play(io)

    assert _left_says_before_chants(io) == [None] * 7 + [
        "あと さんかい",
        "あと にかい",
        "さいごの いっかい",
    ]  # 4.2


@pytest.mark.parametrize(
    ("max_rounds", "expected"),
    [
        (1, ["さいごの いっかい"]),
        (2, ["あと にかい", "さいごの いっかい"]),
        (3, ["あと さんかい", "あと にかい", "さいごの いっかい"]),
    ],
)
def test_rounds_left_with_few_rounds(max_rounds: int, expected: list[str]) -> None:
    io = FakeGameIO([STILL] * max_rounds)

    _play(io, rules=make_rules(max_rounds=max_rounds))

    assert _left_says_before_chants(io) == expected


def test_rounds_left_is_said_before_chant_lcd() -> None:
    io = FakeGameIO([STILL] * 3)

    _play(io, rules=make_rules(max_rounds=3))

    chant = pick_chant(random.Random(SEED))
    after_go = _after(io.calls, "say", texts.say_with_name(texts.say_go(3), NAME))
    assert after_go[:4] == [
        Call("clear_presses"),
        Call("say", ("あと さんかい",)),
        Call("show", (chant.lcd, texts.FACE_CHANT.text())),
        Call("look", texts.POSE_AWAY),
    ]


def test_rounds_left_after_win_is_not_said() -> None:
    io = FakeGameIO([STILL] * 7 + [RoundScript(press_in_pause=True)])

    result = _play(io)

    assert result == GameResult(EndReason.WIN, 7)
    assert _left_says_before_chants(io) == [None] * 7 + ["あと さんかい"]
    assert io.spoken[-1] == SAY_WIN


def test_next_task_near_before_round_2_ends_with_round_phrase() -> None:
    io = FakeGameIO([MOVED, MOVED])
    near = make_status(next_task_in=timedelta(minutes=5))

    # 始める前と 1 回目の前は遠い。1 回目の後で近くなる
    def status() -> RoutineStatus | None:
        return near if io.rounds_started >= 1 else None

    result = _play(io, status_fn=status)

    assert result == GameResult(EndReason.NEXT_TASK, 1)
    assert io.rounds_started == 1
    assert texts.SAY_NEXT_TASK_BEFORE not in io.spoken
    assert io.calls[-2:] == [Call("say", (texts.SAY_NEXT_TASK,)), Call("home")]


def test_start_endings_return_head_after_phrase() -> None:
    io = FakeGameIO([STILL], camera=False)

    _play(io)

    assert io.calls[-2:] == [Call("say", (texts.SAY_NO_CAMERA,)), Call("home")]


def test_camera_error_while_watching_says_phrase_then_ends() -> None:
    io = FakeGameIO([MOVED, RoundScript(motion=CameraLost())])

    result = _play(io)

    assert result == GameResult(EndReason.CAMERA_LOST, 1)
    assert io.calls[-3:] == [
        Call("motion", (2.5,)),
        Call("say", (texts.SAY_CAMERA_LOST,)),
        Call("home"),
    ]


def test_camera_error_when_opening_says_phrase_then_ends() -> None:
    io = FakeGameIO([STILL], errors={"open_camera": CameraLost()})

    result = _play(io)

    assert result == GameResult(EndReason.CAMERA_LOST, 0)
    assert io.spoken == [texts.SAY_CAMERA_LOST]
    assert io.names[-1] == "home"


# 中断・おやすみ・止める旗（4.4、4.5）


@pytest.mark.parametrize("reason", [EndReason.PREEMPTED, EndReason.QUIET, EndReason.STOPPED])
def test_interrupt_while_chanting_finishes_words_then_says_nothing(reason: EndReason) -> None:
    flag = _Flag()
    chants: list[str] = []

    # 2 回目の唱え言葉を話し終えたときに旗を立てる
    def after_say(text: str) -> None:
        if text in _chant_speeches():
            chants.append(text)
            if len(chants) == 2:
                flag.reason = reason

    io = FakeGameIO([MOVED, MOVED], after_say=after_say)

    result = _play(io, interrupted=flag)

    assert result == GameResult(reason, 1)
    # 唱え言葉は言い終え、その後は何の手順もしない（首も動かさない）
    assert io.calls[-1].name == "say"
    assert io.calls[-1].args[0] in _chant_speeches()
    assert "home" not in io.names


@pytest.mark.parametrize("reason", [EndReason.PREEMPTED, EndReason.QUIET, EndReason.STOPPED])
def test_interrupt_before_start_says_nothing(reason: EndReason) -> None:
    io = FakeGameIO([STILL])

    result = _play(io, interrupted=lambda: reason)

    assert result == GameResult(reason, 0)
    assert io.calls == []


def test_interrupt_during_moved_rest_ends_without_more_steps() -> None:
    flag = _Flag()
    io = FakeGameIO([MOVED, MOVED], after_say=flag.raise_on(texts.SAY_MOVED, EndReason.QUIET))

    result = _play(io, interrupted=flag)

    assert result == GameResult(EndReason.QUIET, 0)
    assert io.names[-1] == "say"
    assert io.spoken[-1] == texts.SAY_MOVED
    assert "home" not in io.names


def test_interrupt_before_end_phrase_does_not_say_it() -> None:
    flag = _Flag()
    io = FakeGameIO([MOVED], after_say=flag.raise_on(texts.SAY_MOVED, EndReason.PREEMPTED))

    result = _play(io, rules=make_rules(max_rounds=1), interrupted=flag)

    assert result.reason is EndReason.PREEMPTED
    assert texts.SAY_MAX_ROUNDS not in io.spoken
    assert "home" not in io.names


def test_interrupt_after_end_phrase_does_not_move_head() -> None:
    flag = _Flag()
    io = FakeGameIO([MOVED], after_say=flag.raise_on(texts.SAY_MAX_ROUNDS, EndReason.PREEMPTED))

    result = _play(io, rules=make_rules(max_rounds=1), interrupted=flag)

    assert result == GameResult(EndReason.PREEMPTED, 1)
    assert io.spoken[-1] == texts.SAY_MAX_ROUNDS
    assert "home" not in io.names


# 話す口・首の誤り（4.4、5.3）


def test_revoked_while_speaking_ends_silently() -> None:
    io = FakeGameIO([MOVED], say_errors={texts.SAY_KORONDA: LeaseRevoked()})

    result = _play(io)

    assert result == GameResult(EndReason.PREEMPTED, 0)
    assert io.spoken[-1] == texts.SAY_KORONDA
    assert io.names[-1] == "say"


def test_revoked_on_head_step_ends_silently() -> None:
    io = FakeGameIO([MOVED], errors={"look": LeaseRevoked()})

    result = _play(io)

    assert result.reason is EndReason.PREEMPTED
    assert io.names[-1] == "look"


@pytest.mark.parametrize("error", [SpeechFailed(NAME), DeviceUnavailable(NAME)])
def test_speech_or_device_failure_ends_with_error_and_logs_type_only(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    io = FakeGameIO([MOVED], say_errors={SAY_GO: error})

    result = _play(io)

    assert result == GameResult(EndReason.ERROR, 0)
    assert io.names[-1] == "say"
    assert type(error).__name__ in caplog.text
    assert NAME not in caplog.text


def test_head_failure_ends_with_error() -> None:
    io = FakeGameIO([MOVED], errors={"nod": DeviceUnavailable()})

    result = _play(io)

    assert result.reason is EndReason.ERROR
    assert io.names[-1] == "nod"


def test_other_exceptions_propagate() -> None:
    io = FakeGameIO([MOVED], errors={"look": ValueError("x")})

    with pytest.raises(ValueError):
        _play(io)


# ログ（6.3、7.5）


def test_logs_have_no_call_name_or_motion(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    flag = _Flag()
    scenarios: list[tuple[FakeGameIO, Callable[[], EndReason | None]]] = [
        (FakeGameIO([RoundScript(motion=0.4321)] * 15), lambda: None),
        (FakeGameIO([STILL] * 10), lambda: None),
        (FakeGameIO([RoundScript(press_in_pause=True)]), lambda: None),
        (FakeGameIO([RoundScript(motion=CameraLost(NAME))]), lambda: None),
        (FakeGameIO([MOVED], after_say=flag.raise_on(SAY_GO, EndReason.QUIET)), flag),
    ]
    for io, interrupted in scenarios:
        _play(io, interrupted=interrupted)

    assert caplog.records  # 終わり方の種類は出す
    assert NAME not in caplog.text
    assert "0.4321" not in caplog.text
