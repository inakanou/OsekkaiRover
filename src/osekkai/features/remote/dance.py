"""首だけのダンス（要件 7.4、7.6）。

サンプルと同じ 6 段（首を左右 30 度・上 15 度、1 段 0.25 秒）を、
首の向きを変える口だけで進める。走行の口を受け取らないので、タイヤとステアリングは動かない。

- 段ごとに、動く前に go() を確かめる。偽なら次の段に進まずに False を返す
- 最後の段のあとも、戻す前にもう 1 度 go() を確かめる。偽なら戻さずに False を返す
- 止めたときは首を戻さない。リースを返すと基盤が正面に戻す
- 首の口の例外（リースの取り上げなど）は、そのまま呼ぶ側に出す
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class DanceStep:
    """ダンスの 1 段の首の向き（度）。"""

    pan: float
    tilt: float


_PAN: Final = 30.0
_TILT: Final = 15.0

DANCE_STEPS: Final[tuple[DanceStep, ...]] = tuple(
    DanceStep(pan=_PAN if i % 2 == 0 else -_PAN, tilt=_TILT) for i in range(6)
)
STEP_SECONDS: Final = 0.25


def run_dance(
    look: Callable[[float, float], None],
    go: Callable[[], bool],
    sleep: Callable[[float], None],
    back_to: tuple[float, float],
) -> bool:
    """踊る。最後まで踊って踊る前の向きに戻せたら True、途中で止めたら False。"""
    for step in DANCE_STEPS:
        if not go():
            return False
        look(step.pan, step.tilt)
        sleep(STEP_SECONDS)
    if not go():
        return False
    look(*back_to)
    return True
