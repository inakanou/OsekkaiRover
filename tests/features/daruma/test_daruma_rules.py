"""だるまさんの純粋な決まり（判定・数え方・終わり方・次の予定・唱え言葉と間）。

要件 1.5、2.1、2.3、2.6、4.1、4.2、4.3、4.8、9.1。
"""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from osekkai.features.daruma.config import DarumaConfig
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
from osekkai.features.daruma.texts import CHANTS, PAUSE_MAX_S
from osekkai.features.routine.api import RoutineStatus

NOW = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
MARGIN = timedelta(minutes=10)


def _rules(max_rounds: int = 10) -> Rules:
    return Rules(
        threshold=0.01,
        watch_seconds=2.5,
        max_rounds=max_rounds,
        end_before_task=MARGIN,
    )


def _status(next_task_at: datetime | None = None, *, in_task: bool = False) -> RoutineStatus:
    return RoutineStatus(next_task_at=next_task_at, in_task=in_task, asleep=False, updated_at=NOW)


# --- 設定から決まりを作る ---


def test_rules_from_config_copies_values_and_minutes() -> None:
    config = DarumaConfig(
        threshold=0.2,
        watch_seconds=3.0,
        max_rounds=7,
        end_before_task_min=12,
    )

    rules = Rules.from_config(config)

    assert rules == Rules(
        threshold=0.2,
        watch_seconds=3.0,
        max_rounds=7,
        end_before_task=timedelta(minutes=12),
    )


def test_rules_from_default_config() -> None:
    rules = Rules.from_config(DarumaConfig())

    assert rules == _rules()


# --- 見終わった後の判定（2.6） ---


def test_judge_motion_equal_to_threshold_is_still() -> None:
    assert judge_watch(0.01, False, 0.01) is RoundKind.STILL


def test_judge_motion_over_threshold_is_moved() -> None:
    assert judge_watch(0.0101, False, 0.01) is RoundKind.MOVED


def test_judge_no_motion_is_still() -> None:
    assert judge_watch(0.0, False, 0.01) is RoundKind.STILL


def test_judge_press_only_is_moved() -> None:
    assert judge_watch(0.0, True, 0.01) is RoundKind.MOVED


def test_judge_press_and_motion_is_moved() -> None:
    assert judge_watch(0.5, True, 0.01) is RoundKind.MOVED


# --- 回数と終わり方（4.1） ---


def test_advance_counts_round() -> None:
    tally, end = advance(Tally(), RoundKind.STILL, _rules())

    assert tally == Tally(rounds=1)
    assert end is None


def test_advance_still_rounds_do_not_end_before_max_rounds() -> None:
    # 止まるのが上手な子は、だれもいないと見分けられない。止まり続けても終えない
    rules = _rules(max_rounds=10)
    tally = Tally()
    ends = []
    for _ in range(9):
        tally, end = advance(tally, RoundKind.STILL, rules)
        ends.append(end)

    assert ends == [None] * 9
    tally, end = advance(tally, RoundKind.STILL, rules)
    assert tally == Tally(rounds=10)
    assert end is EndReason.MAX_ROUNDS


def test_advance_ends_at_max_rounds() -> None:
    rules = _rules(max_rounds=10)

    before, end_before = advance(Tally(rounds=8), RoundKind.MOVED, rules)
    last, end_last = advance(before, RoundKind.MOVED, rules)

    assert before.rounds == 9
    assert end_before is None
    assert last.rounds == 10
    assert end_last is EndReason.MAX_ROUNDS


def test_no_nobody_end_reason() -> None:
    assert "NOBODY" not in EndReason.__members__


# --- 残りの回数（4.2） ---


def test_rounds_left_counts_this_round() -> None:
    rules = _rules(max_rounds=10)

    assert rounds_left(Tally(), rules) == 10
    assert rounds_left(Tally(rounds=7), rules) == 3
    assert rounds_left(Tally(rounds=9), rules) == 1


def test_advance_does_not_take_win() -> None:
    with pytest.raises(ValueError):
        advance(Tally(), RoundKind.WIN, _rules())


# --- 次の予定（1.5、4.3、4.8） ---


def test_schedule_without_status_does_not_block() -> None:
    assert schedule_blocks(None, NOW, MARGIN) is False


def test_schedule_in_task_blocks() -> None:
    assert schedule_blocks(_status(None, in_task=True), NOW, MARGIN) is True


def test_schedule_without_next_task_does_not_block() -> None:
    assert schedule_blocks(_status(None), NOW, MARGIN) is False


def test_schedule_exactly_margin_does_not_block() -> None:
    assert schedule_blocks(_status(NOW + MARGIN), NOW, MARGIN) is False


def test_schedule_just_inside_margin_blocks() -> None:
    near = NOW + MARGIN - timedelta(seconds=1)

    assert schedule_blocks(_status(near), NOW, MARGIN) is True


def test_schedule_past_task_time_blocks() -> None:
    assert schedule_blocks(_status(NOW - timedelta(minutes=1)), NOW, MARGIN) is True


def test_schedule_far_task_does_not_block() -> None:
    assert schedule_blocks(_status(NOW + timedelta(hours=1)), NOW, MARGIN) is False


def test_schedule_zero_margin_blocks_only_after_task_time() -> None:
    zero = timedelta(0)

    assert schedule_blocks(_status(NOW), NOW, zero) is False
    assert schedule_blocks(_status(NOW - timedelta(seconds=1)), NOW, zero) is True


def test_schedule_asleep_flag_alone_does_not_block() -> None:
    status = replace(_status(None), asleep=True)

    assert schedule_blocks(status, NOW, MARGIN) is False


# --- 唱え言葉と間（2.1、2.3、9.1） ---


def test_pick_chant_same_seed_same_order() -> None:
    rng_a = random.Random(42)
    rng_b = random.Random(42)

    seq_a = [pick_chant(rng_a) for _ in range(20)]
    seq_b = [pick_chant(rng_b) for _ in range(20)]

    assert seq_a == seq_b
    assert all(chant in CHANTS for chant in seq_a)


def test_pick_chant_uses_all_chants() -> None:
    rng = random.Random(1)

    picked = {pick_chant(rng) for _ in range(200)}

    assert picked == set(CHANTS)


def test_pick_pause_same_seed_same_order_and_in_range() -> None:
    rng_a = random.Random(42)
    rng_b = random.Random(42)

    seq_a = [pick_pause(rng_a) for _ in range(200)]
    seq_b = [pick_pause(rng_b) for _ in range(200)]

    assert seq_a == seq_b
    assert all(0.0 <= p <= PAUSE_MAX_S for p in seq_a)
    assert len(set(seq_a)) > 1


def test_pick_uses_only_given_rng() -> None:
    random.seed(0)
    a = (pick_chant(random.Random(5)), pick_pause(random.Random(5)))
    random.seed(999)
    b = (pick_chant(random.Random(5)), pick_pause(random.Random(5)))

    assert a == b
