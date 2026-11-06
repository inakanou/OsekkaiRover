"""偽のデバイスで Runtime を動かし、状態のファイルと知らせをつなぐ結合テスト。

要件（device-deployment）: 2.1、2.2、6.7、7.1。

テスト用の機能が走行を借りて走り、状態のファイルに走行中と作業中の機能が出ることを確かめる。
停止の最初にファイルが消え、機能が止まる途中を「作業中でない」と見せないことも確かめる。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import ClassVar

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core import Feature, FeatureConfig, FeatureContext, FeatureSpec, Priority, Resource
from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.notice import NOTICE_FILE_NAME
from osekkai.core.paths import Paths, ensure_private_dir
from osekkai.core.registry import FeatureRegistry
from osekkai.core.runtime import Runtime
from osekkai.core.status import STATUS_FILE_NAME, ServiceSnapshot, read_snapshot
from shared_fixtures import load_fixture

WAIT_S = 3.0
TEST_VERSION = "v9.9.9-status"


def _wait_for(condition: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def _row(text: str) -> tuple[int, ...]:
    codes = to_lcd_codes(text)[:16]
    return tuple(codes) + (0x20,) * (16 - len(codes))


class _Probe:
    def __init__(self, status_path: Path) -> None:
        self.status_path = status_path
        self.driving = threading.Event()
        self.file_at_stop: list[bool] = []


def demo_driver(probe: _Probe) -> type[Feature]:
    """走行を借りて走り続ける。止める合図を受けたら、そのときの状態のファイルの有無を残す。"""

    class DemoDriver:
        spec: ClassVar[FeatureSpec] = FeatureSpec(
            name="demo",
            priority=Priority.BACKGROUND,
            resources=frozenset({Resource.DRIVE}),
            config_model=FeatureConfig,
        )

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            self.ctx = ctx

        def run(self, stop: threading.Event) -> None:
            lease = self.ctx.acquire([Resource.DRIVE])
            lease.drive.drive(0.0, 40)
            probe.driving.set()
            stop.wait()
            # まだ走行を持っている間に、状態のファイルはもう消えている
            probe.file_at_stop.append(probe.status_path.exists())
            lease.release()

    return DemoDriver


@pytest.fixture
def paths(xdg_dirs: dict[str, Path]) -> Paths:
    return Paths()


@pytest.fixture
def status_path(paths: Paths) -> Path:
    return paths.runtime_dir / STATUS_FILE_NAME


@pytest.fixture
def runtimes() -> Iterator[list[Runtime]]:
    made: list[Runtime] = []
    yield made
    for runtime in made:
        runtime.shutdown(budget=1.0)


@pytest.fixture
def start_runtime(
    paths: Paths, runtimes: list[Runtime], monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Runtime]:
    monkeypatch.setattr("osekkai.version.get_version", lambda: TEST_VERSION)

    def build(
        fakes: FakeDeviceSet,
        clock: FakeClock,
        features: list[type[Feature]],
        fail: tuple[str, ...] = (),
    ) -> Runtime:
        runtime = Runtime(
            fakes.to_devices(fail), paths, clock, FeatureRegistry(features), stop_budget=1.0
        )
        runtimes.append(runtime)
        assert runtime.start() is None
        return runtime

    return build


def _read_when(path: Path, condition: Callable[[ServiceSnapshot], bool]) -> ServiceSnapshot:
    found: list[ServiceSnapshot] = []

    def ready() -> bool:
        snapshot = read_snapshot(path)
        if snapshot is not None and condition(snapshot):
            found.append(snapshot)
            return True
        return False

    assert _wait_for(ready), read_snapshot(path)
    return found[-1]


def test_走行中と作業中の機能が状態のファイルに出て_停止の最初に消える(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    status_path: Path,
    start_runtime: Callable[..., Runtime],
) -> None:
    probe = _Probe(status_path)
    runtime = start_runtime(fakes, clock, [demo_driver(probe)], fail=("led",))
    assert probe.driving.wait(WAIT_S)

    snapshot = _read_when(status_path, lambda s: s.driving)
    assert snapshot.driving is True
    assert dict(snapshot.busy) == {"drive": "demo"}  # 待機画面（core.idle）は含めない
    assert snapshot.features == ("demo",)
    assert snapshot.unavailable == ("led",)
    assert snapshot.pid == os.getpid()
    assert snapshot.version == TEST_VERSION
    # 道具と同じ見本の形で書く
    written = json.loads(status_path.read_text(encoding="utf-8"))
    assert set(written) == set(load_fixture("status_driving_busy.json"))

    runtime.shutdown(budget=1.0)
    assert not status_path.exists()
    # 機能が止まる途中に、走行を持ったまま「作業中でない」と見せない
    assert probe.file_at_stop == [False]


def test_機能がなければ作業中は空で走行中でない(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    status_path: Path,
    start_runtime: Callable[..., Runtime],
) -> None:
    runtime = start_runtime(fakes, clock, [])
    snapshot = _read_when(status_path, lambda s: True)
    assert snapshot.driving is False
    assert dict(snapshot.busy) == {}
    assert snapshot.features == ()
    assert snapshot.unavailable == ()

    runtime.shutdown(budget=1.0)
    assert not status_path.exists()


def test_置かれた知らせを待機画面がLCDに出す(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    paths: Paths,
    start_runtime: Callable[..., Runtime],
) -> None:
    notice = load_fixture("notice_version.json")
    notice["until"] = clock.now().timestamp() + 20
    ensure_private_dir(paths.runtime_dir)
    (paths.runtime_dir / NOTICE_FILE_NAME).write_text(
        json.dumps(notice, ensure_ascii=False), encoding="utf-8"
    )

    start_runtime(fakes, clock, [])
    expected = [_row(notice["rows"][0]), _row(notice["rows"][1])]
    assert _wait_for(lambda: fakes.display.rows == expected), fakes.display.rows
