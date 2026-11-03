from osekkai.adapters.fake.devices import FakeTimeSync
from osekkai.core.clock import Clock, SystemClock


def test_本物の時計はタイムゾーン付きの地域の時刻を返す() -> None:
    clock: Clock = SystemClock(FakeTimeSync())
    now = clock.now()
    assert now.tzinfo is not None
    assert now.utcoffset() is not None


def test_本物の時計の単調な時刻は進み_待つ操作は実際に待つ() -> None:
    clock = SystemClock(FakeTimeSync())
    before = clock.monotonic()
    clock.sleep(0.02)
    assert clock.monotonic() - before >= 0.015


def test_同期の状態はポートに問い合わせ_一度確かになったら保つ() -> None:
    timesync = FakeTimeSync(synchronized=False)
    clock = SystemClock(timesync)
    assert clock.is_synchronized() is False
    timesync.set_synchronized(True)
    assert clock.is_synchronized() is True
    timesync.set_synchronized(False)
    assert clock.is_synchronized() is True


def test_確かになった後はポートに問い合わせない() -> None:
    timesync = FakeTimeSync(synchronized=True)
    clock = SystemClock(timesync)
    assert clock.is_synchronized() is True
    asked = len(timesync.calls)
    clock.is_synchronized()
    assert len(timesync.calls) == asked
