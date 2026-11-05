"""首の調整値の移し替え（9.3）。

サンプルの調整値のファイル（SunFounder の picar-x.conf。`キー = 値` の行）から 4 つの値を読み、
新しい版の設定（config.yaml）の `core.motion.calibration` の節に移す。
首の左右（pan）と上下（tilt）は、新しい基盤とサンプルで符号が逆なので反転する。

道具は YAML の読み書きの部品を使えないので、設定は行ごとに読んで、節の中の値の行だけを
置き換える（注釈は残す）。読み方が分からない書き方のときは、何も書かない。
値は、ログにも表示にも出さない。
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rover_deploy.layout import ensure_private_dir

MigrationStatus = Literal[
    "migrated", "nothing_to_migrate", "already_set", "out_of_range", "unreadable"
]

LIMIT_DEG = 20.0  # 調整値の範囲（−20〜20 度）
SECTION_PATH = ("core", "motion", "calibration")
ANGLE_KEYS = ("steering", "pan", "tilt")
MOTOR_KEY = "motor_direction"
CALIBRATION_KEYS = (*ANGLE_KEYS, MOTOR_KEY)

# サンプルのキー → 新しい設定のキーと、符号を反転するか
_SAMPLE_KEYS: dict[str, tuple[str, bool]] = {
    "picarx_dir_servo": ("steering", False),
    "picarx_cam_pan_servo": ("pan", True),
    "picarx_cam_tilt_servo": ("tilt", True),
    "picarx_dir_motor": (MOTOR_KEY, False),
}

_DEFAULTS: dict[str, object] = {"steering": 0.0, "pan": 0.0, "tilt": 0.0, MOTOR_KEY: (1, 1)}

_MOTOR_RE = re.compile(r"\[\s*([+-]?\d+)\s*,\s*([+-]?\d+)\s*\]")
_YAML_NUMBER_RE = re.compile(r"[-+]?(\d+(\.\d*)?|\.\d+)")
_KEY_RE = re.compile(r"( *)([A-Za-z_][A-Za-z0-9_]*) *:(.*)")
_COMMENT_RE = re.compile(r"\s#")


@dataclass(frozen=True)
class MigrationResult:
    """移し替えの結果。値は持たない。"""

    status: MigrationStatus


class _Unreadable(Exception):
    """読み方が分からない。"""


class _OutOfRange(Exception):
    """範囲の外の値がある。"""


class _AlreadySet(Exception):
    """設定の調整値が既定値でない。"""


# --- サンプルの調整値 ---


def _read_sample(path: Path) -> dict[str, float | tuple[int, int]]:
    """サンプルの調整値を読み、新しい設定のキーと値（変換した後）にする。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise _Unreadable from exc
    raw: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = (part.strip() for part in stripped.split("=", 1))
        if name in _SAMPLE_KEYS and name not in raw:
            raw[name] = value  # 同じキーが 2 つあれば、前のものを使う

    values: dict[str, float | tuple[int, int]] = {}
    out_of_range = False
    for name, value in raw.items():
        key, negate = _SAMPLE_KEYS[name]
        if key == MOTOR_KEY:
            match = _MOTOR_RE.fullmatch(value)
            if match is None:
                raise _Unreadable
            left, right = int(match.group(1)), int(match.group(2))
            if left not in (1, -1) or right not in (1, -1):
                out_of_range = True
            values[key] = (left, right)
        else:
            try:
                number = float(value)
            except ValueError as exc:
                raise _Unreadable from exc
            if not math.isfinite(number) or abs(number) > LIMIT_DEG:
                out_of_range = True
            values[key] = -number if negate else number
    if out_of_range:
        raise _OutOfRange
    return values


# --- 設定の行 ---


@dataclass(frozen=True)
class _Entry:
    """設定の中身のある 1 行。キーの行でなければ `key` は None。"""

    line: int
    indent: int
    key: str | None
    value: str  # 注釈を除いた値。なければ ""
    value_start: int  # 行の中の値の位置
    value_end: int


def _entries(lines: list[str]) -> list[_Entry]:
    entries: list[_Entry] = []
    for number, raw in enumerate(lines):
        body = raw.rstrip("\r")
        stripped = body.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(body) - len(stripped)
        if "\t" in body[:indent]:
            raise _Unreadable  # 字下げにタブは使えない
        match = _KEY_RE.fullmatch(body)
        if match is None or (match.group(3) and match.group(3)[0] not in " \t"):
            entries.append(_Entry(number, indent, None, stripped, indent, len(body)))
            continue
        rest = match.group(3)
        rest_start = match.start(3)
        comment = _COMMENT_RE.search(rest)
        value_part = rest[: comment.start()] if comment else rest
        value = value_part.strip()
        start = rest_start + (len(value_part) - len(value_part.lstrip()))
        entries.append(_Entry(number, indent, match.group(2), value, start, start + len(value)))
    return entries


