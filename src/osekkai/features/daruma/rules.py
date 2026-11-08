"""だるまさんの純粋な決まり。回の判定・数え方・終わり方・次の予定・唱え言葉と間。

要件 1.5、2.1、2.3、2.6、4.1、4.2、4.3、4.8、9.1。

純粋な関数と不変の型だけ。資源・時計・乱数の状態を持たない（乱数は引数で受ける）。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from osekkai.features.daruma.config import DarumaConfig
from osekkai.features.daruma.texts import CHANTS, PAUSE_MAX_S, Chant
from osekkai.features.routine.api import RoutineStatus


class RoundKind(Enum):
    """1 回の結果。"""

    WIN = "win"  # 唱える間に押された（3.1）
    MOVED = "moved"  # 見ている間にしきい値を超えたか押された（2.6）
    STILL = "still"  # 動きの量がしきい値以下で、押されなかった


class EndReason(Enum):
    """遊びの終わり方。"""

    WIN = "win"  # 3.3
    MAX_ROUNDS = "max_rounds"  # 4.1（タッチされずに回数を終えた。ローバーの勝ち）
    NEXT_TASK = "next_task"  # 1.5、4.3
    NO_CAMERA = "no_camera"  # 1.4
    CAMERA_LOST = "camera_lost"  # 4.6
    BUSY = "busy"  # 1.6（話さない）
    PREEMPTED = "preempted"  # 4.4（話さない）
    QUIET = "quiet"  # 4.5（話さない）
    STOPPED = "stopped"  # 基盤が止める（話さない）
    ERROR = "error"  # 5.3（話さない）


@dataclass(frozen=True)
class Rules:
    """1 回の遊びの決まり。設定から作る。"""

    threshold: float  # これより大きいと動いた（2.6）
    watch_seconds: float  # 見る長さ（2.5）
    max_rounds: int  # この回数のうちにタッチする（4.1）
    end_before_task: timedelta  # 次のタスクのこれより前に終える（1.5、4.3）

    @classmethod
    def from_config(cls, config: DarumaConfig) -> Rules:
        """設定から決まりを作る。分は長さにする。"""
        return cls(
            threshold=config.threshold,
            watch_seconds=config.watch_seconds,
            max_rounds=config.max_rounds,
            end_before_task=timedelta(minutes=config.end_before_task_min),
        )


@dataclass(frozen=True)
class Tally:
    """ここまでの回の数え。"""

    rounds: int = 0  # 終えた回の数（勝ちの回は数えない）


def judge_watch(motion: float, pressed: bool, threshold: float) -> RoundKind:
    """見終わった後の判定。押された、または動きの量がしきい値より大きければ MOVED（2.6）。"""
    if pressed or motion > threshold:
        return RoundKind.MOVED
    return RoundKind.STILL


def advance(tally: Tally, kind: RoundKind, rules: Rules) -> tuple[Tally, EndReason | None]:
    """回の結果で数えを進め、終わるなら理由を返す（4.1）。

    MOVED・STILL だけを受ける（WIN は ValueError）。
    止まり続けても終えない（止まるのが上手な子と、だれもいないのは見分けられない）。
    """
    if kind is RoundKind.WIN:
        raise ValueError("勝ちの回は数えない")
    after = Tally(rounds=tally.rounds + 1)
    if after.rounds >= rules.max_rounds:
        return after, EndReason.MAX_ROUNDS
    return after, None


def rounds_left(tally: Tally, rules: Rules) -> int:
    """これから始める回を含めた、残りの回の数（4.2）。"""
    return rules.max_rounds - tally.rounds


def schedule_blocks(status: RoutineStatus | None, now: datetime, margin: timedelta) -> bool:
    """次の予定が近いか（1.5、4.3、4.8）。

    日課が動いていない（None）・次のタスクがないなら偽。タスクの最中なら真。
    次のタスクまで `margin` を切っていれば真（時刻を過ぎていても真）。
    `now` は日課と同じ時計の、時差つきの時刻を渡す。
    """
    if status is None:
        return False
    if status.in_task:
        return True
    if status.next_task_at is None:
        return False
    return status.next_task_at - now < margin


def pick_chant(rng: random.Random) -> Chant:
    """唱え言葉を渡された乱数で選ぶ（2.1）。"""
    return rng.choice(CHANTS)


def pick_pause(rng: random.Random) -> float:
    """唱えた後の間（秒）を渡された乱数で選ぶ。0 以上 PAUSE_MAX_S 以下（2.3）。"""
    return rng.uniform(0.0, PAUSE_MAX_S)
