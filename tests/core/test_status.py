"""状態のファイル（StatusReporter・ServiceSnapshot）のテスト（device-deployment 2.1〜2.3、6.7）。"""

import json
import logging
import stat
import threading
from pathlib import Path

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Arbiter
from osekkai.core.config import MotionSettings
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.status import (
    STATUS_FILE_NAME,
    STATUS_INTERVAL_S,
    STATUS_THREAD_NAME,
    ServiceSnapshot,
    SnapshotUnreadable,
    StatusReporter,
    build_snapshot,
    read_snapshot,
    snapshot_from_json,
    snapshot_to_json,
)
from osekkai.core.types import Priority, Resource
from shared_fixtures import STATUS_FIXTURES, load_fixture

DRIVING_BUSY = ServiceSnapshot(
    pid=4321,
    version="v0.2.0-3-gabc1234",
    heartbeat=1234.5,
    driving=True,
    busy={"drive": "demo", "display": "demo"},
    unavailable=("servos",),
    features=("demo",),
)
IDLE = ServiceSnapshot(pid=4321, version="v0.2.0", heartbeat=1234.5, driving=False)


def _status_path(tmp_path: Path) -> Path:
    paths = Paths({"XDG_RUNTIME_DIR": str(tmp_path / "run")}, home=tmp_path, uid=1000)
    return paths.runtime_dir / STATUS_FILE_NAME


# --- 状態の型と JSON との行き来 ---


@pytest.mark.parametrize(
    ("name", "snapshot"),
    [("status_driving_busy.json", DRIVING_BUSY), ("status_idle.json", IDLE)],
)
def test_状態の型は共通の見本と行き来できる(name: str, snapshot: ServiceSnapshot) -> None:
    fixture = load_fixture(name)
    assert json.loads(snapshot_to_json(snapshot)) == fixture
    assert snapshot_from_json(json.dumps(fixture)) == snapshot
    assert ServiceSnapshot.from_dict(fixture) == snapshot


def test_見本はすべて読める() -> None:
    for name in STATUS_FIXTURES:
        ServiceSnapshot.from_dict(load_fixture(name))


def _broken(**changes: object) -> dict[str, object]:
    data: dict[str, object] = dict(load_fixture("status_driving_busy.json"))
    data.update(changes)
    return data


@pytest.mark.parametrize(
    "data",
    [
        _broken(schema=2),
        _broken(schema=True),
        _broken(pid="4321"),
        _broken(pid=True),
        _broken(pid=0),
        _broken(version=1),
        _broken(heartbeat="1"),
        _broken(heartbeat=None),
        _broken(driving=1),
        _broken(busy=["drive"]),
        _broken(busy={"drive": 1}),
        _broken(unavailable="servos"),
        _broken(features=[1]),
        _broken(extra=1),
        {k: v for k, v in load_fixture("status_idle.json").items() if k != "driving"},
        [],
        None,
    ],
)
def test_形が違えば読めない(data: object) -> None:
    with pytest.raises(SnapshotUnreadable):
        ServiceSnapshot.from_dict(data)


def test_JSONでなければ読めない() -> None:
    with pytest.raises(SnapshotUnreadable):
        snapshot_from_json("{not json")


def test_心拍は整数でも読める() -> None:
    snapshot = ServiceSnapshot.from_dict(_broken(heartbeat=12))
    assert snapshot.heartbeat == 12.0


def test_ファイルがないか形が違えば読めない(tmp_path: Path) -> None:
    path = tmp_path / STATUS_FILE_NAME
    assert read_snapshot(path) is None
    path.write_text("{}", encoding="utf-8")
    assert read_snapshot(path) is None
    path.write_text(json.dumps(load_fixture("status_idle.json")), encoding="utf-8")
    assert read_snapshot(path) == IDLE


# --- 今の状態から報告を作る ---


