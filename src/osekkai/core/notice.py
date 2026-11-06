"""外から置かれた知らせ（notice.json）を読む（device-deployment 5.5、7.1）。

配置の道具が `Paths.runtime_dir / "notice.json"` に知らせを置く（版の知らせ、戻し忘れの知らせ）。
待機画面は描くたびに `NoticeBoard.current()` を読み、ある行を待機画面の行の代わりに出す。

- ない・期限切れ・形が違う → `None`
- 形が違う・読めないときは、続く間は最初の 1 回だけ警告する
  （毎秒読むので、ログが埋まらないように）。中身はログに出さない
"""

from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from osekkai.core.display import FailureLog

if TYPE_CHECKING:
    from osekkai.core.clock import Clock

logger = logging.getLogger(__name__)

NOTICE_SCHEMA: Final = 1
NOTICE_FILE_NAME: Final = "notice.json"
_KEYS: Final = frozenset({"schema", "rows", "until"})


class NoticeUnreadable(ValueError):
    """知らせの形が違って読めない。中身は持たない。"""


@dataclass(frozen=True)
class Notice:
    rows: tuple[str | None, str | None]  # None の行は待機画面のまま
    until: float | None  # エポック秒。None は消されるまで

    @classmethod
    def from_dict(cls, data: object) -> Notice:
        """形を確かめて作る。違えば `NoticeUnreadable`。"""
        if not isinstance(data, dict) or set(data) != _KEYS:
            raise NoticeUnreadable("キーが違います")
        schema = data["schema"]
        if type(schema) is not int or schema != NOTICE_SCHEMA:
            raise NoticeUnreadable("schema が違います")
        rows = data["rows"]
        if not isinstance(rows, list) or len(rows) != 2:
            raise NoticeUnreadable("rows の形が違います")
        if not all(row is None or isinstance(row, str) for row in rows):
            raise NoticeUnreadable("rows の形が違います")
        until = data["until"]
        if until is not None:
            if isinstance(until, bool) or not isinstance(until, int | float):
                raise NoticeUnreadable("until の形が違います")
            try:
                until = float(until)  # 大きすぎる整数は OverflowError
            except OverflowError:
                raise NoticeUnreadable("until の形が違います") from None
            if not math.isfinite(until):
                raise NoticeUnreadable("until の形が違います")
        return cls(rows=(rows[0], rows[1]), until=until)

    def expired(self, now_epoch: float) -> bool:
        return self.until is not None and now_epoch >= self.until


class NoticeBoard:
    """知らせのファイルを読み、期限内の知らせを返す。複数のスレッドから呼んでよい。"""

    def __init__(self, path: Path, clock: Clock) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = FailureLog(logger)

    @property
    def path(self) -> Path:
        return self._path

    def current(self) -> Notice | None:
        """ない・期限切れ・形が違う → `None`。"""
        with self._lock:
            try:
                text = self._path.read_text(encoding="utf-8")
            except FileNotFoundError:
                self._failures.succeeded()
                return None
            except (OSError, UnicodeDecodeError) as error:
                self._failures.failed("知らせを読めませんでした: %s", type(error).__name__)
                return None
            try:
                notice = Notice.from_dict(json.loads(text))
            except (ValueError, RecursionError):
                # JSON の誤り（ValueError の仲間）と形の誤り。中身は出さない
                self._failures.failed("知らせの形が違います")
                return None
            self._failures.succeeded()
        if notice.expired(self._clock.now().timestamp()):
            return None
        return notice
