"""基盤のイベントの配達（EventBus）のテスト。"""

import dataclasses
import logging
import threading
import time
from collections.abc import Iterator

import pytest

from osekkai.core.events import EventBus, QuietChanged, TimeSynced


@pytest.fixture
def bus() -> Iterator[EventBus]:
    event_bus = EventBus()
    event_bus.start()
    yield event_bus
    event_bus.close()


def test_イベントの型は変えられない値である() -> None:
    assert QuietChanged(quiet=True) == QuietChanged(quiet=True)
    assert TimeSynced() == TimeSynced()
    with pytest.raises(dataclasses.FrozenInstanceError):
        QuietChanged(quiet=True).quiet = False  # type: ignore[misc]


def test_登録した型のイベントだけが登録順に届く(bus: EventBus) -> None:
    received: list[str] = []
    bus.subscribe(QuietChanged, lambda e: received.append(f"a:{e.quiet}"))
    bus.subscribe(TimeSynced, lambda e: received.append("synced"))
    bus.subscribe(QuietChanged, lambda e: received.append(f"b:{e.quiet}"))

    bus.publish(QuietChanged(quiet=True))
    bus.publish(TimeSynced())
    bus.publish(QuietChanged(quiet=False))
    assert bus.wait_idle(timeout=2.0)

    assert received == ["a:True", "b:True", "synced", "a:False", "b:False"]


def test_配達は専用のスレッドで行われる(bus: EventBus) -> None:
    threads: list[threading.Thread] = []
    bus.subscribe(TimeSynced, lambda e: threads.append(threading.current_thread()))
    bus.publish(TimeSynced())
    assert bus.wait_idle(timeout=2.0)
    assert len(threads) == 1
    assert threads[0] is not threading.current_thread()


def test_受け手の例外はログに残り_ほかの受け手と後のイベントに届き続ける(
    bus: EventBus, caplog: pytest.LogCaptureFixture
) -> None:
    received: list[bool] = []

    def broken(event: QuietChanged) -> None:
        raise ValueError("ひみつの中身")

    bus.subscribe(QuietChanged, broken)
    bus.subscribe(QuietChanged, lambda e: received.append(e.quiet))

    with caplog.at_level(logging.ERROR, logger="osekkai.core.events"):
        bus.publish(QuietChanged(quiet=True))
        bus.publish(QuietChanged(quiet=False))
        assert bus.wait_idle(timeout=2.0)

    assert received == [True, False]
    errors = [r for r in caplog.records if r.name == "osekkai.core.events"]
    assert len(errors) == 2
    assert all("ValueError" in r.getMessage() for r in errors)
    # 例外の中身は残さない
    assert all("ひみつ" not in r.getMessage() for r in errors)
    assert all(r.exc_info is None for r in errors)


def test_開始の前に出したイベントは開始の後に届く() -> None:
    event_bus = EventBus()
    received: list[TimeSynced] = []
    event_bus.subscribe(TimeSynced, received.append)
    event_bus.publish(TimeSynced())
    event_bus.start()
    try:
        assert event_bus.wait_idle(timeout=2.0)
        assert received == [TimeSynced()]
    finally:
        event_bus.close()


def test_閉じるとすぐにスレッドが止まり_その後のイベントは捨てる() -> None:
    event_bus = EventBus()
    received: list[TimeSynced] = []
    event_bus.subscribe(TimeSynced, received.append)
    event_bus.start()

    started = time.monotonic()
    event_bus.close(timeout=2.0)
    assert time.monotonic() - started < 1.0
    assert not event_bus.is_running

    event_bus.publish(TimeSynced())
    assert received == []
    event_bus.close()  # 2 回閉じてもよい


def test_配り終えるまで待つ口は_配り終えなければ偽を返す(bus: EventBus) -> None:
    release = threading.Event()

    def slow(event: TimeSynced) -> None:
        release.wait(2.0)

    bus.subscribe(TimeSynced, slow)
    bus.publish(TimeSynced())
    assert bus.wait_idle(timeout=0.05) is False
    release.set()
    assert bus.wait_idle(timeout=2.0) is True


def test_配っている途中に登録しても壊れない(bus: EventBus) -> None:
    received: list[str] = []

    def first(event: QuietChanged) -> None:
        received.append("first")
        bus.subscribe(QuietChanged, lambda e: received.append("late"))

    bus.subscribe(QuietChanged, first)
    bus.publish(QuietChanged(quiet=True))
    assert bus.wait_idle(timeout=2.0)
    bus.publish(QuietChanged(quiet=True))
    assert bus.wait_idle(timeout=2.0)
    assert received == ["first", "first", "late"]