def test_報告は走行中と作業中の資源を持ち基盤の内部の持ち主を除く(
    fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    motion = MotionService(fakes.servos, fakes.motors, MotionSettings(), clock)
    arbiter = Arbiter(clock, motion)
    arbiter.acquire("core.idle", Priority.IDLE, [Resource.VOICE])
    arbiter.acquire("demo", Priority.INTERACTIVE, [Resource.DRIVE, Resource.DISPLAY])
    motion.drive(0, 30)

    snapshot = build_snapshot(
        arbiter=arbiter,
        motion=motion,
        version="v0.2.0-3-gabc1234",
        pid=4321,
        clock=clock,
        unavailable=["servos"],
        features=["demo"],
    )
    assert snapshot == ServiceSnapshot(
        pid=4321,
        version="v0.2.0-3-gabc1234",
        heartbeat=clock.monotonic(),
        driving=True,
        busy={"drive": "demo", "display": "demo"},
        unavailable=("servos",),
        features=("demo",),
    )


def test_止まっていて何も持たれていなければ作業中は空(
    fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    motion = MotionService(fakes.servos, fakes.motors, MotionSettings(), clock)
    arbiter = Arbiter(clock, motion)
    arbiter.acquire("core.idle", Priority.IDLE, [Resource.DISPLAY])
    snapshot = build_snapshot(
        arbiter=arbiter,
        motion=motion,
        version="v0.2.0",
        pid=1,
        clock=clock,
        unavailable=[],
        features=[],
    )
    assert snapshot.driving is False
    assert snapshot.busy == {}


# --- StatusReporter ---


def test_書いた中身は共通の見本の形で0600になる(tmp_path: Path) -> None:
    path = _status_path(tmp_path)
    reporter = StatusReporter(path, lambda: DRIVING_BUSY)
    assert reporter.write_once() is True
    assert json.loads(path.read_text(encoding="utf-8")) == load_fixture("status_driving_busy.json")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # 一時ファイルは残らない
    assert [p.name for p in path.parent.iterdir()] == [STATUS_FILE_NAME]


def test_心拍は書くたびに時計の値になる(tmp_path: Path, clock: FakeClock) -> None:
    path = _status_path(tmp_path)

    def snapshot() -> ServiceSnapshot:
        return ServiceSnapshot(pid=1, version="v", heartbeat=clock.monotonic(), driving=False)

    reporter = StatusReporter(path, snapshot)
    reporter.write_once()
    first = read_snapshot(path)
    clock.advance(1.0)
    reporter.write_once()
    second = read_snapshot(path)
    assert first is not None and second is not None
    assert second.heartbeat - first.heartbeat == 1.0


def test_止めるとファイルを消してその後は書かない(tmp_path: Path) -> None:
    path = _status_path(tmp_path)
    reporter = StatusReporter(path, lambda: IDLE)
    reporter.write_once()
    assert path.exists()
    reporter.stop()
    assert not path.exists()
    assert reporter.write_once() is False
    assert not path.exists()


def test_書けないときは最初の1回だけ警告して続ける(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _status_path(tmp_path)
    failing = [True]

    def snapshot() -> ServiceSnapshot:
        if failing[0]:
            raise OSError("書けない")
        return IDLE

    reporter = StatusReporter(path, snapshot)
    with caplog.at_level(logging.DEBUG, logger="osekkai"):
        assert reporter.write_once() is False
        assert reporter.write_once() is False
        assert reporter.write_once() is False
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "OSError" in warnings[0].getMessage()
    assert "書けない" not in caplog.text  # 例外の中身は出さない
    failing[0] = False
    assert reporter.write_once() is True
    assert read_snapshot(path) == IDLE


def test_書く先がディレクトリでも例外を外に出さない(tmp_path: Path) -> None:
    path = _status_path(tmp_path)
    path.mkdir(parents=True)
    reporter = StatusReporter(path, lambda: IDLE)
    assert reporter.write_once() is False
    reporter.stop()  # 消せなくても例外を出さない


def test_スレッドは決まった名前で書き続け止めると消す(tmp_path: Path) -> None:
    path = _status_path(tmp_path)
    calls = 0
    enough = threading.Event()

    def snapshot() -> ServiceSnapshot:
        nonlocal calls
        calls += 1
        if calls >= 3:
            enough.set()
        return IDLE

    reporter = StatusReporter(path, snapshot, interval=0.01)
    reporter.start()
    try:
        # 始めるとすぐに 1 回書く
        assert path.exists()
        assert any(t.name == STATUS_THREAD_NAME for t in threading.enumerate())
        assert enough.wait(timeout=2.0)
    finally:
        reporter.stop()
    assert not reporter.is_running
    assert not path.exists()


def test_既定の間隔は1秒(tmp_path: Path) -> None:
    assert STATUS_INTERVAL_S == 1.0
    assert StatusReporter(tmp_path / "s.json", lambda: IDLE).interval == 1.0
    with pytest.raises(ValueError):
        StatusReporter(tmp_path / "s.json", lambda: IDLE, interval=0)
