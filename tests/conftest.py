"""テストで共通に使う準備。"""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.logging_setup import LOGGER_NAME
from osekkai.core.ports import Devices

XDG_VARS = {
    "XDG_CONFIG_HOME": "config",
    "XDG_STATE_HOME": "state",
    "XDG_CACHE_HOME": "cache",
    "XDG_DATA_HOME": "data-home",
    "XDG_RUNTIME_DIR": "runtime",
}


@pytest.fixture(autouse=True)
def _restore_osekkai_logger() -> Iterator[None]:
    """テストが `osekkai` のロガーに付けた手渡し先を、終わったら外して閉じる。

    `main()` を呼ぶテストは、そのときの標準エラー（pytest の受け口）への手渡し先を付ける。
    残すと、受け口が閉じた後のログが失敗し、ほかのテストに案内が混ざる。
    """
    logger = logging.getLogger(LOGGER_NAME)
    handlers = list(logger.handlers)
    level = logger.level
    yield
    for handler in [h for h in logger.handlers if h not in handlers]:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(level)


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
