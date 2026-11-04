"""XDG のパス（12.1、12.5）。

置き場所はどれもリポジトリの作業コピーの外にあるので、デプロイで版を入れ替えても残る。
作るだけではディレクトリを作らない。作るのは保存する側（`ensure_private_dir`）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

APP_NAME = "osekkai"
CONFIG_FILE_NAME = "config.yaml"
LOCK_FILE_NAME = "rover.lock"
XDG_VARS = (
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
)

# 機能の名前と保存の key に使える文字（パスの外に出ないようにする）
SAFE_NAME = re.compile(r"[a-z0-9_-]+")


def validate_name(name: str, what: str = "name") -> str:
    """パスの 1 つの部分として安全な名前か確かめる。だめなら `ValueError`。"""
    if not SAFE_NAME.fullmatch(name):
        raise ValueError(f"使えない {what} です（a-z 0-9 _ - だけ）: {name!r}")
    return name


def ensure_private_dir(path: Path) -> Path:
    """ディレクトリを本人だけが読み書きできる権限（0700）で作る。

    今回作ったディレクトリだけに権限を付ける。もとからあるものは変えない。
    """
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(directory, 0o700)  # umask で削られた分を戻す
    return path


class Paths:
    """設定・状態・キャッシュ・データ・ロックの置き場所。"""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        home: Path | None = None,
        uid: int | None = None,
    ) -> None:
        source = os.environ if env is None else env
        # 作ったときの値を写しておく（後で環境が変わっても置き場所は変わらない）
        self._env = {name: source.get(name, "") for name in XDG_VARS}
        self._home = Path.home() if home is None else home
        self._uid = os.getuid() if uid is None else uid

    def _xdg(self, name: str) -> Path | None:
        value = self._env[name]
        if not value:
            return None
        path = Path(value)
        # 相対パスは XDG の決まりで無視する
        return path if path.is_absolute() else None

    def _base(self, name: str, default: str) -> Path:
        base = self._xdg(name)
        return (base if base is not None else self._home / default) / APP_NAME

    @property
    def config_dir(self) -> Path:
        return self._base("XDG_CONFIG_HOME", ".config")

    @property
    def config_file(self) -> Path:
        return self.config_dir / CONFIG_FILE_NAME

    @property
    def state_dir(self) -> Path:
        return self._base("XDG_STATE_HOME", ".local/state")

    def state_dir_for(self, feature: str) -> Path:
        return self.state_dir / validate_name(feature, "機能の名前")

    @property
    def cache_dir(self) -> Path:
        return self._base("XDG_CACHE_HOME", ".cache")

    def cache_dir_for(self, owner: str) -> Path:
        return self.cache_dir / validate_name(owner, "持ち主の名前")

    @property
    def data_dir(self) -> Path:
        return self._base("XDG_DATA_HOME", ".local/share")

    @property
    def runtime_dir(self) -> Path:
        base = self._xdg("XDG_RUNTIME_DIR")
        if base is None:
            return Path(f"/tmp/{APP_NAME}-{self._uid}")
        return base / APP_NAME

    @property
    def lock_file(self) -> Path:
        return self.runtime_dir / LOCK_FILE_NAME
