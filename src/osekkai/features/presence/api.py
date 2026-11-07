"""カメラを借りる口（7.1、7.2、7.6、7.7、7.8）。

ほかの機能（だるまさん・ラジコンなど）が首のカメラを借りるための薄い層。
貸し出しの状態（今の借り手・2 つ目を断る・止まった借り手）は見る流れ（`Watcher`）が持つ。
ここには、機能の本体が始めに登録し、終わりに外す「動いている見る流れの置き場」がある。
この口は置き場から動いている見る流れを見つけ、要求を渡して、時間切れつきで結果を待つ。

使い方（ほかの機能の作り手へ）:

    from osekkai.features.presence import api

    def run(self, stop):
        try:
            loan = api.borrow_camera("daruma", alive=lambda: not stop.is_set())
        except (api.CameraNotAvailable, api.CameraBusy):
            ...  # カメラなしで続けるか、少し待って借り直す
        try:
            amount = loan.motion(1.0)  # 0〜1
            jpeg = loan.frame()        # JPEG のバイト列
        finally:
            loan.release()

- 借りられるのは presence が動いている間だけ。動いていない・カメラがない・使えなくなったときは
  `CameraNotAvailable`。ほかの機能が借りていれば `CameraBusy`
- `alive` には「まだ動いているか」を渡す（`lambda: not stop.is_set()` など）。偽になると
  貸し出しは終わり、その後の要求は `CameraNotBorrowed`。返した後の要求も `CameraNotBorrowed`
- 返すのは `finally` か `with` で必ず行う（`with api.borrow_camera(...) as loan:`）
- カメラを借りても首は借りていない。首を動かすなら、rover-core から NECK を別に借りる
- フレームと静止画はメモリの中の JPEG のバイト列で、ファイルには保存しない。子どもの映像を
  保存するのは、使う人が求めたときだけにする（保存するのは借りた機能の側の仕事）
- 時間切れは `TimeoutError`（決まった文言）。貸し出しは続くので、続けるか返すかは借りた側が決める

この module は軽く保つ（機能の本体やアダプタは読み込まない）。読み込むのは見る流れの module だけで、
それは純粋な Python の部品だけを読む（OpenCV・picamera2 は読まない）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import TracebackType
from typing import Final, Protocol, runtime_checkable

from osekkai.features.presence.watcher import (
    CameraBusy,
    CameraNotAvailable,
    CameraNotBorrowed,
    LoanToken,
    Watcher,
)

__all__ = [
    "CameraBusy",
    "CameraLoan",
    "CameraNotAvailable",
    "CameraNotBorrowed",
    "borrow_camera",
    "clear_watcher",
    "register_watcher",
    "running_watcher",
]

# 動きの量は見るスレッドが秒数だけ撮り続けるので、その秒数にこの余裕を足して待つ
MOTION_MARGIN_SECONDS: Final = 5.0
# フレームと静止画を待つ秒数
REQUEST_TIMEOUT_SECONDS: Final = 10.0

_lock = threading.Lock()
_running: Watcher | None = None


def register_watcher(watcher: Watcher) -> None:
    """動いている見る流れを置く（機能の本体が始めに呼ぶ）。"""
    global _running
    with _lock:
        _running = watcher


def clear_watcher(watcher: Watcher) -> None:
    """見る流れを外す（機能の本体が終わりに呼ぶ）。ほかの見る流れが置かれていれば外さない。"""
    global _running
    with _lock:
        if _running is watcher:
            _running = None


def running_watcher() -> Watcher | None:
    """動いている見る流れ。presence が動いていなければ None。"""
    with _lock:
        return _running


@runtime_checkable
class CameraLoan(Protocol):
    """借りたカメラ。返すまで、借りた機能だけが使える。"""

    def motion(self, seconds: float) -> float:
        """`seconds` 秒の間の映像の変化の大きさを 0〜1 で返す（7.3）。"""
        ...

    def frame(self, max_width: int = 640, quality: int = 70) -> bytes:
        """いまの映像の 1 枚を JPEG で返す（7.4）。"""
        ...

    def still(self) -> bytes:
        """カメラの最大の大きさの静止画を JPEG で返す（保存しない。7.5）。"""
        ...

    def release(self) -> None:
        """カメラを返す。2 回呼んでもよい。"""
        ...

    def __enter__(self) -> CameraLoan: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class _WatcherLoan:
    """見る流れに要求を渡して待つ、借りたカメラ。"""

    def __init__(self, watcher: Watcher, lent: LoanToken, alive: Callable[[], bool]) -> None:
        self._watcher = watcher
        self._token = lent
        self._alive = alive
        self._lock = threading.Lock()
        self._released = False

    def motion(self, seconds: float) -> float:
        watcher = self._usable()
        ticket = watcher.request_motion(self._token, seconds)
        return ticket.wait(seconds + MOTION_MARGIN_SECONDS)

    def frame(self, max_width: int = 640, quality: int = 70) -> bytes:
        watcher = self._usable()
        ticket = watcher.request_frame(self._token, max_width, quality)
        return ticket.wait(REQUEST_TIMEOUT_SECONDS)

    def still(self) -> bytes:
        watcher = self._usable()
        ticket = watcher.request_still(self._token)
        return ticket.wait(REQUEST_TIMEOUT_SECONDS)

    def release(self) -> None:
        with self._lock:
            self._released = True
        self._watcher.end_loan(self._token)

    def __enter__(self) -> _WatcherLoan:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def _usable(self) -> Watcher:
        """要求を渡してよい見る流れ。返した後・止まった後は断る（7.6、7.7）。"""
        with self._lock:
            released = self._released
        if released:
            raise CameraNotBorrowed()
        if not self._is_alive():
            self.release()
            raise CameraNotBorrowed()
        if running_watcher() is not self._watcher:
            raise CameraNotAvailable()  # presence が止まった（7.8）
        return self._watcher

    def _is_alive(self) -> bool:
        try:
            return bool(self._alive())
        except Exception:
            return False  # 確かめられなければ、止まったとする（見る流れと同じ）


def borrow_camera(owner: str, *, alive: Callable[[], bool]) -> CameraLoan:
    """首のカメラを借りる（7.1）。借りている間、presence は顔を探さない（7.2）。

    `owner` は借りる機能の名前（記録に出る）。`alive` は借りた機能がまだ動いているか。
    presence が動いていない・カメラがなければ `CameraNotAvailable`（7.8）、
    ほかの機能が借りていれば `CameraBusy`。
    """
    watcher = running_watcher()
    if watcher is None:
        raise CameraNotAvailable()
    lent = watcher.start_loan(owner, alive)
    return _WatcherLoan(watcher, lent, alive)
