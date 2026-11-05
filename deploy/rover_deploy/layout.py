"""実機の置き場所（10.2）。

置き場所はここだけで決める。作るだけではディレクトリを作らない。
サンプルの場所（作業コピー・仮想環境・ユニット）は、読む・写す・止める・起動するだけで、書かない（9.1）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

CORE_APP_NAME = "osekkai"  # 基盤の XDG の名前（rover-core の Paths と同じ）
TOOL_APP_NAME = "osekkai-deploy"
CORE_UNIT_NAME = "osekkai.service"
SAMPLE_UNIT_NAME = "osekkai-rover.service"
RELEASE_DIR_LEN = 12  # 版のディレクトリの名前は sha の先頭 12 桁

_XDG_VARS = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")
_SHA_RE = re.compile(r"[0-9a-f]{40}")


def ensure_private_dir(path: Path) -> Path:
    """ディレクトリを本人だけが読み書きできる権限（0700）で作る。

    今回作った親にも 0700 を付ける。`path` そのものは、もとからあっても 0700 にそろえる。
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
    os.chmod(path, 0o700)
    return path


class Layout:
    """実機の置き場所。家のディレクトリ・環境の変数・uid から決める。"""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        home: Path | None = None,
        uid: int | None = None,
    ) -> None:
        source = os.environ if env is None else env
        # 作ったときの値を写しておく（後で環境が変わっても置き場所は変わらない）
        self._env = {name: source.get(name, "") for name in _XDG_VARS}
        self.home = Path.home() if home is None else home
        self._uid = os.getuid() if uid is None else uid

    def _xdg(self, name: str) -> Path | None:
        value = self._env[name]
        if not value:
            return None
        path = Path(value)
        # 相対パスは XDG の決まりで無視する
        return path if path.is_absolute() else None

    def _config_home(self) -> Path:
        base = self._xdg("XDG_CONFIG_HOME")
        return base if base is not None else self.home / ".config"

    def _state_home(self) -> Path:
        base = self._xdg("XDG_STATE_HOME")
        return base if base is not None else self.home / ".local/state"

    # --- 新しい基盤 ---

    @property
    def root(self) -> Path:
        return self.home / "osekkai-rover"

    @property
    def repo_git(self) -> Path:
        """bare の写し。"""
        return self.root / "repo.git"

    @property
    def releases_dir(self) -> Path:
        return self.root / "releases"

    def release_dir(self, sha: str) -> Path:
        """版のディレクトリ。`sha` は 40 桁の小文字の 16 進数。"""
        if not _SHA_RE.fullmatch(sha):
            raise ValueError("コミットの識別子は 40 桁の小文字の 16 進数にします")
        return self.releases_dir / sha[:RELEASE_DIR_LEN]

    @property
    def current_link(self) -> Path:
        """今の版へのリンク。"""
        return self.root / "current"

    @property
    def tool_dir(self) -> Path:
        """開発 PC から写した道具。"""
        return self.root / "tool"

    # --- ユニット ---

    @property
    def user_unit_dir(self) -> Path:
        return self._config_home() / "systemd/user"

    @property
    def core_unit_file(self) -> Path:
        return self.user_unit_dir / CORE_UNIT_NAME

    @property
    def sample_unit_file(self) -> Path:
        return self.user_unit_dir / SAMPLE_UNIT_NAME

    # --- 道具の状態 ---

    @property
    def state_dir(self) -> Path:
        return self._state_home() / TOOL_APP_NAME

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "deploy.lock"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "log"

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "backups"

    # --- 基盤の XDG の置き場所（rover-core の Paths と同じ決まり） ---

    @property
    def core_config_dir(self) -> Path:
        return self._config_home() / CORE_APP_NAME

    @property
    def core_state_dir(self) -> Path:
        return self._state_home() / CORE_APP_NAME

    @property
    def runtime_dir(self) -> Path:
        """基盤の実行時のディレクトリ（状態のファイルと知らせの置き場所）。

        `XDG_RUNTIME_DIR` がなければ `/tmp/osekkai-<uid>`。
        rover-core の `Paths.runtime_dir` と同じ決まりにする。
        """
        base = self._xdg("XDG_RUNTIME_DIR")
        if base is None:
            return Path(f"/tmp/{CORE_APP_NAME}-{self._uid}")
        return base / CORE_APP_NAME

    # --- サンプル ---

    @property
    def sample_dir(self) -> Path:
        """サンプルの作業コピー。"""
        return self.home / "works/OsekkaiRover"

    @property
    def sample_venv(self) -> Path:
        return self.home / ".venvs/osekkai"

    def sample_exists(self) -> bool:
        """サンプルの作業コピーがあるか。"""
        return (self.sample_dir / ".git").exists()
