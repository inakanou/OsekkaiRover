"""キャッシュの合計の上限（12.1、12.6）。

キャッシュ全体（`cache/osekkai/`）の合計の大きさを上限の内に保つ。
最後に使った時刻は mtime で持ち、使うたびに `touch` で時計の時刻にする。
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import threading
from pathlib import Path

from osekkai.core.clock import Clock
from osekkai.core.paths import Paths, ensure_private_dir, validate_name

MIB = 1024 * 1024

logger = logging.getLogger(__name__)


class CacheManager:
    """キャッシュの置き場所と、合計の上限を守る仕組み。"""

    def __init__(self, root: Path, max_bytes: int, clock: Clock) -> None:
        if max_bytes <= 0:
            raise ValueError("キャッシュの上限は 1 以上にしてください")
        self._root = root
        self._max_bytes = max_bytes
        self._clock = clock
        # 消す操作は複数のスレッドから呼ばれる
        self._lock = threading.Lock()

    @classmethod
    def from_paths(cls, paths: Paths, max_mb: int, clock: Clock) -> CacheManager:
        return cls(paths.cache_dir, max_mb * MIB, clock)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def dir_for(self, owner: str) -> Path:
        """持ち主の置き場所を返す。なければ 0700 で作る。"""
        return ensure_private_dir(self._root / validate_name(owner, "持ち主の名前"))

    def touch(self, path: Path) -> None:
        """最後に使った時刻を今にする。"""
        stamp = self._clock.now().timestamp()
        os.utime(path, (stamp, stamp))

    def enforce_limit(self) -> None:
        """合計が上限を超えていれば、最後に使った時刻が古いものから消す。"""
        with self._lock:
            entries = self._scan()
            total = sum(size for _, size, _ in entries)
            if total <= self._max_bytes:
                return
            entries.sort(key=lambda entry: entry[2])
            removed = 0
            freed = 0
            for path, size, _ in entries:
                if total <= self._max_bytes:
                    break
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass  # ほかで消えた
                except OSError as error:
                    logger.warning("キャッシュのファイルを消せませんでした: %s", error.strerror)
                    continue
                total -= size
                removed += 1
                freed += size
            logger.info("キャッシュを %d 個（%d バイト）消しました", removed, freed)

    def remove(self, path: Path) -> None:
        """根の中のファイルを 1 つ消す。なければ何もしない。"""
        target = self._inside_root(path)
        with self._lock:
            try:
                mode = os.lstat(target).st_mode
            except FileNotFoundError:
                return
            if stat.S_ISDIR(mode):
                raise ValueError("ディレクトリは消せません")
            with contextlib.suppress(FileNotFoundError):
                os.unlink(target)

    def _inside_root(self, path: Path) -> Path:
        # 親のリンクと「..」を解いてから、根の中か確かめる
        absolute = Path(os.path.abspath(path))
        target = absolute.parent.resolve() / absolute.name
        root = self._root.resolve()
        if target == root or not target.is_relative_to(root):
            raise ValueError("キャッシュの外のパスは消せません")
        return target

    def _scan(self) -> list[tuple[str, int, float]]:
        """根の下の普通のファイルを（パス、大きさ、mtime）で集める。リンクはたどらない。"""
        entries: list[tuple[str, int, float]] = []
        if not self._root.is_dir():
            return entries
        for directory, _dirs, files in os.walk(self._root, followlinks=False):
            for name in files:
                path = os.path.join(directory, name)
                try:
                    info = os.lstat(path)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    entries.append((path, info.st_size, info.st_mtime))
        return entries
