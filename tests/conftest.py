"""テストで共通に使う準備。"""

from pathlib import Path

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.ports import Devices

XDG_VARS = {
    "XDG_CONFIG_HOME": "config",
    "XDG_STATE_HOME": "state",
    "XDG_CACHE_HOME": "cache",
    "XDG_DATA_HOME": "data-home",
    "XDG_RUNTIME_DIR": "runtime",
}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fakes(clock: FakeClock) -> FakeDeviceSet:
    """型の付いた偽物の一式（`clock` を共有する）。"""
    return FakeDeviceSet.create(clock)


@pytest.fixture
def fake_devices(fakes: FakeDeviceSet) -> Devices:
    """`fakes` と同じ偽物で作ったデバイス一式。"""
    return fakes.to_devices()


@pytest.fixture
def xdg_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """XDG の環境変数を、一時ディレクトリの中に向ける。"""
    dirs: dict[str, Path] = {}
    for name, sub in XDG_VARS.items():
        path = tmp_path / "xdg" / sub
        path.mkdir(parents=True)
        monkeypatch.setenv(name, str(path))
        dirs[name] = path
    return dirs
