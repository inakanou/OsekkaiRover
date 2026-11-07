"""日課のテストの共通の準備の確かめ。"""

from datetime import timedelta
from pathlib import Path

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.storage import StateStore


def test_時計は日本の時刻の平日の朝で始まる(clock: FakeClock) -> None:
    now = clock.now()
    assert now.utcoffset() == timedelta(hours=9)
    assert str(now.tzinfo) == "Asia/Tokyo"
    assert now.weekday() == 0
    assert (now.hour, now.minute) == (6, 0)


def test_保存先は一時のディレクトリの中の日課のもの(
    routine_store: StateStore, xdg_dirs: dict[str, Path]
) -> None:
    assert routine_store.directory.name == "routine"
    assert routine_store.directory.is_relative_to(xdg_dirs["XDG_STATE_HOME"])
    routine_store.save("record", {"a": 1})
    assert routine_store.load("record") == {"a": 1}
