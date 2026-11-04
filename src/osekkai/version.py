"""動いている版の文字列を返す。"""

import subprocess
from importlib import metadata
from pathlib import Path

_DIST_NAME = "osekkai-rover"
_GIT_TIMEOUT_S = 2.0


def _git_describe() -> str | None:
    # パッケージのある場所で調べる（作業ディレクトリに左右されない）
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    described = result.stdout.strip()
    return described or None


def _package_version() -> str:
    try:
        return metadata.version(_DIST_NAME)
    except metadata.PackageNotFoundError:
        return "unknown"


def get_version() -> str:
    """git describe の結果を返す。使えなければパッケージの版を返す。"""
    return _git_describe() or _package_version()
