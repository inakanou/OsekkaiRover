"""カメラを借りる口と、PC の道具用の偽のカメラ（1.4、6.2、8.4）。

本物は見つけてあいさつ（presence）からカメラを借りる。借りている間、見つけてあいさつは
顔を探さない（6.2）。見つけてあいさつの誤り（使えない・使われている）はそのまま出す。
「カメラを使えない」への言いかえは遊びの口（lease_io）が行う。

偽のカメラ（`ScriptedCamera`）は、台本の動きの量を順に返す。
PC の道具（`--fake-hardware`）だけが使う。
映像・静止画は出さない。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from types import TracebackType

import osekkai.features.presence.api as presence_api
from osekkai.core.clock import Clock
from osekkai.features.presence.api import CameraLoan, CameraNotAvailable, CameraNotBorrowed

__all__ = ["BorrowCamera", "ScriptedCamera", "borrow_from_presence"]

# (借りる機能の名前, まだ動いているか) -> 借りたカメラ
BorrowCamera = Callable[[str, Callable[[], bool]], CameraLoan]


def borrow_from_presence(owner: str, alive: Callable[[], bool]) -> CameraLoan:
    """見つけてあいさつからカメラを借りる（1.4、6.2）。

    見つけてあいさつが動いていない・カメラがなければ `CameraNotAvailable`、
    ほかの機能が借りていれば `CameraBusy`。どちらもそのまま出す。
    """
    return presence_api.borrow_camera(owner, alive=alive)


class ScriptedCamera:
    """台本の動きの量を返す偽のカメラ（8.4）。

    `motion(seconds)` は時計で `seconds` 秒待ち、列の値を順に返す（最後まで行ったら初めから）。
    既定の列は、しきい値 0.01 の下と上の両方の判定を出す。
    """

    def __init__(self, clock: Clock, values: Sequence[float] = (0.0, 0.004, 0.03)) -> None:
        if not values:
            raise ValueError("values must not be empty")
        self._clock = clock
        self._values = tuple(values)
        self._lock = threading.Lock()
        self._next = 0

    def borrow(self, owner: str, alive: Callable[[], bool]) -> CameraLoan:
        """カメラを貸す。いつでも貸せる（借り手の名前は使わない）。"""
        return _ScriptedLoan(self, alive)

    def _measure(self, seconds: float) -> float:
        """時計で `seconds` 秒待ち、列の次の値を返す。"""
        self._clock.sleep(seconds)
        with self._lock:
            value = self._values[self._next]
            self._next = (self._next + 1) % len(self._values)
        return value


class _ScriptedLoan:
    """偽のカメラの貸し出し。返した後・借り手が止まった後は断る。"""

    def __init__(self, camera: ScriptedCamera, alive: Callable[[], bool]) -> None:
        self._camera = camera
        self._alive = alive
        self._lock = threading.Lock()
        self._released = False

    def motion(self, seconds: float) -> float:
        self._check()
        return self._camera._measure(seconds)

    def frame(self, max_width: int = 640, quality: int = 70) -> bytes:
        raise CameraNotAvailable()  # 映像は出さない

    def still(self) -> bytes:
        raise CameraNotAvailable()  # 静止画は出さない

    def release(self) -> None:
        with self._lock:
            self._released = True

    def __enter__(self) -> _ScriptedLoan:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def _check(self) -> None:
        """返した後・止まった後は `CameraNotBorrowed`（本物の口と同じ）。"""
        with self._lock:
            released = self._released
        if released:
            raise CameraNotBorrowed()
        try:
            alive = bool(self._alive())
        except Exception:
            alive = False  # 確かめられなければ、止まったとする
        if not alive:
            self.release()
            raise CameraNotBorrowed()
