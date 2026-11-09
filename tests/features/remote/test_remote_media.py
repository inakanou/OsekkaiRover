"""首と USB のカメラの映像を配る（要件 8.1〜8.5、9.1）。

読み取りは、ふつうはハブごとの補助のスレッドで進む。多くのテストはスレッドを使わず
（`run_in_thread=False`）、読み取りの 1 歩（`_step`）を偽の時計で進めて確かめる。
スレッドの動きは、短い待ちのテストで少しだけ確かめる。
"""

from __future__ import annotations

import builtins
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from types import TracebackType

import pytest
from remote_fakes import FakeClock, FakeFrameSource, FakeUsbCamera

from osekkai.features.presence.api import (
    CameraBusy,
    CameraLoan,
    CameraNotAvailable,
    CameraNotBorrowed,
)
from osekkai.features.remote.media import Camera, FrameHub, NeckSource, UsbSource
from osekkai.features.remote.ports import FrameSource

WAIT_S = 3.0  # スレッドを待つ上限（実際の秒）
A = b"\xff\xd8a\xff\xd9"
B = b"\xff\xd8b\xff\xd9"
C = b"\xff\xd8c\xff\xd9"
STILL = b"\xff\xd8still\xff\xd9"


def _hub(
    source: FrameSource | None = None,
    camera: Camera = Camera.USB,
    clock: FakeClock | None = None,
) -> tuple[FrameHub, FakeClock]:
    clock = clock if clock is not None else FakeClock()
    src = source if source is not None else FakeFrameSource([A, B, C])
    return FrameHub(camera, src, clock, run_in_thread=False), clock


def _opened(source: FakeFrameSource) -> bool:
    """元が開いているか（読み直すたびに今の値を見る）。"""
    return source.is_open


def _closed(camera: FakeUsbCamera) -> bool:
    return camera.closed


def _until(check: Callable[[], bool]) -> bool:
    """実際の時間で WAIT_S まで、check が真になるのを待つ。"""
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.01)
    return check()


# --- 見る端末の数（8.2） ---


def test_見る端末は3つまでで4つめは断る() -> None:
    hub, _ = _hub()
    with hub.viewer() as v1, hub.viewer() as v2, hub.viewer() as v3:
        assert v1 is not None and v2 is not None and v3 is not None
        assert hub.viewers() == 3
        with hub.viewer() as v4:
            assert v4 is None
            assert hub.viewers() == 3
    assert hub.viewers() == 0


def test_席を返すとまた見られる() -> None:
    hub, _ = _hub()
    with hub.viewer(), hub.viewer(), hub.viewer():
        pass
    with hub.viewer() as v:
        assert v is not None


def test_閉じたハブでは見られない() -> None:
    hub, _ = _hub()
    hub.close()
    with hub.viewer() as v:
        assert v is None


# --- 新しい 1 枚だけを渡す（8.1） ---


def test_まだ送っていない新しい1枚だけを渡す() -> None:
    hub, clock = _hub()
    with hub.viewer() as v:
        assert v is not None
        hub._step()
        assert v.next_frame(timeout=0.01) == A
        assert v.next_frame(timeout=0.01) is None  # 同じ 1 枚は 2 度渡さない
        clock.advance(0.1)
        hub._step()
        assert v.next_frame(timeout=0.01) == B


def test_見る端末ごとに送った1枚を覚える() -> None:
    hub, clock = _hub()
    with hub.viewer() as v1, hub.viewer() as v2:
        assert v1 is not None and v2 is not None
        hub._step()
        assert v1.next_frame(timeout=0.01) == A
        clock.advance(0.1)
        hub._step()
        assert v2.next_frame(timeout=0.01) == B  # 遅れた端末は最新の 1 枚だけを受ける
        assert v1.next_frame(timeout=0.01) == B
        assert v2.next_frame(timeout=0.01) is None


def test_最新の1枚だけを持つ() -> None:
    hub, clock = _hub()
    with hub.viewer():
        assert hub.latest() is None
        hub._step()
        clock.advance(0.1)
        hub._step()
        assert hub.latest() == B


# --- 1 秒 10 枚まで（8.1） ---


def test_1秒に10枚より多くは読まない() -> None:
    hub, clock = _hub()
    with hub.viewer():
        hub._step()
        assert hub.latest() == A
        clock.advance(0.05)
        wait = hub._step()
        assert hub.latest() == A  # 0.1 秒たつまで次を読まない
        assert wait is not None and wait == pytest.approx(0.05)
        clock.advance(0.05)
        hub._step()
        assert hub.latest() == B


# --- 3 秒で閉じる（8.4） ---


