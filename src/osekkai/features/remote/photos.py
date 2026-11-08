"""写真の名前・保存・整理・返してよいか（要件 9.5、9.6、9.7）。

- 名前は `YYYYMMDD-HHMMSS-NN-<neck|usb>.jpg`。日時は撮った時刻（`Clock.now()` の地域の時刻）の
  壁の時刻で、NN はその秒とカメラで重ならない通し番号（00〜99）
- 一時ファイルに書いてから置き換える。ファイルは 0600、ディレクトリは 0700
- 整理は名前の日時でくらべる。`now` も地域の時刻の壁の時刻として見る（タイムゾーンは落とす）
- 写真の名前はログに出さない。出すのは誤りの型の名前だけ
"""

from __future__ import annotations

import logging
import os
import re
import stat
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from osekkai.core.clock import Clock
from osekkai.core.paths import Paths, ensure_private_dir

logger = logging.getLogger(__name__)

PHOTO_NAME: Final = re.compile(r"\d{8}-\d{6}-\d{2}-(neck|usb)\.jpg")
CAMERAS: Final = frozenset({"neck", "usb"})
MAX_PER_SECOND: Final = 100  # 通し番号は 00〜99
STAMP_FORMAT: Final = "%Y%m%d-%H%M%S"
STAMP_LENGTH: Final = 15  # YYYYMMDD-HHMMSS
TEMP_PREFIX: Final = ".remote-photo-"
FILE_MODE: Final = 0o600


class PhotoNamesExhausted(RuntimeError):
    """同じ秒・同じカメラの通し番号（00〜99）を使い切った。"""


def default_photo_dir(paths: Paths) -> Path:
    """写真の保存先（リポジトリの外、データの置き場所の中）。"""
    return paths.data_dir / "remote" / "photos"


class PhotoStore:
    """写真を保存し、古いものを消し、返してよい写真のパスを引く。"""

    def __init__(self, directory: Path, clock: Clock) -> None:
        self.directory = directory
        self._clock = clock  # 時刻は呼ぶ側が Clock.now() で渡す。同じ時計を持っておく

    def save(self, camera: Literal["neck", "usb"], jpeg: bytes, taken_at: datetime) -> str:
        """写真を重ならない名前で保存し、その名前を返す（9.5）。"""
        if camera not in CAMERAS:
            raise ValueError("知らないカメラです")
        ensure_private_dir(self.directory)
        temp = self._write_temp(jpeg)
        try:
            name = self._free_name(camera, taken_at)
            os.replace(temp, self.directory / name)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return name

    def path_for(self, name: str) -> Path | None:
        """この機能で撮った実在の写真のパス。それ以外は None（9.7）。"""
        if not PHOTO_NAME.fullmatch(name):
            return None
        path = self.directory / name
        try:
            info = os.lstat(path)
        except OSError:
            return None
        # シンボリックリンクやディレクトリは返さない
        return path if stat.S_ISREG(info.st_mode) else None

    def prune(self, now: datetime, days: int) -> int:
        """名前の日時が `now - days` より古い写真を消し、消した数を返す（9.6）。

        名前の決まりに合わないもの、シンボリックリンク、ディレクトリには触らない。
        """
        limit = now.replace(tzinfo=None) - timedelta(days=days)
        try:
            entries = list(os.scandir(self.directory))
        except FileNotFoundError:
            return 0
        except OSError as e:
            logger.warning("写真の保存先を読めませんでした: %s", type(e).__name__)
            return 0
        removed = 0
        for entry in entries:
            if not PHOTO_NAME.fullmatch(entry.name):
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            try:
                taken = datetime.strptime(entry.name[:STAMP_LENGTH], STAMP_FORMAT)
            except ValueError:
                continue  # ない日付の名前は撮った写真ではない
            if taken >= limit:
                continue
            try:
                Path(entry.path).unlink()
            except OSError as e:
                logger.warning("古い写真を消せませんでした: %s", type(e).__name__)
                continue
            removed += 1
        return removed

    def _write_temp(self, jpeg: bytes) -> Path:
        """同じディレクトリに本人だけが読める一時ファイルを書く。"""
        fd, temp_name = tempfile.mkstemp(dir=self.directory, prefix=TEMP_PREFIX)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as file:
                os.fchmod(file.fileno(), FILE_MODE)
                file.write(jpeg)
                file.flush()
                os.fsync(file.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return temp

    def _free_name(self, camera: str, taken_at: datetime) -> str:
        stamp = taken_at.strftime(STAMP_FORMAT)
        for number in range(MAX_PER_SECOND):
            name = f"{stamp}-{number:02d}-{camera}.jpg"
            if not os.path.lexists(self.directory / name):
                return name
        raise PhotoNamesExhausted("この秒の写真の名前を使い切りました")
