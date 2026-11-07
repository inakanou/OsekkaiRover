"""日課のテストで共通に使う準備。本体は routine_fixtures と routine_kit にある。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from routine_fixtures import make_clock, make_store
from routine_kit import Kit

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.paths import Paths
from osekkai.core.storage import StateStore
from osekkai.features.routine import api


@pytest.fixture
def clock() -> FakeClock:
    """日本の時刻（Asia/Tokyo）の平日の朝で始まる偽の時計。`fakes` もこの時計を使う。"""
    return make_clock()


@pytest.fixture
def routine_store(xdg_dirs: dict[str, Path], clock: FakeClock) -> StateStore:
    """一時のディレクトリの中にある、日課の機能の保存先。"""
    return make_store(Paths(), clock)


@pytest.fixture
def kit(xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock) -> Iterator[Kit]:
    """日課の機能を動かす基盤の部品一式。終わったら状況の口を消して片付ける。"""
    k = Kit(fakes, clock)
    k.router.start()
    k.bus.start()
    k.idle.start(run_thread=False)
    yield k
    api._publish(None)
    k.idle.stop()
    k.bus.close()
    k.router.close()
