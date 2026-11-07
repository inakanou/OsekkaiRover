"""ボタンの押下の配達（ButtonRouter と ButtonHandle）のテスト（5.1〜5.4）。"""

import logging
import threading
import time
from collections.abc import Iterator

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Arbiter
from osekkai.core.buttons import ButtonRouter
from osekkai.core.config import MotionSettings
from osekkai.core.motion import MotionService
from osekkai.core.ports import Unavailable
from osekkai.core.types import ButtonPress, DeviceUnavailable, LeaseRevoked, Resource

B = Resource.BUTTON


@pytest.fixture
def router(fakes: FakeDeviceSet, clock: FakeClock) -> Iterator[ButtonRouter]:
    r = ButtonRouter(fakes.buttons, clock)
    r.start()
    yield r
    r.close()


@pytest.fixture
def arbiter(fakes: FakeDeviceSet, clock: FakeClock, router: ButtonRouter) -> Arbiter:
    motion = MotionService(fakes.servos, fakes.motors, MotionSettings(), clock)
    return Arbiter(clock, motion, buttons=router)


def _press(fakes: FakeDeviceSet, clock: FakeClock, source: str = "usr") -> None:
    # チャタリングの除去に当たらないよう、押すたびに時刻を進める
    clock.advance(0.2)
    fakes.buttons.press(source)  # type: ignore[arg-type]


# --- 5.1 持ち主に 1 回だけ ---


