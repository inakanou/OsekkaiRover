"""時刻の同期の見張り（TimeSyncMonitor）のテスト。"""

import threading
import time
from collections.abc import Iterator

import pytest

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.clock import TimeSyncMonitor
from osekkai.core.events import EventBus, TimeSynced


@pytest.fixture
def bus() -> Iterator[EventBus]:
    event_bus = EventBus()
    event_bus.start()
    yield event_bus
    event_bus.close()


def _collect(bus: EventBus) -> list[TimeSynced]:
    received: list[TimeSynced] = []
    bus.subscribe(TimeSynced, received.append)
    return received


def test_同期を切り替えると_時刻が確かになったことがちょうど1回届く(bus: EventBus) -> None:
    clock = FakeClock(synchronized=False)
    received = _collect(bus)
    monitor = TimeSyncMonitor(clock, bus)

    assert monitor.check_once() is False
    assert monitor.check_once() is False
    assert bus.wait_idle(timeout=2.0)
    assert received == []

    clock.set_synchronized(True)
    assert monitor.check_once() is True
    # 確かめ続けても、もう出さない。同期が外れても確かなまま
    assert monitor.check_once() is True
    clock.set_synchronized(False)
    assert monitor.check_once() is True
    assert monitor.synced is True
    assert bus.wait_idle(timeout=2.0)
    assert received == [TimeSynced()]


def test_確かめる間隔の既定は30秒である() -> None:
    monitor = TimeSyncMonitor(FakeClock(), EventBus())
    assert monitor.interval == 30.0


def test_スレッドで見張り_同期したら1回だけ知らせて終わる(bus: EventBus) -> None:
    clock = FakeClock(synchronized=False)
    delivered = threading.Event()
    received: list[TimeSynced] = []

    def on_synced(event: TimeSynced) -> None:
        received.append(event)
        delivered.set()

    bus.subscribe(TimeSynced, on_synced)
    monitor = TimeSyncMonitor(clock, bus, interval=0.01)
    monitor.start()
    try:
        assert not delivered.wait(0.05)
        clock.set_synchronized(True)
        assert delivered.wait(2.0)
        monitor.join(timeout=2.0)
        assert not monitor.is_running
        assert bus.wait_idle(timeout=2.0)
        assert received == [TimeSynced()]
        # 偽の時計の時刻を進めて空回りしていない
        assert clock.sleeps == []
    finally:
        monitor.stop()


def test_始めから同期していれば_すぐに1回知らせる(bus: EventBus) -> None:
    clock = FakeClock(synchronized=True)
    received = _collect(bus)
    monitor = TimeSyncMonitor(clock, bus)  # 30 秒を待たずに知らせる
    monitor.start()
    try:
        monitor.join(timeout=2.0)
        assert not monitor.is_running
        assert bus.wait_idle(timeout=2.0)
        assert received == [TimeSynced()]
    finally:
        monitor.stop()


def test_止めると30秒の待ちの途中でもすぐに終わる(bus: EventBus) -> None:
    clock = FakeClock(synchronized=False)
    received = _collect(bus)
    monitor = TimeSyncMonitor(clock, bus)
    monitor.start()
    was_running = monitor.is_running

    started = time.monotonic()
    monitor.stop(timeout=2.0)
    assert time.monotonic() - started < 1.0
    assert was_running
    assert not monitor.is_running
    assert bus.wait_idle(timeout=2.0)
    assert received == []
