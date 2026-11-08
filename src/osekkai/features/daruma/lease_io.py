"""借りた資源とカメラで遊びの口（GameIO）を動かす（2.5、2.7、3.3、4.6、4.7、5.1、6.2、6.3）。

リースの声・LCD・首・ボタンと、見つけてあいさつから借りたカメラで動かす。
走行（DRIVE）は借りないし、踊りにも走行の口を渡さない。首だけで踊る（5.1）。
映像・静止画は求めない。動きの量はログにも残さない（6.3）。
ログには決まった文言と型の名前だけを出す。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Final, Protocol

import osekkai.features.presence.api as presence_api
from osekkai.core.clock import Clock
from osekkai.core.lcd_text import center
from osekkai.core.types import DeviceUnavailable, LeaseRevoked
from osekkai.features.daruma.camera import BorrowCamera
from osekkai.features.daruma.flow import CameraLost
from osekkai.features.daruma.rules import EndReason

__all__ = ["DANCE_JOIN_S", "OWNER", "REST_STEP_S", "GameLease", "LeaseGameIO"]

logger = logging.getLogger(__name__)

OWNER: Final = "daruma"  # カメラを借りる機能の名前
DANCE_JOIN_S: Final = 3.0  # 話し終えてから踊りを待つ上限
REST_STEP_S: Final = 0.1  # 休む間に中断を見る間隔
_EPSILON_S: Final = 1e-6  # 時計の小数の誤差を待たない


class _Voice(Protocol):
    def say(self, text: str) -> None: ...


class _Display(Protocol):
    def show(self, line1: str, line2: str) -> None: ...


class _Neck(Protocol):
    def look(self, pan: float, tilt: float) -> None: ...
    def home(self) -> None: ...
    def nod(self, times: int = ...) -> None: ...
    def dance(self) -> None: ...


class _Button(Protocol):
    def clear(self) -> None: ...
    def has_press(self) -> bool: ...
    def wait_press(self, timeout: float) -> bool: ...


class GameLease(Protocol):
    """遊びが使うリースの口（基盤の `Lease` がこの形を持つ）。"""

    @property
    def active(self) -> bool: ...
    @property
    def voice(self) -> _Voice: ...
    @property
    def display(self) -> _Display: ...
    @property
    def neck(self) -> _Neck: ...
    @property
    def button(self) -> _Button: ...


class LeaseGameIO:
    """`GameIO` を、借りたリース（VOICE・DISPLAY・BUTTON・NECK）とカメラで動かす。

    `interrupted` は中断・静かな状態・止める旗を見る口（休む間に早く戻るのに使う）。
    `stop` は機能を止める旗。カメラの貸し出しは、止める旗が立つかリースが終わると切れる。
    """

    def __init__(
        self,
        lease: GameLease,
        *,
        borrow: BorrowCamera,
        clock: Clock,
        interrupted: Callable[[], EndReason | None],
        stop: threading.Event,
    ) -> None:
        self._lease = lease
        self._borrow = borrow
        self._clock = clock
        self._interrupted = interrupted
        self._stop = stop
        self._lock = threading.Lock()
        self._loan: presence_api.CameraLoan | None = None

    # 話す・出す・首

    def show(self, line1: str, line2: str) -> None:
        self._lease.display.show(center(line1), center(line2))

    def say(self, text: str) -> None:
        self._lease.voice.say(text)

    def look(self, pan: float, tilt: float) -> None:
        self._lease.neck.look(pan, tilt)

    def home(self) -> None:
        self._lease.neck.home()

    def nod(self) -> None:
        self._lease.neck.nod(times=1)

    def celebrate(self, text: str) -> None:
        """首だけの踊りを短命のスレッドで動かし、このスレッドで話す（3.3、5.1）。

        話し終えたら踊りを待つ（最大 DANCE_JOIN_S 秒）。踊りの誤りは型の名前だけを記録して捨てる。
        話す側の例外はそのまま外に出す。
        """
        neck = self._lease.neck

        def dance() -> None:
            try:
                neck.dance()  # 走行の口は渡さない（5.1）
            except (LeaseRevoked, DeviceUnavailable) as exc:
                logger.info("踊りを止めました: %s", type(exc).__name__)
            except Exception as exc:
                logger.warning("踊れませんでした: %s", type(exc).__name__)

        thread = threading.Thread(target=dance, name="daruma-dance", daemon=True)
        thread.start()
        try:
            self._lease.voice.say(text)
        finally:
            thread.join(DANCE_JOIN_S)
            if thread.is_alive():
                logger.warning("踊りが時間内に終わりませんでした")

    # ボタン

    def clear_presses(self) -> None:
        self._lease.button.clear()

    def pressed(self) -> bool:
        return self._lease.button.has_press()

    def wait_press(self, seconds: float) -> bool:
        return self._lease.button.wait_press(seconds)

    # カメラ

    def open_camera(self) -> bool:
        """カメラを借りる。使えない・使われているなら False（1.4、6.2）。"""
        with self._lock:
            if self._loan is not None:
                return True
            try:
                self._loan = self._borrow(OWNER, self._alive)
            except (presence_api.CameraNotAvailable, presence_api.CameraBusy) as exc:
                logger.info("カメラを借りられませんでした: %s", type(exc).__name__)
                return False
            return True

    def motion(self, seconds: float) -> float:
        """借りたカメラで見た動きの量。どの誤りも CameraLost（4.6）。量は記録しない（6.3）。"""
        with self._lock:
            loan = self._loan
        if loan is None:
            logger.warning("カメラを借りていません")
            raise CameraLost()
        try:
            return loan.motion(seconds)
        except Exception as exc:
            logger.warning("カメラが使えなくなりました: %s", type(exc).__name__)
            raise CameraLost() from exc

    # 待つ・閉じる

    def rest(self, seconds: float) -> None:
        """時計で REST_STEP_S 秒ずつ待つ。中断・静かな状態・止める旗で直ちに戻る。"""
        deadline = self._clock.monotonic() + seconds
        while self._interrupted() is None:
            left = deadline - self._clock.monotonic()
            if left <= _EPSILON_S:
                return
            self._clock.sleep(min(REST_STEP_S, left))

    def close(self) -> None:
        """カメラを返す（何度呼んでもよい）。首を正面に戻すのは流れと基盤（4.7）。"""
        with self._lock:
            loan, self._loan = self._loan, None
        if loan is None:
            return
        try:
            loan.release()
        except Exception as exc:
            logger.warning("カメラを返せませんでした: %s", type(exc).__name__)

    def _alive(self) -> bool:
        """止める旗が立っておらず、リースが有効な間だけ借りる。"""
        return not self._stop.is_set() and self._lease.active
