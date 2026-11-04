"""InstanceLock（1.7）のテスト。"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

from osekkai.core.instance_lock import InstanceLock
from osekkai.core.paths import Paths


def test_同じロックを2回取ろうとすると2回目は断られる(tmp_path: Path) -> None:
    path = tmp_path / "run" / "rover.lock"
    first = InstanceLock(path)
    second = InstanceLock(path)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
        assert first.held is True
        assert second.held is False
    finally:
        first.release()
        second.release()


def test_放した後なら別のものが取れる(tmp_path: Path) -> None:
    path = tmp_path / "rover.lock"
    first = InstanceLock(path)
    assert first.acquire() is True
    first.release()
    first.release()  # 2 回放してもよい
    assert first.held is False
    second = InstanceLock(path)
    try:
        assert second.acquire() is True
    finally:
        second.release()


def test_ロックのファイルとディレクトリは本人だけが使える(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    lock = InstanceLock(paths.lock_file)
    try:
        assert lock.acquire() is True
        assert stat.S_IMODE(paths.runtime_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(paths.lock_file.stat().st_mode) == 0o600
        assert paths.lock_file.read_text().strip() == str(os.getpid())
    finally:
        lock.release()


def test_with文で取って抜けると放す(tmp_path: Path) -> None:
    path = tmp_path / "rover.lock"
    with InstanceLock(path) as lock:
        assert InstanceLock(path).acquire() is False
    assert lock.held is False
    other = InstanceLock(path)
    try:
        assert other.acquire() is True
    finally:
        other.release()


def test_別のプロセスが持っているあいだは取れない(tmp_path: Path) -> None:
    path = tmp_path / "rover.lock"
    lock = InstanceLock(path)
    try:
        assert lock.acquire() is True
        code = (
            "import sys\n"
            "from osekkai.core.instance_lock import InstanceLock\n"
            "sys.exit(0 if InstanceLock(sys.argv[1]).acquire() else 75)\n"
        )
        result = subprocess.run([sys.executable, "-c", code, str(path)], check=False)
        assert result.returncode == 75
    finally:
        lock.release()