def test_見る端末がなくなって3秒で閉じる() -> None:
    source = FakeFrameSource([A])
    hub, clock = _hub(source)
    with hub.viewer():
        hub._step()
        assert _opened(source)
    clock.advance(2.9)
    hub.tick()
    hub._step()
    assert _opened(source)  # 3 秒たつまでは閉じない
    clock.advance(0.1)
    hub.tick()
    assert hub._step() is None  # 読み取りを終える
    assert not _opened(source)
    assert source.closes == 1


def test_3秒のうちにまた見に来たら閉じない() -> None:
    source = FakeFrameSource([A])
    hub, clock = _hub(source)
    with hub.viewer():
        hub._step()
    clock.advance(2.0)
    with hub.viewer():
        clock.advance(2.0)
        hub.tick()
        hub._step()
        assert _opened(source)
    clock.advance(2.0)
    hub.tick()
    hub._step()
    assert _opened(source)  # 最後の端末が去ってから数える
    clock.advance(1.0)
    hub.tick()
    hub._step()
    assert not _opened(source)


def test_見る端末がいる間は閉じない() -> None:
    source = FakeFrameSource([A])
    hub, clock = _hub(source)
    with hub.viewer():
        hub._step()
        clock.advance(10.0)
        hub.tick()
        hub._step()
        assert _opened(source)


def test_閉じた後にまた見に来たら開き直す() -> None:
    source = FakeFrameSource([A, B])
    hub, clock = _hub(source)
    with hub.viewer():
        hub._step()
    clock.advance(3.0)
    hub.tick()
    hub._step()
    assert not _opened(source)
    with hub.viewer() as v:
        assert v is not None
        hub._step()
        assert source.opens == 2
        assert v.next_frame(timeout=0.01) == B


# --- 使えない印と試し直し（8.3） ---


def test_開けなければ使えない印にして1秒おいて試し直す() -> None:
    source = FakeFrameSource([A], can_open=False)
    hub, clock = _hub(source)
    assert hub.available()
    with hub.viewer() as v:
        assert v is not None
        assert hub._step() == pytest.approx(1.0)
        assert not hub.available()
        clock.advance(0.5)
        hub._step()
        assert source.opens == 1  # 1 秒たつまでは試さない
        source.can_open = True
        clock.advance(0.5)
        hub._step()
        assert source.opens == 2
        assert hub.available()
        assert v.next_frame(timeout=0.01) == A


def test_読み取りに失敗したら使えない印にして閉じ1秒おいて開き直す() -> None:
    source = FakeFrameSource([A, None, B])
    hub, clock = _hub(source)
    with hub.viewer() as v:
        assert v is not None
        hub._step()
        assert hub.available()
        clock.advance(0.1)
        assert hub._step() == pytest.approx(1.0)
        assert not hub.available()
        assert hub.latest() is None  # 古い 1 枚は配らない
        assert source.closes == 1
        clock.advance(0.9)
        hub._step()
        assert source.opens == 1
        clock.advance(0.1)
        hub._step()
        assert source.opens == 2
        assert hub.available()
        assert v.next_frame(timeout=0.01) == B


class _Broken:
    """読むと例外を投げる元。"""

    def __init__(self) -> None:
        self.closes = 0

    def open(self) -> bool:
        return True

    def read(self) -> bytes | None:
        raise OSError("hidden")

    def still(self) -> bytes | None:
        raise OSError("hidden")

    def close(self) -> None:
        self.closes += 1


def test_元の例外は受けて型の名前だけをまれにログに出す(caplog: pytest.LogCaptureFixture) -> None:
    hub, clock = _hub(_Broken())
    caplog.set_level(logging.DEBUG, logger="osekkai.features.remote.media")
    with hub.viewer():
        hub._step()
        assert not hub.available()
        clock.advance(1.0)
        hub._step()
    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["USB のカメラを読めませんでした: OSError"]  # 続けて失敗しても 1 回だけ
    assert "hidden" not in caplog.text
    assert hub.still() is None


def test_首のカメラの失敗も日本語の決まった文で出す(caplog: pytest.LogCaptureFixture) -> None:
    hub, _ = _hub(_Broken(), Camera.NECK)
    caplog.set_level(logging.DEBUG, logger="osekkai.features.remote.media")
    with hub.viewer():
        hub._step()
    assert [r.getMessage() for r in caplog.records] == ["首のカメラを読めませんでした: OSError"]