def test_押下はボタンの持ち主に1回だけ届く(
    arbiter: Arbiter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    lease = arbiter.acquire("talk", 50, [B])
    handle = lease.button
    assert not handle.has_press()
    _press(fakes, clock)
    assert handle.has_press()
    assert handle.wait_press(0.5) is True
    assert handle.has_press() is False
    assert handle.wait_press(0.05) is False


def test_持ち主がいれば待ち手には届かない(
    arbiter: Arbiter, router: ButtonRouter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    got: list[ButtonPress] = []
    router.add_waiter("other", 80, got.append)
    lease = arbiter.acquire("talk", 50, [B])
    _press(fakes, clock)
    assert got == []
    assert lease.button.wait_press(0.5)


def test_押下の数だけ消費できる(arbiter: Arbiter, fakes: FakeDeviceSet, clock: FakeClock) -> None:
    lease = arbiter.acquire("talk", 50, [B])
    _press(fakes, clock)
    _press(fakes, clock, "extra")
    handle = lease.button
    assert handle.wait_press(0.5)
    assert handle.wait_press(0.5)
    assert not handle.wait_press(0.05)


def test_clearでそれまでの押下を忘れる(
    arbiter: Arbiter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    lease = arbiter.acquire("talk", 50, [B])
    _press(fakes, clock)
    lease.button.clear()
    assert not lease.button.has_press()
    assert not lease.button.wait_press(0.05)


def test_待っている間の押下で起きる(
    arbiter: Arbiter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    lease = arbiter.acquire("talk", 50, [B])
    result: list[bool] = []
    t = threading.Thread(target=lambda: result.append(lease.button.wait_press(None)))
    t.start()
    time.sleep(0.05)
    _press(fakes, clock)
    t.join(2.0)
    assert result == [True]


def test_待ちは時間切れでFalse(arbiter: Arbiter) -> None:
    lease = arbiter.acquire("talk", 50, [B])
    start = time.monotonic()
    assert lease.button.wait_press(0.1) is False
    assert time.monotonic() - start >= 0.09


def test_取り上げられると待ちがLeaseRevokedで起きる(arbiter: Arbiter) -> None:
    lease = arbiter.acquire("talk", 50, [B])
    errors: list[BaseException] = []

    def run() -> None:
        try:
            lease.button.wait_press(None)
        except LeaseRevoked as error:
            errors.append(error)

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.05)
    arbiter.revoke_all("talk")
    t.join(2.0)
    assert not t.is_alive()
    assert len(errors) == 1


def test_終わったリースの口はLeaseRevoked(arbiter: Arbiter) -> None:
    lease = arbiter.acquire("talk", 50, [B])
    handle = lease.button
    lease.release()
    with pytest.raises(LeaseRevoked):
        handle.has_press()
    with pytest.raises(LeaseRevoked):
        handle.clear()
    with pytest.raises(LeaseRevoked):
        handle.wait_press(0.0)


def test_借りていないボタンの口はValueError(arbiter: Arbiter) -> None:
    lease = arbiter.acquire("talk", 50, [Resource.LED])
    with pytest.raises(ValueError):
        _ = lease.button


def test_配達の口がなければDeviceUnavailable(fakes: FakeDeviceSet, clock: FakeClock) -> None:
    motion = MotionService(fakes.servos, fakes.motors, MotionSettings(), clock)
    arbiter = Arbiter(clock, motion)
    lease = arbiter.acquire("talk", 50, [B])
    with pytest.raises(DeviceUnavailable):
        lease.button.has_press()


# --- 5.2 待ち手の最高の優先度に ---


def test_持ち主がいなければ最も高い待ち手に届く(
    router: ButtonRouter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    low: list[ButtonPress] = []
    high: list[ButtonPress] = []
    router.add_waiter("low", 10, low.append)
    router.add_waiter("high", 50, high.append)
    _press(fakes, clock, "usr")
    assert low == []
    assert len(high) == 1
    assert high[0].source == "usr"
    assert high[0].at == clock.monotonic()


def test_同じ優先度なら先に登録した方に届く(
    router: ButtonRouter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    first: list[ButtonPress] = []
    second: list[ButtonPress] = []
    router.add_waiter("a", 50, first.append)
    router.add_waiter("b", 50, second.append)
    _press(fakes, clock)
    assert len(first) == 1
    assert second == []


def test_登録を解くと次の待ち手に届く(
    router: ButtonRouter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    high: list[ButtonPress] = []
    low: list[ButtonPress] = []
    unregister = router.add_waiter("high", 50, high.append)
    router.add_waiter("low", 10, low.append)
    unregister()
    unregister()  # 何度呼んでもよい
    _press(fakes, clock)
    assert high == []
    assert len(low) == 1


def test_持ち主ごとに待ち手をすべて外せる(
    router: ButtonRouter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    high: list[ButtonPress] = []
    low: list[ButtonPress] = []
    router.add_waiter("high", 50, high.append)
    router.add_waiter("high", 60, high.append)
    unregister = router.add_waiter("low", 10, low.append)
    router.remove_waiters("high")
    router.remove_waiters("high")  # 何度呼んでもよい
    router.remove_waiters("none")  # 知らない持ち主でもよい
    _press(fakes, clock)
    assert high == []
    assert len(low) == 1
    unregister()  # 外した後に解いてもよい
    _press(fakes, clock)
    assert len(low) == 1


def test_待ち手の例外で配達が止まらない(
    router: ButtonRouter,
    fakes: FakeDeviceSet,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[ButtonPress] = []

    def bad(press: ButtonPress) -> None:
        calls.append(press)
        raise RuntimeError("hidden input")

    router.add_waiter("bad", 50, bad)
    with caplog.at_level(logging.WARNING):
        _press(fakes, clock)
        _press(fakes, clock)
    assert len(calls) == 2
    assert "RuntimeError" in caplog.text
    assert "hidden input" not in caplog.text


# --- 5.3 誰もいなければ捨て、持ち越さない ---


def test_受け手がいなければ捨てて次の持ち主に持ち越さない(
    arbiter: Arbiter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    _press(fakes, clock)
    lease = arbiter.acquire("talk", 50, [B])
    assert not lease.button.has_press()
    assert not lease.button.wait_press(0.05)


def test_リースが終わったら待ち行列を捨てる(
    arbiter: Arbiter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    first = arbiter.acquire("talk", 50, [B])
    _press(fakes, clock)
    first.release()
    second = arbiter.acquire("quiz", 50, [B])
    assert not second.button.has_press()
    assert not second.button.wait_press(0.05)


def test_リースが終わった後の押下は待ち手に届く(
    arbiter: Arbiter, router: ButtonRouter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    got: list[ButtonPress] = []
    router.add_waiter("w", 10, got.append)
    lease = arbiter.acquire("talk", 50, [B])
    lease.release()
    _press(fakes, clock)
    assert len(got) == 1


# --- 5.4 チャタリング ---


def test_50ms以内の重複は捨てられる(
    router: ButtonRouter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    got: list[ButtonPress] = []
    router.add_waiter("w", 50, got.append)
    fakes.buttons.press("usr")
    clock.advance(0.03)
    fakes.buttons.press("usr")
    assert len(got) == 1
    clock.advance(0.06)
    fakes.buttons.press("usr")
    assert len(got) == 2


def test_USRと追加のボタンはどちらも押下になる(
    router: ButtonRouter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    got: list[ButtonPress] = []
    router.add_waiter("w", 50, got.append)
    _press(fakes, clock, "usr")
    _press(fakes, clock, "extra")
    assert [p.source for p in got] == ["usr", "extra"]


def test_知らない名前のボタンは捨てる(
    router: ButtonRouter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    got: list[ButtonPress] = []
    router.add_waiter("w", 50, got.append)
    _press(fakes, clock, "other")
    assert got == []


# --- 開始と終わり ---


def test_使えないボタンでも開始できる(clock: FakeClock) -> None:
    r = ButtonRouter(Unavailable("buttons", "no gpio"), clock)
    r.start()
    r.close()


def test_closeの後は知らせない(fakes: FakeDeviceSet, clock: FakeClock) -> None:
    r = ButtonRouter(fakes.buttons, clock)
    got: list[ButtonPress] = []
    r.add_waiter("w", 50, got.append)
    r.start()
    r.close()
    _press(fakes, clock)
    assert got == []
