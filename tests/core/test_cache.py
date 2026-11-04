"""キャッシュの合計の上限（12.1、12.6）。"""

import os
import stat
from pathlib import Path

import pytest

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.cache import CacheManager
from osekkai.core.paths import Paths


@pytest.fixture
def paths(xdg_dirs: dict[str, Path]) -> Paths:
    return Paths()


def _write(cache: CacheManager, path: Path, size: int, clock: FakeClock) -> Path:
    path.write_bytes(b"x" * size)
    cache.touch(path)
    clock.advance(1)
    return path


def test_持ち主ごとの置き場所を0700で作る(paths: Paths, clock: FakeClock) -> None:
    cache = CacheManager(paths.cache_dir, max_bytes=1000, clock=clock)
    directory = cache.dir_for("core")
    assert directory == paths.cache_dir / "core"
    assert directory.is_dir()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.cache_dir.stat().st_mode) == 0o700


def test_持ち主の名前がパスの外に出るなら断る(paths: Paths, clock: FakeClock) -> None:
    cache = CacheManager(paths.cache_dir, max_bytes=1000, clock=clock)
    with pytest.raises(ValueError):
        cache.dir_for("../evil")


def test_リポジトリの外の利用者のディレクトリに置く(
    paths: Paths, clock: FakeClock, xdg_dirs: dict[str, Path]
) -> None:
    cache = CacheManager.from_paths(paths, max_mb=200, clock=clock)
    assert cache.root == xdg_dirs["XDG_CACHE_HOME"] / "osekkai"
    assert cache.max_bytes == 200 * 1024 * 1024


def test_touchは最後に使った時刻を時計の時刻にする(paths: Paths, clock: FakeClock) -> None:
    cache = CacheManager(paths.cache_dir, max_bytes=1000, clock=clock)
    path = cache.dir_for("core") / "a.wav"
    path.write_bytes(b"x")
    cache.touch(path)
    assert path.stat().st_mtime == pytest.approx(clock.now().timestamp())


def test_上限を超えたら最も古く使ったファイルから消す(paths: Paths, clock: FakeClock) -> None:
    cache = CacheManager(paths.cache_dir, max_bytes=250, clock=clock)
    tts = cache.dir_for("core") / "tts"
    tts.mkdir()
    other = cache.dir_for("stars")
    oldest = _write(cache, tts / "1.wav", 100, clock)
    middle = _write(cache, other / "2.wav", 100, clock)
    newest = _write(cache, tts / "3.wav", 100, clock)

    cache.enforce_limit()

    assert not oldest.exists()
    assert middle.exists()
    assert newest.exists()
    # ディレクトリは消さない
    assert tts.is_dir()


def test_上限の内なら何も消さない(paths: Paths, clock: FakeClock) -> None:
    cache = CacheManager(paths.cache_dir, max_bytes=300, clock=clock)
    directory = cache.dir_for("core")
    files = [_write(cache, directory / f"{i}.wav", 100, clock) for i in range(3)]
    cache.enforce_limit()
    assert all(path.exists() for path in files)


def test_大きく超えたら上限の内に収まるまで消す(paths: Paths, clock: FakeClock) -> None:
    cache = CacheManager(paths.cache_dir, max_bytes=150, clock=clock)
    directory = cache.dir_for("core")
    files = [_write(cache, directory / f"{i}.wav", 100, clock) for i in range(4)]
    cache.enforce_limit()
    assert [path.exists() for path in files] == [False, False, False, True]


def test_使い直したファイルは残る(paths: Paths, clock: FakeClock) -> None:
    cache = CacheManager(paths.cache_dir, max_bytes=250, clock=clock)
    directory = cache.dir_for("core")
    first = _write(cache, directory / "1.wav", 100, clock)
    second = _write(cache, directory / "2.wav", 100, clock)
    cache.touch(first)  # 古いほうを使い直す
    clock.advance(1)
    third = _write(cache, directory / "3.wav", 100, clock)

    cache.enforce_limit()

    assert first.exists()
    assert not second.exists()
    assert third.exists()


def test_根の外を指すリンクはたどらず数えない(
    paths: Paths, clock: FakeClock, tmp_path: Path
) -> None:
    cache = CacheManager(paths.cache_dir, max_bytes=50, clock=clock)
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "big.bin"
    precious.write_bytes(b"x" * 1000)
    directory = cache.dir_for("core")
    (directory / "link").symlink_to(outside, target_is_directory=True)
    (directory / "file-link").symlink_to(precious)
    small = _write(cache, directory / "small.wav", 10, clock)

    cache.enforce_limit()

    assert precious.exists()
    assert small.exists()


def test_removeは根の中のファイルを消す(paths: Paths, clock: FakeClock) -> None:
    cache = CacheManager(paths.cache_dir, max_bytes=1000, clock=clock)
    path = cache.dir_for("core") / "a.wav"
    path.write_bytes(b"x")
    cache.remove(path)
    assert not path.exists()


def test_removeはないファイルでも誤りにしない(paths: Paths, clock: FakeClock) -> None:
    cache = CacheManager(paths.cache_dir, max_bytes=1000, clock=clock)
    cache.remove(cache.dir_for("core") / "missing.wav")


def test_removeは根の外のパスを断る(paths: Paths, clock: FakeClock, tmp_path: Path) -> None:
    cache = CacheManager(paths.cache_dir, max_bytes=1000, clock=clock)
    cache.dir_for("core")
    outside = tmp_path / "keep.txt"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError):
        cache.remove(outside)
    with pytest.raises(ValueError):
        cache.remove(paths.cache_dir / "core" / ".." / ".." / ".." / "keep.txt")
    assert outside.exists()


def test_removeはディレクトリを消さない(paths: Paths, clock: FakeClock) -> None:
    cache = CacheManager(paths.cache_dir, max_bytes=1000, clock=clock)
    directory = cache.dir_for("core")
    with pytest.raises(ValueError):
        cache.remove(directory)
    assert directory.is_dir()


def test_数えている間に消えたファイルは無視する(
    paths: Paths, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = CacheManager(paths.cache_dir, max_bytes=50, clock=clock)
    directory = cache.dir_for("core")
    old = _write(cache, directory / "old.wav", 100, clock)
    _write(cache, directory / "new.wav", 10, clock)
    real_unlink = os.unlink

    def vanish_then_unlink(path: str | os.PathLike[str], *args: object, **kwargs: object) -> None:
        real_unlink(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr(os, "unlink", vanish_then_unlink)
    cache.enforce_limit()
    assert not old.exists()


def test_上限が正でなければ断る(paths: Paths, clock: FakeClock) -> None:
    with pytest.raises(ValueError):
        CacheManager(paths.cache_dir, max_bytes=0, clock=clock)