def test_片方のカメラが使えなくてももう一方は配れる() -> None:
    clock = FakeClock()
    neck, _ = _hub(FakeFrameSource([None]), Camera.NECK, clock)
    usb, _ = _hub(FakeFrameSource([A]), Camera.USB, clock)
    with neck.viewer(), usb.viewer() as v:
        assert v is not None
        neck._step()
        usb._step()
        assert not neck.available()
        assert usb.available()
        assert v.next_frame(timeout=0.01) == A


# --- 写真の 1 枚（9.1） ---


def test_読んでいないときの写真は開いて撮って閉じる() -> None:
    source = FakeFrameSource([A], still=STILL)
    hub, _ = _hub(source)
    assert hub.still() == STILL
    assert source.opens == 1
    assert not _opened(source)


def test_読んでいる間の写真は開いたままの元から撮る() -> None:
    source = FakeFrameSource([A], still=STILL)
    hub, _ = _hub(source)
    with hub.viewer():
        hub._step()
        assert hub.still() == STILL
        assert source.opens == 1
        assert _opened(source)


def test_開けなければ写真はない() -> None:
    hub, _ = _hub(FakeFrameSource([A], still=STILL, can_open=False))
    assert hub.still() is None


def test_USBの写真は1枚読む() -> None:
    camera = FakeUsbCamera([A, B])
    source = UsbSource(camera)
    hub, _ = _hub(source)
    assert hub.still() == A
    assert _closed(camera)
    assert source.open()
    assert source.read() == B
    assert source.still() == B
    source.close()
    assert _closed(camera)
    assert source.read() is None


# --- 閉じる ---


def test_閉じると元を閉じ何度呼んでもよい() -> None:
    source = FakeFrameSource([A])
    hub, _ = _hub(source)
    with hub.viewer() as v:
        assert v is not None
        hub._step()
        hub.close()
        hub.close()
        assert not _opened(source)
        assert source.closes == 1
        assert v.next_frame(timeout=0.01) is None
    assert hub._step() is None


# --- 首のカメラを借りる・返す ---


class _FakeLoan:
    """偽の借りたカメラ。返した後は CameraNotBorrowed。"""

    def __init__(self, frames: list[bytes] | None = None, error: Exception | None = None) -> None:
        self.frames = frames if frames is not None else [A]
        self.error = error
        self.released = False
        self.frame_args: list[tuple[int, int]] = []

    def motion(self, seconds: float) -> float:
        return 0.0

    def frame(self, max_width: int = 640, quality: int = 70) -> bytes:
        self._check()
        self.frame_args.append((max_width, quality))
        return self.frames[0]

    def still(self) -> bytes:
        self._check()
        return STILL

    def release(self) -> None:
        self.released = True

    def __enter__(self) -> _FakeLoan:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def _check(self) -> None:
        if self.released:
            raise CameraNotBorrowed()
        if self.error is not None:
            raise self.error


