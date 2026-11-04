"""単一の実行の保証（1.7）。

ロックのファイルに `flock(LOCK_EX | LOCK_NB)` をかけ、プロセスが続くあいだ保つ。
取れなかったときの知らせと終了コード 75 は、呼び出し側（Runtime）が受け持つ。
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType

from osekkai.core.paths import ensure_private_dir


class InstanceLock:
    """1 つのロックのファイルの排他のロック。取ったら、このオブジェクトが fd を持ち続ける。"""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """ロックを取る。ほかが持っていれば False（すでに動いている）。"""
        if self._fd is not None:
            return True
        ensure_private_dir(self._path.parent)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        # 調べやすいように、持っているプロセスの番号を書く（失敗してもロックは保つ）
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass
        return True

    def release(self) -> None:
        """ロックを放す。2 回呼んでもよい。"""
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)  # 閉じると flock も外れる

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
