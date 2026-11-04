"""機能ごとの状態の保存（12.2、12.3、12.4）。"""

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.paths import Paths
from osekkai.core.storage import StateStore


@pytest.fixture
def paths(xdg_dirs: dict[str, Path]) -> Paths:
    return Paths()


def test_保存したものを読める(paths: Paths, clock: FakeClock) -> None:
    store = StateStore.for_feature(paths, "stars", clock)
    store.save("record", {"count": 3, "names": ["a"], "ok": True, "x": None})
    assert store.load("record") == {"count": 3, "names": ["a"], "ok": True, "x": None}
    assert (paths.state_dir / "stars" / "record.json").is_file()


def test_ファイルがなければNoneを返す(paths: Paths, clock: FakeClock) -> None:
    store = StateStore.for_feature(paths, "stars", clock)
    assert store.load("record") is None


def test_状態のディレクトリは本人だけが読み書きできる権限で作る(
    paths: Paths, clock: FakeClock
) -> None:
    StateStore.for_feature(paths, "stars", clock).save("record", {"a": 1})
    for path in (paths.state_dir, paths.state_dir / "stars"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_置き換えの直前で例外が起きても前の内容が読める(
    paths: Paths, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore.for_feature(paths, "stars", clock)
    store.save("record", {"count": 1})

    def broken_replace(src: object, dst: object) -> None:
        raise OSError("電源が切れた")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", broken_replace)
        with pytest.raises(OSError):
            store.save("record", {"count": 2})

    assert store.load("record") == {"count": 1}
    # 一時ファイルは残らない
    assert sorted(p.name for p in (paths.state_dir / "stars").iterdir()) == ["record.json"]


def test_保存はfsyncしてから置き換えディレクトリもfsyncする(
    paths: Paths, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def spy_fsync(fd: int) -> None:
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        calls.append(f"fsync-{kind}")
        real_fsync(fd)

    def spy_replace(src: str, dst: str) -> None:
        calls.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    StateStore.for_feature(paths, "stars", clock).save("record", {"a": 1})
    assert calls == ["fsync-file", "replace", "fsync-dir"]


@pytest.mark.parametrize("content", [b"{not json", b"[1, 2]", b"\xff\xfe\x00"])
def test_壊れたファイルは退避して空として返しログに残す(
    paths: Paths,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    content: bytes,
) -> None:
    feature_dir = paths.state_dir / "stars"
    feature_dir.mkdir(parents=True)
    (feature_dir / "record.json").write_bytes(content)
    store = StateStore.for_feature(paths, "stars", clock)

    with caplog.at_level(logging.WARNING):
        assert store.load("record") is None

    names = sorted(p.name for p in feature_dir.iterdir())
    assert names == ["record.corrupt-20260101T090000.json"]
    assert (feature_dir / names[0]).read_bytes() == content
    assert "record" in caplog.text
    assert "not json" not in caplog.text
    # 退避の後は空から始め、保存もできる
    store.save("record", {"a": 1})
    assert store.load("record") == {"a": 1}


def test_同じ時刻に2回壊れても前の退避を上書きしない(paths: Paths, clock: FakeClock) -> None:
    feature_dir = paths.state_dir / "stars"
    feature_dir.mkdir(parents=True)
    store = StateStore.for_feature(paths, "stars", clock)
    for content in (b"{1", b"{2"):
        (feature_dir / "record.json").write_bytes(content)
        assert store.load("record") is None
    contents = sorted(p.read_bytes() for p in feature_dir.iterdir())
    assert contents == [b"{1", b"{2"]


@pytest.mark.parametrize("key", ["..", "../other/x", "a/b", "/etc/passwd", "", "A", "a.b", "a\\b"])
def test_パスの外に出られるkeyを断る(paths: Paths, clock: FakeClock, key: str) -> None:
    store = StateStore.for_feature(paths, "stars", clock)
    with pytest.raises(ValueError):
        store.save(key, {"a": 1})
    with pytest.raises(ValueError):
        store.load(key)


def test_ほかの機能の保存先に書けない(paths: Paths, clock: FakeClock) -> None:
    mine = StateStore.for_feature(paths, "stars", clock)
    other = StateStore.for_feature(paths, "quiz", clock)
    other.save("record", {"owner": "quiz"})

    for key in ("../quiz/record", "..%2Fquiz%2Frecord"):
        with pytest.raises(ValueError):
            mine.save(key, {"owner": "stars"})
    mine.save("record", {"owner": "stars"})

    assert other.load("record") == {"owner": "quiz"}
    assert json.loads((paths.state_dir / "quiz" / "record.json").read_text()) == {"owner": "quiz"}


def test_機能の名前でパスの外に出られない(paths: Paths, clock: FakeClock) -> None:
    with pytest.raises(ValueError):
        StateStore.for_feature(paths, "../quiz", clock)


def test_書けない値の保存は失敗し前の内容を保ち一時ファイルを残さない(
    paths: Paths, clock: FakeClock
) -> None:
    store = StateStore.for_feature(paths, "stars", clock)
    store.save("record", {"count": 1})
    with pytest.raises(TypeError):
        store.save("record", {"bad": object()})  # type: ignore[dict-item]
    assert store.load("record") == {"count": 1}
    assert sorted(p.name for p in (paths.state_dir / "stars").iterdir()) == ["record.json"]
