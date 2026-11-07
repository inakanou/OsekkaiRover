"""鍵のファイルを読む口。要件 8.1、8.3、9.2。

鍵はリポジトリの外の `~/.config/osekkai/env` に「名前=値」の行で置く（8.3）。
持ち主が本人で、ほかの人が読み書きできないときだけ値を返す。
値と行の中身は、ログにも例外の文にも出さない。問題は種類の名前だけで伝える（8.1）。
"""

from __future__ import annotations

import os
import stat
from enum import Enum
from typing import Final

from osekkai.core.paths import Paths

VARIABLE_NAME: Final = "ANTHROPIC_API_KEY"
FILE_NAME: Final = "env"
_QUOTES: Final = "\"'"
_MAX_BYTES: Final = 64 * 1024  # 鍵のファイルはこれより大きくならない


class SetupProblem(Enum):
    NO_FILE = "no_file"
    NOT_PRIVATE = "not_private"  # 持ち主が本人でない、またはほかの人が読み書きできる
    NOT_WRITTEN = "not_written"  # 名前の行がないか、値が空
    UNREADABLE = "unreadable"


def read_setup(paths: Paths) -> str | SetupProblem:
    """鍵の値を返す。使えなければ問題の種類を返す。

    ここではログに何も出さない。呼ぶ側が SetupProblem の名前だけを出す。
    """
    target = paths.config_dir / FILE_NAME
    try:
        # O_NONBLOCK: 名前つきパイプが置かれていても待ち続けない
        fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return SetupProblem.NO_FILE
    except OSError:
        return SetupProblem.UNREADABLE
    try:
        # 開いたものを確かめる（確かめてから開くまでに入れ替わらないように）
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return SetupProblem.UNREADABLE
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            return SetupProblem.NOT_PRIVATE
        data = os.read(fd, _MAX_BYTES)
    except OSError:
        return SetupProblem.UNREADABLE
    finally:
        os.close(fd)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return SetupProblem.UNREADABLE
    value = _find(text)
    return value if value else SetupProblem.NOT_WRITTEN


def _find(text: str) -> str:
    """決まった名前の最後の行の値。なければ空。"""
    found = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if sep and name.strip() == VARIABLE_NAME:
            found = _unquote(value.strip())
    return found


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
        value = value[1:-1]
    return value.strip()
