"""リースから得る操作の口（4.9、4.4、4.6、1.6）。

口は `Lease` の性質（`lease.voice` など）からだけ作る。操作のたびにリースが有効かを
確かめ、無効なら `LeaseRevoked` を投げる。確かめるときはロックを取らない
（段階のある動きの「続けてよいか」は MotionService のロックの中で呼ばれるため）。

- 走行の口は、中断を知らされた後の `drive()` も断る。`stop()` は、中断を知らされた後も
  リースが終わるまでは使える（止めるのは安全なので）
- 段階のある動き（うなずき・見回し・ダンス）は、段階ごとに有効かと中断の知らせを確かめる。
  中止（`MotionAborted`）は `LeaseRevoked` に変える。中止のときは首を戻さない
- 声の口は、発話のロックを待つ前と、ロックを取った直後（話し始める前）に確かめる。
  話し始めた後は、取り上げられても発話の途中では止めない（4.6）
- ボタンの口は、リースの押下の待ち行列を読む。待つ間にリースが終わったら `LeaseRevoked`
- サービスを渡されていない資源は、操作すると `DeviceUnavailable` を投げる（1.6）。
  サービスの `DeviceUnavailable` と `SpeechFailed` は、そのまま外に出す
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

from osekkai.core.types import DeviceUnavailable, LeaseRevoked, MotionAborted

if TYPE_CHECKING:
    from osekkai.core.arbiter import Lease
    from osekkai.core.buttons import ButtonRouter
    from osekkai.core.display import DisplayService, LedService
    from osekkai.core.motion import MotionService
    from osekkai.core.voice import VoiceService

_S = TypeVar("_S")
_Go = Callable[[], bool]
_Run = Callable[[_Go], None]


def _check(lease: Lease) -> None:
    if not lease.active:
        raise LeaseRevoked(lease.owner)


def _service(service: _S | None, device: str) -> _S:
    if service is None:
        raise DeviceUnavailable(device)
    return service


def _may_move(lease: Lease) -> bool:
    """段階のある動きと走行を続けてよいか。ロックを取らない。"""
    return lease.active and not lease.preempt_requested.is_set()


class VoiceHandle:
    def __init__(self, lease: Lease, service: VoiceService | None) -> None:
        self._lease = lease
        self._service = service

    def say(self, text: str, *, keep: bool = True) -> None:
        """話し終わるまで戻らない。失敗は `SpeechFailed`（6.6）。"""
        lease = self._lease
        _check(lease)
        # ほかの発話の終わりを待つ間に取り上げられたら、話さない（4.9）
        spoke = _service(self._service, "voice").speak(
            text, keep=keep, should_continue=lambda: lease.active
        )
        if not spoke:
            raise LeaseRevoked(lease.owner)


class DisplayHandle:
    def __init__(self, lease: Lease, service: DisplayService | None) -> None:
        self._lease = lease
        self._service = service

    def show(self, line1: str, line2: str = "") -> None:
        _check(self._lease)
        _service(self._service, "display").show(line1, line2)

    def set_backlight(self, on: bool) -> None:
        _check(self._lease)
        _service(self._service, "display").set_backlight(on)


class LedHandle:
    def __init__(self, lease: Lease, service: LedService | None) -> None:
        self._lease = lease
        self._service = service

    def set(self, on: bool) -> None:
        _check(self._lease)
        _service(self._service, "led").set(on)


class ButtonHandle:
    def __init__(self, lease: Lease, router: ButtonRouter | None) -> None:
        self._lease = lease
        self._router = router

    def clear(self) -> None:
        """それまでの押下を忘れる。"""
        _check(self._lease)
        _service(self._router, "buttons").clear(self._lease)

    def wait_press(self, timeout: float | None) -> bool:
        """押下を 1 つ消費する（5.1）。時間切れなら False。取り上げられたら LeaseRevoked。"""
        lease = self._lease
        _check(lease)
        got = _service(self._router, "buttons").wait_press(lease, timeout)
        if got is None:
            raise LeaseRevoked(lease.owner)
        return got

    def has_press(self) -> bool:
        _check(self._lease)
        return _service(self._router, "buttons").has_press(self._lease)


class DriveHandle:
    def __init__(self, lease: Lease, motion: MotionService) -> None:
        self._lease = lease
        self._motion = motion

    def drive(self, steering: float, speed: int) -> None:
        """度（±30）、速さ −100〜100。中断を知らされた後は断る。"""
        lease = self._lease
        if not _may_move(lease):
            raise LeaseRevoked(lease.owner)
        self._motion.drive(steering, speed)
        if not _may_move(lease):
            # 確かめてから動かすまでの間に中断が来たら、動かしたモーターを止める
            self._motion.emergency_stop()
            raise LeaseRevoked(lease.owner)

    def stop(self) -> None:
        _check(self._lease)
        self._motion.stop()

    def _may_move(self) -> bool:
        return _may_move(self._lease)


class NeckHandle:
    def __init__(self, lease: Lease, motion: MotionService) -> None:
        self._lease = lease
        self._motion = motion

    def look(self, pan: float, tilt: float) -> None:
        _check(self._lease)
        self._motion.look(pan, tilt)

    def home(self) -> None:
        """首だけを正面に戻す（走行のステアリングには触らない）。"""
        _check(self._lease)
        self._motion.home_neck()

    def sleepy(self) -> None:
        _check(self._lease)
        self._motion.sleepy()

    def nod(self, times: int = 2) -> None:
        self._staged(lambda go: self._motion.nod(times, go))

    def look_around(self) -> None:
        self._staged(self._motion.look_around)

    def dance(self, drive: DriveHandle | None = None) -> None:
        """タイヤは、有効な走行の口を渡されたときだけ回す（設定の許可も要る、8.3）。"""
        lease = self._lease
        wheels = drive is not None and drive._may_move()

        def go() -> bool:
            if not _may_move(lease):
                return False
            # タイヤを回すなら、走行のリースも続けて有効でなければならない
            return not wheels or (drive is not None and drive._may_move())

        self._staged(lambda ok: self._motion.dance(wheels=wheels, should_continue=ok), go)

    def _staged(self, run: _Run, go: _Go | None = None) -> None:
        lease = self._lease
        if not _may_move(lease):
            raise LeaseRevoked(lease.owner)

        try:
            run(go if go is not None else lambda: _may_move(lease))
        except MotionAborted as error:
            raise LeaseRevoked(lease.owner) from error
