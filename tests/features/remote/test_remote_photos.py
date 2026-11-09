"""写真の名前・保存・整理・返してよいか（要件 9.5、9.6、9.7）。"""

import logging
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from remote_fakes import FakeClock

from osekkai.features.remote.photos import PHOTO_NAME, PhotoNamesExhausted, PhotoStore

JST = timezone(timedelta(hours=9))
SHOT = datetime(2026, 11, 8, 10, 20, 30, tzinfo=JST)
JPEG = b"\xff\xd8hidden\xff\xd9"


def _store(tmp_path: Path) -> PhotoStore:
    return PhotoStore(tmp_path / "data" / "remote" / "photos", FakeClock(SHOT))


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _put(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(JPEG)
    return path


# --- 名前と保存 ---


def test_名前は撮った日時と通し番号とカメラ(tmp_path: Path) -> None:
    store = _store(tmp_path)
    name = store.save("neck", JPEG, SHOT)
    assert name == "20261108-102030-00-neck.jpg"  # 9.5
    assert PHOTO_NAME.fullmatch(name)
    assert (store.directory / name).read_bytes() == JPEG


def test_同じ秒の写真は重ならない(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.save("neck", JPEG, SHOT)
    second = store.save("neck", b"other", SHOT.replace(microsecond=500))
    assert first == "20261108-102030-00-neck.jpg"
    assert second == "20261108-102030-01-neck.jpg"
    assert (store.directory / first).read_bytes() == JPEG
    assert (store.directory / second).read_bytes() == b"other"


def test_カメラがちがえば同じ番号でもよい(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save("neck", JPEG, SHOT)
    assert store.save("usb", JPEG, SHOT) == "20261108-102030-00-usb.jpg"


def test_もとからある写真を上書きしない(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = _put(store.directory, "20261108-102030-00-neck.jpg")
    old.write_bytes(b"old")
    assert store.save("neck", JPEG, SHOT) == "20261108-102030-01-neck.jpg"
    assert old.read_bytes() == b"old"


def test_その秒の番号を使い切ったら知らせる(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for number in range(100):
        _put(store.directory, f"20261108-102030-{number:02d}-neck.jpg")
    with pytest.raises(PhotoNamesExhausted):
        store.save("neck", JPEG, SHOT)


def test_知らないカメラの名前は断る(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        bad: Any = "../x"
        store.save(bad, JPEG, SHOT)


def test_ファイルは本人だけディレクトリも本人だけ(tmp_path: Path) -> None:
    store = _store(tmp_path)
    name = store.save("usb", JPEG, SHOT)
    assert _mode(store.directory / name) == 0o600  # 9.5
    assert _mode(store.directory) == 0o700
    assert _mode(store.directory.parent) == 0o700


def test_一時ファイルを残さない(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save("neck", JPEG, SHOT)
    store.save("usb", JPEG, SHOT)
    assert sorted(os.listdir(store.directory)) == [
        "20261108-102030-00-neck.jpg",
        "20261108-102030-00-usb.jpg",
    ]


def test_置き換えに失敗しても一時ファイルを残さない(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)

    def broken(src: object, dst: object) -> None:
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        store.save("neck", JPEG, SHOT)
    assert os.listdir(store.directory) == []


def test_保存で写真の名前をログに出さない(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    store = _store(tmp_path)
    name = store.save("neck", JPEG, SHOT)
    assert name not in caplog.text
    assert "102030" not in caplog.text


# --- 返してよいか ---


def test_保存した写真は返す(tmp_path: Path) -> None:
    store = _store(tmp_path)
    name = store.save("neck", JPEG, SHOT)
    assert store.path_for(name) == store.directory / name  # 9.7


def test_ない写真は返さない(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.path_for("20261108-102030-00-neck.jpg") is None


@pytest.mark.parametrize(
    "name",
    [
        "../20261108-102030-00-neck.jpg",
        "..",
        "/etc/passwd",
        "sub/20261108-102030-00-neck.jpg",
        "20261108-102030-00-neck.jpg/",
        "20261108-102030-00-neck.jpeg",
        "20261108-102030-00-face.jpg",
        "20261108-102030-0-neck.jpg",
        "x20261108-102030-00-neck.jpg",
        "20261108-102030-00-neck.jpg\n",
        "notes.txt",
        "",
    ],
)
def test_名前の決まりに合わないものは返さない(tmp_path: Path, name: str) -> None:
    store = _store(tmp_path)
    store.save("neck", JPEG, SHOT)
    _put(store.directory, "notes.txt")
    _put(tmp_path / "data" / "remote", "20261108-102030-00-neck.jpg")
    assert store.path_for(name) is None  # 9.7


def test_シンボリックリンクは返さない(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = _put(tmp_path / "outside", "other.jpg")
    store.directory.mkdir(parents=True)
    (store.directory / "20261108-102030-00-neck.jpg").symlink_to(target)
    assert store.path_for("20261108-102030-00-neck.jpg") is None


def test_ディレクトリは返さない(tmp_path: Path) -> None:
    store = _store(tmp_path)
    (store.directory / "20261108-102030-00-neck.jpg").mkdir(parents=True)
    assert store.path_for("20261108-102030-00-neck.jpg") is None


# --- 整理 ---

NOW = datetime(2026, 12, 8, 10, 20, 30, tzinfo=JST)


def test_決めた日数より古い写真だけを消す(tmp_path: Path) -> None:
    store = _store(tmp_path)
    border = _put(store.directory, "20261108-102030-00-neck.jpg")  # ちょうど 30 日
    older = _put(store.directory, "20261108-102029-00-usb.jpg")  # 30 日と 1 秒
    oldest = _put(store.directory, "20260101-000000-05-neck.jpg")
    recent = _put(store.directory, "20261207-090000-00-neck.jpg")
    assert store.prune(NOW, 30) == 2  # 9.6
    assert border.exists()
    assert recent.exists()
    assert not older.exists()
    assert not oldest.exists()


def test_名前の決まりに合わないファイルに触らない(tmp_path: Path) -> None:
    store = _store(tmp_path)
    keep = [
        _put(store.directory, "notes.txt"),
        _put(store.directory, "20200101-000000-00-face.jpg"),
        _put(store.directory, "20200101-000000-00-neck.jpg.bak"),
        _put(store.directory, "20201399-000000-00-neck.jpg"),  # ない日付
    ]
    assert store.prune(NOW, 30) == 0
    assert all(path.exists() for path in keep)


def test_古い名前のシンボリックリンクとその先に触らない(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = _put(tmp_path / "outside", "keep.jpg")
    store.directory.mkdir(parents=True)
    link = store.directory / "20200101-000000-00-neck.jpg"
    link.symlink_to(target)
    assert store.prune(NOW, 30) == 0
    assert link.is_symlink()
    assert target.exists()


def test_古い名前のディレクトリは消さない(tmp_path: Path) -> None:
    store = _store(tmp_path)
    folder = store.directory / "20200101-000000-00-neck.jpg"
    folder.mkdir(parents=True)
    assert store.prune(NOW, 30) == 0
    assert folder.is_dir()


def test_保存先がまだないときは何もしない(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.prune(NOW, 30) == 0
    assert not store.directory.exists()


def test_消せなかった写真は飛ばして名前をログに出さない(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store = _store(tmp_path)
    stuck = _put(store.directory, "20200101-000000-00-neck.jpg")
    gone = _put(store.directory, "20200101-000000-01-neck.jpg")
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == stuck.name:
            raise PermissionError("nope")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert store.prune(NOW, 30) == 1
    assert stuck.exists()
    assert not gone.exists()
    assert "PermissionError" in caplog.text
    assert "20200101" not in caplog.text