def _block(entries: list[_Entry], parent: int) -> list[_Entry]:
    """`entries[parent]` の下の行（字下げが深い行）を返す。"""
    indent = entries[parent].indent
    block: list[_Entry] = []
    for entry in entries[parent + 1 :]:
        if entry.indent <= indent:
            break
        block.append(entry)
    return block


def _children(block: list[_Entry]) -> list[_Entry]:
    if not block:
        raise _Unreadable
    indent = block[0].indent
    if any(entry.indent < indent for entry in block):
        raise _Unreadable
    return [entry for entry in block if entry.indent == indent]


def _find_section(entries: list[_Entry]) -> tuple[list[_Entry], list[_Entry]]:
    """`core.motion.calibration` の節の下の行と、そのうちの子の行を返す。"""
    level = [entry for entry in entries if entry.indent == 0]
    block: list[_Entry] = []
    for name in SECTION_PATH:
        found = [entry for entry in level if entry.key == name]
        if len(found) != 1 or found[0].value != "":
            raise _Unreadable
        block = _block(entries, entries.index(found[0]))
        level = _children(block)
    return block, level


def _parse_current(entry: _Entry) -> float | tuple[int, int]:
    if entry.key == MOTOR_KEY:
        match = _MOTOR_RE.fullmatch(entry.value)
        if match is None:
            raise _Unreadable
        return (int(match.group(1)), int(match.group(2)))
    if _YAML_NUMBER_RE.fullmatch(entry.value) is None:
        raise _Unreadable
    return float(entry.value)


def _format(key: str, value: float | tuple[int, int]) -> str:
    if key == MOTOR_KEY:
        assert isinstance(value, tuple)
        return f"[{value[0]}, {value[1]}]"
    assert isinstance(value, float)
    number = round(value, 6)
    if number == 0:
        return "0.0"  # -0.0 にしない
    text = f"{number:.6f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def _rewrite(text: str, values: dict[str, float | tuple[int, int]]) -> str:
    """設定の調整値の節の行だけを置き換えた文を返す。既定値でなければ `_AlreadySet`。"""
    lines = text.split("\n")
    entries = _entries(lines)
    block, children = _find_section(entries)
    current: dict[str, _Entry] = {}
    for entry in children:
        if entry.key is None:
            raise _Unreadable
        if entry.key not in CALIBRATION_KEYS:
            continue
        if entry.key in current:
            raise _Unreadable
        current[entry.key] = entry
    already_set = False
    for key, entry in current.items():
        if _parse_current(entry) != _DEFAULTS[key]:
            already_set = True
    if already_set:
        raise _AlreadySet

    for key, entry in current.items():
        if key in values:
            line = lines[entry.line]
            new = _format(key, values[key])
            lines[entry.line] = line[: entry.value_start] + new + line[entry.value_end :]

    missing = [key for key in CALIBRATION_KEYS if key in values and key not in current]
    if missing:
        last = block[-1].line
        eol = "\r" if lines[last].endswith("\r") else ""
        indent = " " * children[0].indent
        added = [f"{indent}{key}: {_format(key, values[key])}{eol}" for key in missing]
        lines[last + 1 : last + 1] = added
    return "\n".join(lines)


def _write_private(path: Path, text: str) -> None:
    """本人だけが読み書きできる権限（0600）で原子的に書く。"""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def migrate(sample_conf: Path, config_file: Path, example: Path) -> MigrationResult:
    """サンプルの調整値を、新しい版の設定に移す。

    設定がなければ、版の見本（`example`）から作る。値はログに出さない。
    """
    try:
        values = _read_sample(sample_conf)
    except _Unreadable:
        return MigrationResult("unreadable")
    except _OutOfRange:
        return MigrationResult("out_of_range")
    if not values:
        return MigrationResult("nothing_to_migrate")

    exists = config_file.exists()
    source = config_file if exists else example
    try:
        text = source.read_bytes().decode("utf-8")  # 改行を変えないように読む
        new_text = _rewrite(text, values)
    except _AlreadySet:
        return MigrationResult("already_set")
    except (OSError, UnicodeError, _Unreadable):
        return MigrationResult("unreadable")
    if not exists:
        ensure_private_dir(config_file.parent)
    _write_private(config_file, new_text)
    return MigrationResult("migrated")
