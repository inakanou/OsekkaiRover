"""時刻の同期の判定（10.1）。

`timedatectl show -p NTPSynchronized --value` の yes/no を使う。
使えないとき（失敗・時間切れ・ほかの答え）は、systemd-timesyncd の印のファイルの有無で判定する。
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]

QUERY_TIMEOUT: Final = 2.0
SYNC_MARKER: Final = Path("/run/systemd/timesync/synchronized")
COMMAND: Final = ("timedatectl", "show", "-p", "NTPSynchronized", "--value")


class TimedatectlTimeSync:
    """TimeSyncPort の実機の実装。例外は外に出さない。"""

    def __init__(self, *, run: Runner = subprocess.run, marker: Path = SYNC_MARKER) -> None:
        self._run = run
        self._marker = marker
        self._last: tuple[bool, str] | None = None

    def is_synchronized(self) -> bool:
        answer = self._query()
        if answer is not None:
            result, source = answer, "timedatectl"
        else:
            result, source = self._marker_exists(), "marker"
        # 判定が変わったときだけ残す（同期するまで 30 秒ごとに呼ばれるため）
        if self._last != (result, source):
            logger.info("時刻の同期の判定: %s（%s）", "済み" if result else "まだ", source)
            self._last = (result, source)
        return result

    def _query(self) -> bool | None:
        try:
            done = self._run(
                list(COMMAND),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=QUERY_TIMEOUT,
                check=False,
            )
        except Exception as error:
            logger.debug("timedatectl を使えません: %s", type(error).__name__)
            return None
        if done.returncode != 0:
            logger.debug("timedatectl が失敗しました: %d", done.returncode)
            return None
        value = str(done.stdout).strip()
        if value == "yes":
            return True
        if value == "no":
            return False
        return None

    def _marker_exists(self) -> bool:
        try:
            return self._marker.exists()
        except Exception as error:
            logger.debug("同期の印のファイルを見られません: %s", type(error).__name__)
            return False
