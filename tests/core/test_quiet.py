"""静かな状態（QuietMode）のテスト（9.4〜9.6）。"""

import json
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import JST, FakeClock
from osekkai.core.arbiter import Arbiter
from osekkai.core.config import MotionSettings
from osekkai.core.display import DisplayService
from osekkai.core.events import EventBus, QuietChanged
from osekkai.core.idle import IdleScreen, IdleState
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.quiet import QuietMode
from osekkai.core.storage import StateStore

START = datetime(2026, 3, 7, 21, 0, 0, tzinfo=JST)
BLANK = (0x20,) * 16


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def arbiter(fakes: FakeDeviceSet, clock: FakeClock) -> Arbiter:
    motion = MotionService(fakes.servos, fakes.motors, MotionSettings(), clock)
    return Arbiter(clock, motion, display=DisplayService(fakes.display))


@pytest.fixture
def idle(arbiter: Arbiter, clock: FakeClock) -> Iterator[IdleScreen]:
    screen = IdleScreen(arbiter, clock)
    screen.start(run_thread=False)
    yield screen
    screen.stop()


@pytest.fixture
def bus() -> Iterator[EventBus]:
    events = EventBus()
    events.start()
    yield events
    events.close()


@pytest.fixture
def store(xdg_dirs: dict[str, Path], clock: FakeClock) -> StateStore:
    return StateStore.for_feature(Paths(), "core", clock)


@pytest.fixture
def received(bus: EventBus) -> list[bool]:
    got: list[bool] = []
    bus.subscribe(QuietChanged, lambda e: got.append(e.quiet))
    return got


def _state(screen: IdleScreen) -> IdleState:
    return screen.state


def test_始めは静かでない(
    idle: IdleScreen, bus: EventBus, store: StateStore, clock: FakeClock, received: list[bool]
) -> None:
    quiet = QuietMode(idle, bus, store, clock)
    assert not quiet.is_quiet
    assert not quiet.was_quiet_before_restart
    assert _state(idle) is IdleState.SHOWING
    assert bus.wait_idle(2.0)
    assert received == []


def test_入ると画面が消えてバックライトが消えイベントが届く(
    idle: IdleScreen,
    bus: EventBus,
    store: StateStore,
    clock: FakeClock,
    fakes: FakeDeviceSet,
    received: list[bool],
) -> None:
    quiet = QuietMode(idle, bus, store, clock)
    quiet.request_quiet()
    assert quiet.is_quiet
    assert _state(idle) is IdleState.QUIET
    assert fakes.display.rows == [BLANK, BLANK]
    assert fakes.display.backlight is False
    assert bus.wait_idle(2.0)
    assert received == [True]


def test_終えるとバックライトが点いて待機画面に戻りイベントが届く(
    idle: IdleScreen,
    bus: EventBus,
    store: StateStore,
    clock: FakeClock,
    fakes: FakeDeviceSet,
    received: list[bool],
) -> None:
    quiet = QuietMode(idle, bus, store, clock)
    quiet.request_quiet()
    quiet.end_quiet()
    assert not quiet.is_quiet
    assert _state(idle) is IdleState.SHOWING
    assert fakes.display.backlight is True
    assert fakes.display.rows[0] != BLANK
    assert bus.wait_idle(2.0)
    assert received == [True, False]


def test_同じ要求を繰り返してもイベントは重ならない(
    idle: IdleScreen, bus: EventBus, store: StateStore, clock: FakeClock, received: list[bool]
) -> None:
    quiet = QuietMode(idle, bus, store, clock)
    quiet.request_quiet()
    quiet.request_quiet()
    quiet.end_quiet()
    quiet.end_quiet()
    assert bus.wait_idle(2.0)
    assert received == [True, False]


def test_最後の要求が勝つ(
    idle: IdleScreen, bus: EventBus, store: StateStore, clock: FakeClock
) -> None:
    quiet = QuietMode(idle, bus, store, clock)
    # 2 つの機能が入るのを求めても、1 回の終わりで終わる（数えない）
    quiet.request_quiet()
    quiet.request_quiet()
    quiet.end_quiet()
    assert not quiet.is_quiet


