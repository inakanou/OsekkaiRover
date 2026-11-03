"""機能ごとの状態の保存（12.2、12.3、12.4）。"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Union

from osekkai.core.clock import Clock
from osekkai.core.paths import Paths, ensure_private_dir, validate_name

# 3.11 でも動くように Union で書く
JsonValue = Union[None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]]  # noqa: UP007

logger = logging.getLogger(__name__)


class StateStore:
    """1 つの機能の状態を `<dir>/<key>.json` に保存する。機能ごとに 1 つ作る。"""

    def __init__(self, directory: Path, clock: Clock) -> None:
        self._dir = directory
        self._clock = clock

    @classmethod
    def for_feature(cls, paths: Paths, feature: str, clock: Clock) -> StateStore:
        return cls(paths.state_dir_for(feature), clock)

    @property
    def directory(self) -> Path:
        return self._dir

    def _file(self, key: str) -> Path:
        return self._dir / f"{validate_name(key, 'key')}.json"

    def load(self, key: str) -> dict[str, JsonValue] | None:
        """読む。なければ `None`。壊れていれば退避して `None`（12.4）。"""
        path = self._file(key)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            return data
        self._move_aside(key, path)
        return None

    def _move_aside(self, key: str, path: Path) -> None:
        stamp = self._clock.now().strftime("%Y%m%dT%H%M%S")
        target = self._dir / f"{key}.corrupt-{stamp}.json"
        number = 1
        while target.exists():
            number += 1
            target = self._dir / f"{key}.corrupt-{stamp}-{number}.json"
        os.replace(path, target)
        # 中身はログに出さない
        logger.warning("壊れた状態のファイルを退避しました: key=%s -> %s", key, target.name)

    def save(self, key: str, data: Mapping[str, JsonValue]) -> None:
        """原子的に書く（12.3）。失敗したら例外を投げ、前の内容は残る。"""
        path = self._file(key)
        text = json.dumps(dict(data), ensure_ascii=False, indent=2)
        ensure_private_dir(self._dir)
        fd, tmp_name = tempfile.mkstemp(dir=self._dir, prefix=f".{key}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise
        _fsync_dir(self._dir)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
