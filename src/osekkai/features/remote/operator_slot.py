"""操作する端末を 1 台にする（要件 3.1〜3.4）。

- 1 つのロックの中で、操作する端末の印と最後の操作の時刻を持つ
- 操作のたびに `claim` を呼ぶ。映像を見る・状態を聞くは操作に入らないので、呼ぶ側は `claim` しない
- 10 秒の交代は `expire` で起こす。`claim` は自分では空にしない（空にしたら走行を止めるのは呼ぶ側）
- 印をログに出さない
"""

from __future__ import annotations

import threading
from enum import Enum
from typing import Final

from osekkai.core.clock import Clock


class ClaimResult(Enum):
    MINE = "mine"
    OTHER = "other"  # 3.2


class OperatorSlot:
    """操作する端末の席。最後の操作から 10 秒で空く（3.3）。"""

    IDLE_SECONDS: Final = 10.0

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._holder: str | None = None
        self._last = 0.0

    def claim(self, token: str) -> ClaimResult:
        """空か自分の席なら自分の席にし、最後の操作の時刻を更新する（3.1）。

        ほかの端末の席なら OTHER を返し、時刻を更新しない（3.2）。
        """
        with self._lock:
            if self._holder is None or self._holder == token:
                self._holder = token
                self._last = self._clock.monotonic()
                return ClaimResult.MINE
            return ClaimResult.OTHER

    def holder(self) -> str | None:
        with self._lock:
            return self._holder

    def expire(self) -> bool:
        """最後の操作からちょうど 10 秒以上たったら席を空にして True（3.3）。

        時計が戻った（今が最後の操作より前）ときも空にする（止まる側に倒す）。
        """
        with self._lock:
            if self._holder is None:
                return False
            elapsed = self._clock.monotonic() - self._last
            if elapsed < 0 or elapsed >= self.IDLE_SECONDS:
                self._holder = None
                return True
            return False

    def drop(self, token: str) -> bool:
        """その端末の席なら空にして True（ログインが無効になった。3.4）。"""
        with self._lock:
            if self._holder is not None and self._holder == token:
                self._holder = None
                return True
            return False