def test_変わるたびに時刻とともに保存する(
    idle: IdleScreen, bus: EventBus, store: StateStore, clock: FakeClock
) -> None:
    quiet = QuietMode(idle, bus, store, clock)
    quiet.request_quiet()
    assert store.load("quiet") == {"quiet": True, "changed_at": START.isoformat()}
    clock.advance(60)
    quiet.end_quiet()
    data = store.load("quiet")
    assert data is not None
    assert data["quiet"] is False
    assert data["changed_at"] == clock.now().isoformat()
    path = store.directory / "quiet.json"
    assert json.loads(path.read_text(encoding="utf-8"))["quiet"] is False


def test_保存の後に作り直すと再起動の前に静かだったが真で今は静かでない(
    idle: IdleScreen, bus: EventBus, store: StateStore, clock: FakeClock, received: list[bool]
) -> None:
    QuietMode(idle, bus, store, clock).request_quiet()
    idle.set_quiet(False)  # 再起動で画面は作り直される
    restarted = QuietMode(idle, bus, store, clock)
    assert restarted.was_quiet_before_restart
    assert not restarted.is_quiet
    assert _state(idle) is IdleState.SHOWING
    assert bus.wait_idle(2.0)
    assert received == [True]  # 起動ではイベントを出さない


def test_続けて再起動しても再起動の前に静かだったが消えない(
    idle: IdleScreen, bus: EventBus, store: StateStore, clock: FakeClock
) -> None:
    QuietMode(idle, bus, store, clock).request_quiet()
    # 機能が静かに戻す前に、もう一度再起動した（停電が続いたなど）
    assert QuietMode(idle, bus, store, clock).was_quiet_before_restart
    assert QuietMode(idle, bus, store, clock).was_quiet_before_restart


def test_保存がなければ起動でファイルを作らない(
    idle: IdleScreen, bus: EventBus, store: StateStore, clock: FakeClock
) -> None:
    QuietMode(idle, bus, store, clock)
    assert not (store.directory / "quiet.json").exists()


def test_起動は前の保存を書き換えない(
    idle: IdleScreen, bus: EventBus, store: StateStore, clock: FakeClock
) -> None:
    QuietMode(idle, bus, store, clock).request_quiet()
    path = store.directory / "quiet.json"
    before = path.read_bytes()
    clock.advance(3600)
    QuietMode(idle, bus, store, clock)
    assert path.read_bytes() == before


def test_終えてから再起動すると偽になる(
    idle: IdleScreen, bus: EventBus, store: StateStore, clock: FakeClock
) -> None:
    quiet = QuietMode(idle, bus, store, clock)
    quiet.request_quiet()
    quiet.end_quiet()
    assert not QuietMode(idle, bus, store, clock).was_quiet_before_restart


@pytest.mark.parametrize(
    "content",
    ["{壊れている", '{"quiet": "yes"}', '{"other": 1}', "[true]"],
)
def test_壊れたか形の違う保存は偽として読む(
    idle: IdleScreen, bus: EventBus, store: StateStore, clock: FakeClock, content: str
) -> None:
    store.directory.mkdir(parents=True, exist_ok=True)
    (store.directory / "quiet.json").write_text(content, encoding="utf-8")
    assert not QuietMode(idle, bus, store, clock).was_quiet_before_restart


def test_読めないときは偽として扱いログに残す(
    idle: IdleScreen,
    bus: EventBus,
    store: StateStore,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(key: str) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(store, "load", broken)
    with caplog.at_level(logging.WARNING, logger="osekkai.core.quiet"):
        quiet = QuietMode(idle, bus, store, clock)
    assert not quiet.was_quiet_before_restart
    assert not quiet.is_quiet
    assert "OSError" in caplog.text


def test_保存に失敗しても切り替わりイベントが届く(
    idle: IdleScreen,
    bus: EventBus,
    store: StateStore,
    clock: FakeClock,
    fakes: FakeDeviceSet,
    received: list[bool],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    quiet = QuietMode(idle, bus, store, clock)

    def broken(key: str, data: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "save", broken)
    with caplog.at_level(logging.WARNING, logger="osekkai.core.quiet"):
        quiet.request_quiet()
    assert quiet.is_quiet
    assert fakes.display.backlight is False
    assert bus.wait_idle(2.0)
    assert received == [True]
    assert "OSError" in caplog.text


def test_起動では保存しない(
    idle: IdleScreen,
    bus: EventBus,
    store: StateStore,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    QuietMode(idle, bus, store, clock).request_quiet()
    calls: list[str] = []
    monkeypatch.setattr(store, "save", lambda key, data: calls.append(key))
    quiet = QuietMode(idle, bus, store, clock)
    assert quiet.was_quiet_before_restart
    assert not quiet.is_quiet
    assert calls == []
