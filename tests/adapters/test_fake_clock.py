import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.clock import Clock

JST = timezone(timedelta(hours=9))


def test_偽の時計は_Clock_として使える() -> None:
    clock: Clock = FakeClock()
    assert clock.now().tzinfo is not None


def test_1時間進めても実際には待たない() -> None:
    clock = FakeClock(start=datetime(2026, 4, 1, 7, 0, tzinfo=JST), monotonic=100.0)
    started = time.monotonic()
    clock.advance(3600)
    assert time.monotonic() - started < 0.5
    assert clock.now() == datetime(2026, 4, 1, 8, 0, tzinfo=JST)
    assert clock.monotonic() == 3700.0


def test_待つ操作は時刻を進めて直ちに戻る() -> None:
    clock = FakeClock(start=datetime(2026, 4, 1, 7, 0, tzinfo=JST))
    before = clock.monotonic()
    started = time.monotonic()
    clock.sleep(3600)
    assert time.monotonic() - started < 0.5
    assert clock.monotonic() - before == 3600
    assert clock.now() == datetime(2026, 4, 1, 8, 0, tzinfo=JST)
    assert clock.sleeps == [3600]


def test_負の秒数は断る() -> None:
    clock = FakeClock()
    with pytest.raises(ValueError):
        clock.advance(-1)
    with pytest.raises(ValueError):
        clock.sleep(-0.1)


def test_タイムゾーンのない開始時刻は断る() -> None:
    with pytest.raises(ValueError):
        FakeClock(start=datetime(2026, 4, 1, 7, 0))


def test_現在時刻だけを変えられる() -> None:
    clock = FakeClock(monotonic=5.0)
    clock.set_now(datetime(2030, 1, 1, tzinfo=JST))
    assert clock.now() == datetime(2030, 1, 1, tzinfo=JST)
    assert clock.monotonic() == 5.0


def test_同期の状態を切り替えられる() -> None:
    clock = FakeClock()
    assert clock.is_synchronized() is True
    clock.set_synchronized(False)
    assert clock.is_synchronized() is False
    clock.set_synchronized(True)
    assert clock.is_synchronized() is True
    assert FakeClock(synchronized=False).is_synchronized() is False


def test_複数のスレッドから進めても失われない() -> None:
    clock = FakeClock(monotonic=0.0)

    def work() -> None:
        for _ in range(1000):
            clock.advance(0.001)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert clock.monotonic() == pytest.approx(4.0)
