"""首と USB のカメラの映像を配る（要件 8.1〜8.5、9.1）。

- カメラごとに 1 つのハブ（`FrameHub`）がある。読み取りはハブごとに 1 つの補助のスレッド
  （`remote-cam-neck`・`remote-cam-usb`）で行い、最新の 1 枚だけをメモリに持つ。保存しない（8.5）
- 1 秒に 10 枚までしか読まない（8.1）。見る端末は 3 つまで（8.2）
- 見る端末は元に触らない。最新の 1 枚を待って受け取るだけ。まだ送っていない新しい 1 枚だけを渡す
- 読み取りに失敗したら使えない印にし、元を閉じ、1 秒おいて開き直す（8.3）。
  もう一方のカメラには影響しない
- 見る端末がなくなって 3 秒たったら、元を閉じてスレッドを終える（8.4）。また見に来たら始める
- 首のカメラ（`NeckSource`）は、首のリースを持っている間だけ見つけてあいさつから借りる。
  返す合図（`FrameHub.release_neck`）で、読み取りの 1 歩を待たずに、すぐ返す
- 誤りはログに型の名前だけを出す。続く誤りは 60 秒に 1 回だけ出す

ロックは 2 つ。
- `_cond`: 状態（見る端末の数・最新の 1 枚・使えない印・開いているか）を守る。短くだけ持つ
- `_source_lock`: 元を開く・読む・撮る・閉じるを 1 つずつにする。首の返す合図だけは
  これを取らない（首の元は自分で、どのスレッドから閉じてもよいようにしている）
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from enum import Enum
from typing import Final, Protocol, TypeVar

import osekkai.features.presence.api as presence_api
from osekkai.core.clock import Clock
from osekkai.features.presence.api import (
    CameraBusy,
    CameraLoan,
    CameraNotAvailable,
    CameraNotBorrowed,
)
from osekkai.features.remote.ports import FrameSource, UsbCameraPort

__all__ = [
    "BorrowCamera",
    "Camera",
    "FrameHub",
    "NeckSource",
    "UsbSource",
    "Viewer",
    "borrow_from_presence",
]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

OWNER: Final = "remote"
NECK_MAX_WIDTH: Final = 480
NECK_QUALITY: Final = 70
RETRY_S: Final = 1.0  # 失敗から開き直すまで（8.3）
LOG_EVERY_S: Final = 60.0  # 続く誤りのログの間
JOIN_S: Final = 2.0  # 閉じるときにスレッドを待つ上限
EPSILON_S: Final = 1e-6  # 時刻の足し算の丸めの誤差
# 首のカメラが読めない誤り（ない・presence が止まった・ほかが借りている・返した・時間切れ）
NECK_ERRORS: Final = (CameraNotAvailable, CameraBusy, CameraNotBorrowed, TimeoutError)

# (借りる機能の名前, まだ動いているか) -> 借りたカメラ
BorrowCamera = Callable[[str, Callable[[], bool]], CameraLoan]


class Camera(Enum):
    NECK = "neck"
    USB = "usb"


_CAMERA_NAMES: Final[Mapping[Camera, str]] = {Camera.NECK: "首のカメラ", Camera.USB: "USB のカメラ"}


def borrow_from_presence(owner: str, alive: Callable[[], bool]) -> CameraLoan:
    """見つけてあいさつから首のカメラを借りる。使えなければその誤りをそのまま出す。"""
    return presence_api.borrow_camera(owner, alive=alive)


class NeckSource:
    """首のカメラの元（FrameSource）。首のリースを持っている間だけ借りる。

    `may_borrow` は「首のリースを持っているか」。偽の間は借りず、借りていれば返す。
    `close()` はどのスレッドから呼んでもよい（返す合図のため）。
    """

    def __init__(
        self,
        may_borrow: Callable[[], bool],
        *,
        borrow: BorrowCamera = borrow_from_presence,
    ) -> None:
        self._may_borrow = may_borrow
        self._borrow = borrow
        self._lock = threading.Lock()
        self._loan: CameraLoan | None = None

    def open(self) -> bool:
        if not self._allowed():
            return False
        with self._lock:
            if self._loan is not None:
                return True
        try:
            loan = self._borrow(OWNER, self._allowed)
        except (CameraNotAvailable, CameraBusy):
            return False
        old = self._swap(loan)
        if old is not None:
            old.release()  # ほかのスレッドが先に借りていた
        if not self._allowed():
            self.close()  # 借りている間にリースを失った
            return False
        return True

    def read(self) -> bytes | None:
        loan = self._usable()
        if loan is None:
            return None
        try:
            return loan.frame(max_width=NECK_MAX_WIDTH, quality=NECK_QUALITY)
        except NECK_ERRORS:
            return None

    def still(self) -> bytes | None:
        loan = self._usable()
        if loan is None:
            return None
        try:
            return loan.still()
        except NECK_ERRORS:
            return None

    def close(self) -> None:
        loan = self._swap(None)
        if loan is not None:
            loan.release()

    def _swap(self, loan: CameraLoan | None) -> CameraLoan | None:
        """借りたカメラを入れ替え、前のものを返す。"""
        with self._lock:
            old, self._loan = self._loan, loan
        return old

    def _usable(self) -> CameraLoan | None:
        """今読んでよい借りたカメラ。リースを失っていれば返して None。"""
        with self._lock:
            loan = self._loan
        if loan is None:
            return None
        if not self._allowed():
            self.close()
            return None
        return loan

    def _allowed(self) -> bool:
        try:
            return bool(self._may_borrow())
        except Exception:
            return False  # 確かめられなければ、持っていないとする


class UsbSource:
    """USB のカメラの元（FrameSource）。写真は 1 枚読む。"""

    def __init__(self, port: UsbCameraPort) -> None:
        self._port = port

    def open(self) -> bool:
        return self._port.open()

    def read(self) -> bytes | None:
        return self._port.read_jpeg()

    def still(self) -> bytes | None:
        return self._port.read_jpeg()

    def close(self) -> None:
        self._port.close()


class Viewer(Protocol):
    def next_frame(self, timeout: float) -> bytes | None:
        """まだ送っていない新しい 1 枚。`timeout` 秒のうちになければ None。"""
        ...


class _Seat:
    """見る端末の席。送った 1 枚の通し番号を覚える。"""

    def __init__(self, hub: FrameHub) -> None:
        self._hub = hub
        self.sent = 0

    def next_frame(self, timeout: float) -> bytes | None:
        return self._hub._next_for(self, timeout)


class FrameHub:
    """1 つのカメラの映像を、見る端末に配る。"""

    MAX_VIEWERS: Final = 3  # 8.2
    MAX_FPS: Final = 10.0  # 8.1
    IDLE_CLOSE_S: Final = 3.0  # 8.4

    def __init__(
        self,
        camera: Camera,
        source: FrameSource,
        clock: Clock,
        *,
        run_in_thread: bool = True,
    ) -> None:
        """`run_in_thread=False` は、テストで読み取りを 1 歩ずつ（`_step`）進めるとき。"""
        self.camera = camera
        self._source = source
        self._clock = clock
        self._run_in_thread = run_in_thread
        self._cond = threading.Condition()
        self._source_lock = threading.Lock()
        self._viewers = 0
        self._active = False  # 読み取りを続けるか（見る端末がいる・3 秒たっていない）
        self._closed = False
        self._idle_since: float | None = None
        self._thread: threading.Thread | None = None
        self._frame: bytes | None = None
        self._seq = 0
        self._available = True
        self._opened = False
        self._retry_at: float | None = None
        self._next_at: float | None = None
        self._logged_at: float | None = None

    # --- 見る端末 ---

    @contextmanager
    def viewer(self) -> Iterator[Viewer | None]:
        """席を取る。満員・閉じた後は None（8.2）。席は抜けるときに返す。"""
        seat = self._take_seat()
        try:
            yield seat
        finally:
            if seat is not None:
                self._leave()

    def viewers(self) -> int:
        with self._cond:
            return self._viewers

    def available(self) -> bool:
        """使えるか（8.3）。読み取りに失敗した・首のカメラを返した後は偽。"""
        with self._cond:
            return self._available

    def latest(self) -> bytes | None:
        with self._cond:
            return self._frame

    # --- 写真 ---

    def still(self) -> bytes | None:
        """写真の 1 枚（9.1）。読んでいなければ開いて撮って閉じる。撮れなければ None。"""
        with self._cond:
            if self._closed:
                return None
        with self._source_lock:
            with self._cond:
                opened_here = not self._opened
            if opened_here and not self._call(self._source.open):
                return None
            try:
                return self._call(self._source.still)
            finally:
                if opened_here:
                    self._call(self._source.close)

    # --- 進める・閉じる ---

    def tick(self) -> None:
        """見る端末がなくなって 3 秒たったら、読み取りを終えさせる（8.4）。"""
        with self._cond:
            idle = self._idle_since
            if self._viewers or not self._active or idle is None:
                return
            if self._clock.monotonic() - idle >= self.IDLE_CLOSE_S:
                self._active = False
                self._cond.notify_all()

    def release_neck(self) -> None:
        """首のカメラを返す合図。読み取りの 1 歩を待たずに、すぐ返す。USB のハブでは何もしない。"""
        if self.camera is not Camera.NECK:
            return
        with self._cond:
            self._opened = False
            self._available = False
            self._frame = None
            self._retry_at = self._clock.monotonic() + RETRY_S
        self._call(self._source.close)

    def close(self) -> None:
        """読み取りを終え、元を閉じる。何度呼んでもよい。"""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._active = False
            thread = self._thread
            self._cond.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(JOIN_S)
            if thread.is_alive():
                return  # スレッドは終わるときに元を閉じる
        self._close_source()

    # --- 内側 ---

    def _take_seat(self) -> _Seat | None:
        with self._cond:
            if self._closed or self._viewers >= self.MAX_VIEWERS:
                return None
            self._viewers += 1
            self._idle_since = None
            self._active = True
            if self._run_in_thread and self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name=f"remote-cam-{self.camera.value}", daemon=True
                )
                self._thread.start()
            self._cond.notify_all()
            return _Seat(self)

    def _leave(self) -> None:
        with self._cond:
            self._viewers -= 1
            if self._viewers == 0:
                self._idle_since = self._clock.monotonic()

    def _next_for(self, seat: _Seat, timeout: float) -> bytes | None:
        with self._cond:

            def newer() -> bool:
                return self._closed or (self._frame is not None and self._seq > seat.sent)

            self._cond.wait_for(newer, timeout)
            if self._closed or self._frame is None or self._seq <= seat.sent:
                return None
            seat.sent = self._seq
            return self._frame

    def _run(self) -> None:
        """補助のスレッド。読み取りの 1 歩を繰り返す。終えるときは元を閉じる。"""
        while True:
            try:
                wait = self._step()
            except Exception as e:  # 1 歩の中で受けきれなかった誤り
                self._log(e)
                wait = RETRY_S
            with self._cond:
                if wait is None:
                    if self._active and not self._closed:
                        continue  # 閉じている間に、また見に来た
                    self._thread = None
                    return
                self._cond.wait(wait)

    def _step(self) -> float | None:
        """読み取りの 1 歩。次の 1 歩までの秒数を返す。読み取りを終えたら None。"""
        with self._cond:
            active = self._active and not self._closed
        if not active:
            self._close_source()
            return None
        with self._source_lock:
            now = self._clock.monotonic()
            with self._cond:
                opened = self._opened
                retry_at = self._retry_at
                next_at = self._next_at
            if not opened:
                if retry_at is not None and now + EPSILON_S < retry_at:
                    return retry_at - now
                if not self._call(self._source.open):
                    self._fail(now, close=False)
                    return RETRY_S
                with self._cond:
                    self._opened = True
                    self._retry_at = None
            elif next_at is not None and now + EPSILON_S < next_at:
                return next_at - now
            frame = self._call(self._source.read)
            if frame is None:
                self._fail(now, close=True)
                return RETRY_S
            period = 1.0 / self.MAX_FPS
            with self._cond:
                self._next_at = now + period
                self._frame = frame
                self._seq += 1
                self._available = True
                self._cond.notify_all()
            return period

    def _fail(self, now: float, *, close: bool) -> None:
        """使えない印にし、1 秒おいて開き直す（8.3）。`_source_lock` を持って呼ぶ。"""
        with self._cond:
            self._opened = False
            self._available = False
            self._frame = None
            self._retry_at = now + RETRY_S
        if close:
            self._call(self._source.close)

    def _close_source(self) -> None:
        with self._source_lock:
            with self._cond:
                opened, self._opened = self._opened, False
                self._frame = None
            if opened:
                self._call(self._source.close)

    def _call(self, fn: Callable[[], _T]) -> _T | None:
        """元の口を呼ぶ。誤りは受けて型の名前だけをログに出し、None を返す。"""
        try:
            return fn()
        except Exception as e:
            self._log(e)
            return None

    def _log(self, error: Exception) -> None:
        now = self._clock.monotonic()
        with self._cond:
            last = self._logged_at
            if last is not None and now - last < LOG_EVERY_S:
                return
            self._logged_at = now
        logger.warning("%sを読めませんでした: %s", _CAMERA_NAMES[self.camera], type(error).__name__)
