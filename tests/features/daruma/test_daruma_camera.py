"""だるまさんのカメラを借りる口と、PC の道具用の偽のカメラ。

要件 1.4、6.2、8.4。
見つけてあいさつの口（`presence.api.borrow_camera`）は、このファイルの中の偽物に差し替えて確かめる。
"""

from collections.abc import Callable
from types import TracebackType

import pytest

from osekkai.adapters.fake.clock import FakeClock
from osekkai.features.daruma.camera import BorrowCamera, ScriptedCamera, borrow_from_presence
from osekkai.features.presence import api
from osekkai.features.presence.api import (
    CameraBusy,
    CameraLoan,
    CameraNotAvailable,
    CameraNotBorrowed,
)

OWNER = "daruma"


class _Loan:
    """見つけてあいさつが貸す、借りたカメラの偽物。"""

    def __init__(self) -> None:
        self.released = 0

    def motion(self, seconds: float) -> float:
        return 0.5

    def frame(self, max_width: int = 640, quality: int = 70) -> bytes:
        return b""

    def still(self) -> bytes:
        return b""

    def release(self) -> None:
        self.released += 1

    def __enter__(self) -> "_Loan":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


class _Presence:
    """見つけてあいさつの `borrow_camera` の偽物。呼ばれた借り手と alive を残す。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.loan = _Loan()
        self.calls: list[tuple[str, Callable[[], bool]]] = []

    def borrow_camera(self, owner: str, *, alive: Callable[[], bool]) -> CameraLoan:
        self.calls.append((owner, alive))
        if self.error is not None:
            raise self.error
        return self.loan


@pytest.fixture
def presence(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Presence]:
    def install(*, error: Exception | None = None) -> _Presence:
        fake = _Presence(error=error)
        monkeypatch.setattr(api, "borrow_camera", fake.borrow_camera)
        return fake

    return install


# --- 見つけてあいさつから借りる（1.4、6.2） ---


def test_borrow_from_presence_passes_owner_and_alive(presence: Callable[..., _Presence]) -> None:
    fake = presence()

    def alive() -> bool:
        return True

    loan = borrow_from_presence(OWNER, alive)

    assert loan is fake.loan
    assert fake.calls == [(OWNER, alive)]


def test_borrow_from_presence_matches_borrow_type(presence: Callable[..., _Presence]) -> None:
    presence()
    borrow: BorrowCamera = borrow_from_presence

    assert isinstance(borrow(OWNER, lambda: True), CameraLoan)


def test_borrow_from_presence_passes_unavailable_through(
    presence: Callable[..., _Presence],
) -> None:
    error = CameraNotAvailable()
    presence(error=error)

    with pytest.raises(CameraNotAvailable) as raised:
        borrow_from_presence(OWNER, lambda: True)

    assert raised.value is error


def test_borrow_from_presence_passes_busy_through(presence: Callable[..., _Presence]) -> None:
    error = CameraBusy()
    presence(error=error)

    with pytest.raises(CameraBusy) as raised:
        borrow_from_presence(OWNER, lambda: True)

    assert raised.value is error


def test_borrow_from_presence_without_running_presence_is_unavailable() -> None:
    # 見つけてあいさつが動いていない（見る流れが置かれていない）なら、本物の口が断る
    assert api.running_watcher() is None

    with pytest.raises(CameraNotAvailable):
        borrow_from_presence(OWNER, lambda: True)


def test_borrowed_loan_is_returned_by_release(presence: Callable[..., _Presence]) -> None:
    fake = presence()

    with borrow_from_presence(OWNER, lambda: True) as loan:
        loan.motion(1.0)

    assert fake.loan.released == 1


# --- 偽のカメラ（8.4） ---


def test_scripted_camera_returns_values_in_order_and_repeats() -> None:
    clock = FakeClock()
    loan = ScriptedCamera(clock, (0.1, 0.2, 0.3)).borrow(OWNER, lambda: True)

    amounts = [loan.motion(1.0) for _ in range(5)]

    assert amounts == [0.1, 0.2, 0.3, 0.1, 0.2]


def test_scripted_camera_waits_the_seconds_on_the_clock() -> None:
    clock = FakeClock()
    start = clock.monotonic()
    loan = ScriptedCamera(clock, (0.0,)).borrow(OWNER, lambda: True)

    loan.motion(1.5)
    loan.motion(0.5)

    assert clock.sleeps == [1.5, 0.5]
    assert clock.monotonic() == start + 2.0


def test_scripted_camera_default_values_cross_the_threshold() -> None:
    # 既定の列は、しきい値 0.01 の下と上の両方を出す
    loan = ScriptedCamera(FakeClock()).borrow(OWNER, lambda: True)

    amounts = [loan.motion(1.0) for _ in range(3)]

    assert amounts == [0.0, 0.004, 0.03]
    assert any(amount <= 0.01 for amount in amounts)
    assert any(amount > 0.01 for amount in amounts)


def test_scripted_camera_matches_borrow_type_and_loan_shape() -> None:
    borrow: BorrowCamera = ScriptedCamera(FakeClock()).borrow

    loan = borrow(OWNER, lambda: True)

    assert isinstance(loan, CameraLoan)


def test_scripted_camera_does_not_give_frames_or_stills() -> None:
    loan = ScriptedCamera(FakeClock()).borrow(OWNER, lambda: True)

    with pytest.raises(CameraNotAvailable):
        loan.frame()
    with pytest.raises(CameraNotAvailable):
        loan.still()


def test_scripted_camera_refuses_motion_after_release() -> None:
    loan = ScriptedCamera(FakeClock()).borrow(OWNER, lambda: True)
    loan.release()
    loan.release()  # 2 回呼んでもよい

    with pytest.raises(CameraNotBorrowed):
        loan.motion(1.0)


def test_scripted_camera_releases_with_context_manager() -> None:
    camera = ScriptedCamera(FakeClock(), (0.2,))

    with camera.borrow(OWNER, lambda: True) as loan:
        assert loan.motion(1.0) == 0.2

    with pytest.raises(CameraNotBorrowed):
        loan.motion(1.0)


def test_scripted_camera_refuses_motion_when_borrower_stopped() -> None:
    flag = [True]
    loan = ScriptedCamera(FakeClock()).borrow(OWNER, lambda: flag[0])
    flag[0] = False

    with pytest.raises(CameraNotBorrowed):
        loan.motion(1.0)


def test_scripted_camera_rejects_empty_script() -> None:
    with pytest.raises(ValueError):
        ScriptedCamera(FakeClock(), ())