class _Borrow:
    """偽の借りる口。借りた回数と、渡された名前と alive を覚える。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.loans: list[_FakeLoan] = []
        self.owners: list[str] = []
        self.alive: Callable[[], bool] | None = None

    def __call__(self, owner: str, alive: Callable[[], bool]) -> CameraLoan:
        self.owners.append(owner)
        self.alive = alive
        if self.error is not None:
            raise self.error
        loan = _FakeLoan()
        self.loans.append(loan)
        return loan


class _Allowed:
    """首のリースを持っているか（切り替えられる）。"""

    def __init__(self, value: bool = True) -> None:
        self.value = value

    def __call__(self) -> bool:
        return self.value


def _neck(
    allowed: bool = True, error: Exception | None = None
) -> tuple[NeckSource, _Borrow, _Allowed]:
    borrow = _Borrow(error)
    may = _Allowed(allowed)
    return NeckSource(may, borrow=borrow), borrow, may


def test_首のリースがなければ借りない() -> None:
    source, borrow, _ = _neck(allowed=False)
    assert not source.open()
    assert borrow.owners == []
    assert source.read() is None


def test_首のリースがあれば借りて小さめの1枚を読む() -> None:
    source, borrow, may = _neck()
    assert source.open()
    assert borrow.owners == ["remote"]
    assert source.read() == A
    assert borrow.loans[0].frame_args == [(480, 70)]
    assert source.still() == STILL
    assert borrow.alive is not None and borrow.alive()
    may.value = False
    assert not borrow.alive()  # リースを失えば presence も貸し出しを終える


@pytest.mark.parametrize("error", [CameraNotAvailable(), CameraBusy()])
def test_借りられなければ開けない(error: Exception) -> None:
    source, _, _ = _neck(error=error)
    assert not source.open()
    assert source.read() is None


@pytest.mark.parametrize(
    "error", [CameraNotAvailable(), CameraBusy(), CameraNotBorrowed(), TimeoutError()]
)
def test_読めない誤りは1枚なしにする(error: Exception) -> None:
    source, borrow, _ = _neck()
    assert source.open()
    borrow.loans[0].error = error
    assert source.read() is None
    assert source.still() is None


def test_閉じると借りたカメラを返す() -> None:
    source, borrow, _ = _neck()
    assert source.open()
    source.close()
    source.close()
    assert borrow.loans[0].released
    assert source.read() is None


def test_リースを失った後の読みは借りたカメラを返す() -> None:
    source, borrow, may = _neck()
    assert source.open()
    may.value = False
    assert source.read() is None
    assert borrow.loans[0].released


def test_返す合図で首のカメラをすぐ返し使えない印にする() -> None:
    source, borrow, may = _neck()
    hub, clock = _hub(source, Camera.NECK)
    with hub.viewer() as v:
        assert v is not None
        hub._step()
        assert v.next_frame(timeout=0.01) == A
        may.value = False
        hub.release_neck()
        assert borrow.loans[0].released  # 読み取りの 1 歩を待たずに返す
        assert not hub.available()
        clock.advance(1.0)
        hub._step()
        assert len(borrow.loans) == 1  # リースがない間は借り直さない
        assert not hub.available()
        may.value = True
        clock.advance(1.0)
        hub._step()
        assert len(borrow.loans) == 2  # 借り直したら、また配る
        assert hub.available()


def test_USBのハブは返す合図で閉じない() -> None:
    source = FakeFrameSource([A])
    hub, _ = _hub(source, Camera.USB)
    with hub.viewer():
        hub._step()
        hub.release_neck()
        assert _opened(source)
        assert hub.available()


def test_3秒で閉じると首のカメラを返す() -> None:
    source, borrow, _ = _neck()
    hub, clock = _hub(source, Camera.NECK)
    with hub.viewer():
        hub._step()
    clock.advance(3.0)
    hub.tick()
    hub._step()
    assert borrow.loans[0].released


# --- 保存しない（8.5） ---


@pytest.fixture
def no_files(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """ファイルを開いたら落とす。"""

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("ファイルを開きました")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(os, "open", refuse)
    yield


@pytest.mark.usefixtures("no_files")
def test_映像と写真をファイルに書かない() -> None:
    source = FakeFrameSource([A, B], still=STILL)
    hub, clock = _hub(source)
    with hub.viewer() as v:
        assert v is not None
        hub._step()
        clock.advance(0.1)
        hub._step()
        assert v.next_frame(timeout=0.01) == B
        assert hub.still() == STILL
    clock.advance(3.0)
    hub.tick()
    hub._step()
    hub.close()


# --- 補助のスレッド ---


def test_読み取りはremoteの名前のdaemonのスレッドで行う() -> None:
    clock = FakeClock()
    source = FakeFrameSource([A, B])
    hub = FrameHub(Camera.NECK, source, clock)
    try:
        assert not any(t.name == "remote-cam-neck" for t in threading.enumerate())
        with hub.viewer() as v:
            assert v is not None
            assert v.next_frame(timeout=WAIT_S) == A
            threads = [t for t in threading.enumerate() if t.name == "remote-cam-neck"]
            assert len(threads) == 1
            assert threads[0].daemon
            clock.advance(0.1)
            assert v.next_frame(timeout=WAIT_S) == B
    finally:
        hub.close()
    assert _until(lambda: not any(t.name == "remote-cam-neck" for t in threading.enumerate()))
    assert not _opened(source)


def test_スレッドは3秒で閉じて終わりまた見に来たら始める() -> None:
    clock = FakeClock()
    source = FakeFrameSource([A, B])
    hub = FrameHub(Camera.USB, source, clock)
    try:
        with hub.viewer() as v:
            assert v is not None
            assert v.next_frame(timeout=WAIT_S) == A
        clock.advance(3.0)
        hub.tick()
        assert _until(lambda: not _opened(source))
        assert _until(lambda: not any(t.name == "remote-cam-usb" for t in threading.enumerate()))
        with hub.viewer() as v:
            assert v is not None
            assert v.next_frame(timeout=WAIT_S) == B
            assert source.opens == 2
    finally:
        hub.close()


def test_スレッドの中の例外で止まらない() -> None:
    clock = FakeClock()
    broken = _Broken()
    hub = FrameHub(Camera.USB, broken, clock)
    try:
        with hub.viewer():
            assert _until(lambda: broken.closes >= 1)
            assert not hub.available()
    finally:
        hub.close()
