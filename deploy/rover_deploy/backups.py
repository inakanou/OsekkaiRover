"""バックアップの作成・世代の削除・一覧・復元（3.1、3.2、3.4、3.5、3.7、9.2）。

普段のバックアップ（`backup-<識別子>.tar.gz`）には、次を入れる。
- 基盤の設定（`config/`）、記録（`state/`）
- 基盤のデータ（`data/`）。取り直せる素材（voices・models・jtalk）は除く
- サンプルのデータ（`sample-data/`）。声のキャッシュ（tts_cache）は除く

サンプルのバックアップ（`sample-<識別子>.tar.gz`）は、初めて新しい版を入れるときに作る（9.2）。
作業コピー全体（`sample/`）、ユニットのファイル（`sample-unit/`）、
仮想環境の `pip freeze`（`pip-freeze.txt`）。
サンプルの場所には何も書かない（9.1）。世代の削除でも消さない。

ファイルは 0600、置き場所は 0700 にする（3.4）。一時の名前で書いてから置き換える。
例外の文言は決まったものだけにする（ファイルの中身やパスは出さない）。
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import stat
import tarfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rover_deploy.layout import CORE_APP_NAME, Layout, ensure_private_dir
from rover_deploy.state import validate_deploy_id
from rover_deploy.system import CommandError, Runner

KEEP_BACKUPS = 5  # 実機に残す普段のバックアップの数（3.5）
PIP_FREEZE_TIMEOUT_S = 120.0

BACKUP_PREFIX = "backup-"
SAMPLE_BACKUP_PREFIX = "sample-"
SUFFIX = ".tar.gz"

# 普段のバックアップの中の名前
CONFIG_ROOT = "config"
STATE_ROOT = "state"
DATA_ROOT = "data"
SAMPLE_DATA_ROOT = "sample-data"
# サンプルのバックアップの中の名前
SAMPLE_ROOT = "sample"
SAMPLE_UNIT_ROOT = "sample-unit"
PIP_FREEZE_NAME = "pip-freeze.txt"

DATA_EXCLUDES = frozenset({"voices", "models", "jtalk"})  # 取り直せる素材
SAMPLE_DATA_EXCLUDES = frozenset({"tts_cache"})  # 声のキャッシュ

_STAGING_SUFFIX = ".restore-new"
_OLD_SUFFIX = ".restore-old"
_ID_RE = re.compile(r"\d{8}T\d{6}Z")


class BackupError(Exception):
    """バックアップを扱えなかった。文言は決まったものだけ。"""


class BackupFailed(BackupError):
    """バックアップを作れなかった（3.2）。"""

    def __init__(self) -> None:
        super().__init__("バックアップを作れませんでした")


class RestoreFailed(BackupError):
    """バックアップから戻せなかった。"""

    def __init__(self, message: str = "バックアップから戻せませんでした") -> None:
        super().__init__(message)


@dataclass(frozen=True)
class BackupInfo:
    backup_id: str  # deploy_id と同じ形
    path: Path
    size_bytes: int


@dataclass(frozen=True)
class _Source:
    """バックアップに入れるディレクトリかファイル 1 つ。"""

    root: str  # バックアップの中の名前
    path: Path  # 元の場所
    excludes: frozenset[str]  # 直下の、入れない名前
    follow_link: bool = False  # 元の場所がリンクなら先のファイルを入れる（ユニットのファイルだけ）


# --- 置き場所 ---


def core_data_dir(layout: Layout, env: Mapping[str, str] | None = None) -> Path:
    """基盤のデータのディレクトリ。rover-core の `Paths.data_dir` と同じ決まり。

    `XDG_DATA_HOME` が空か相対なら `~/.local/share` を使う。
    """
    source = os.environ if env is None else env
    value = source.get("XDG_DATA_HOME", "")
    base = Path(value) if value and Path(value).is_absolute() else layout.home / ".local/share"
    return base / CORE_APP_NAME


def backup_path(layout: Layout, backup_id: str) -> Path:
    """普段のバックアップのファイル。"""
    return layout.backups_dir / f"{BACKUP_PREFIX}{validate_deploy_id(backup_id)}{SUFFIX}"


def sample_backup_path(layout: Layout, backup_id: str) -> Path:
    """サンプルのバックアップのファイル。"""
    return layout.backups_dir / f"{SAMPLE_BACKUP_PREFIX}{validate_deploy_id(backup_id)}{SUFFIX}"


def _sources(layout: Layout, env: Mapping[str, str] | None) -> list[_Source]:
    return [
        _Source(CONFIG_ROOT, layout.core_config_dir, frozenset()),
        _Source(STATE_ROOT, layout.core_state_dir, frozenset()),
        _Source(DATA_ROOT, core_data_dir(layout, env), DATA_EXCLUDES),
        _Source(SAMPLE_DATA_ROOT, layout.sample_dir / "data", SAMPLE_DATA_EXCLUDES),
    ]


def _sample_sources(layout: Layout) -> list[_Source]:
    return [
        _Source(SAMPLE_ROOT, layout.sample_dir, frozenset()),
        # `systemctl --user link` で作ったユニットはリンクなので、先のファイルを入れる
        _Source(SAMPLE_UNIT_ROOT, layout.sample_unit_file, frozenset(), follow_link=True),
    ]


# --- 中身を数える ---


def _walk(source: _Source) -> Iterator[tuple[Path, str]]:
    """入れるディレクトリとふつうのファイルを、(元の場所, 中の名前) で返す。

    記号のリンクや特別なファイルは入れない（復元で外を指さないように）。
    `follow_link` のものだけは、リンクの先のふつうのファイルを、リンクの名前で入れる。
    """
    top = source.path
    try:
        top_stat = top.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISREG(top_stat.st_mode):
        yield top, f"{source.root}/{top.name}"
        return
    if stat.S_ISLNK(top_stat.st_mode) and source.follow_link:
        try:
            resolved = top.resolve(strict=True)
        except (OSError, RuntimeError):
            raise BackupFailed() from None
        if not stat.S_ISREG(resolved.lstat().st_mode):
            raise BackupFailed()
        yield resolved, f"{source.root}/{top.name}"
        return
    if stat.S_ISLNK(top_stat.st_mode):
        # 置き場所そのものがリンクだと、戻すときに形が変わる。黙って飛ばさずに失敗にする
        raise BackupFailed()
    if not stat.S_ISDIR(top_stat.st_mode):
        return
    yield top, source.root
    stack = [(top, source.root, True)]
    while stack:
        directory, name, is_top = stack.pop()
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            if is_top and entry.name in source.excludes:
                continue
            child_name = f"{name}/{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                yield Path(entry.path), child_name
                stack.append((Path(entry.path), child_name, False))
            elif entry.is_file(follow_symlinks=False):
                yield Path(entry.path), child_name


def _total_bytes(sources: list[_Source]) -> int:
    total = 0
    for source in sources:
        for path, _name in _walk(source):
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
    return total


def estimate_backup_bytes(
    layout: Layout, *, include_sample: bool = False, env: Mapping[str, str] | None = None
) -> int:
    """バックアップに入るファイルの大きさの合計（圧縮の前）。空き容量の確かめ（2.4）に使う。

    `include_sample` なら、サンプルのバックアップの分も足す（pip freeze の分は数えない）。
    読めないときや、置き場所そのものがリンクのとき（作るときに失敗する）は 0。
    """
    sources = _sources(layout, env)
    if include_sample and layout.sample_exists():
        sources += _sample_sources(layout)
    try:
        return _total_bytes(sources)
    except (OSError, BackupFailed):
        return 0


# --- 作る ---


def _write_archive(
    layout: Layout, final: Path, fill: Callable[[tarfile.TarFile], None]
) -> BackupInfo:
    """一時の名前で 0600 の tar.gz を書き、置き換える。失敗は `BackupFailed`。"""
    temporary = final.with_name(f".{final.name}.tmp")
    try:
        ensure_private_dir(layout.backups_dir)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as raw:
                with tarfile.open(fileobj=raw, mode="w:gz") as tar:
                    fill(tar)
                raw.flush()
                os.fsync(raw.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        os.replace(temporary, final)
        size = final.stat().st_size
    except (OSError, tarfile.TarError, CommandError):
        raise BackupFailed() from None
    backup_id = final.name.removeprefix(BACKUP_PREFIX).removeprefix(SAMPLE_BACKUP_PREFIX)
    return BackupInfo(backup_id=backup_id.removesuffix(SUFFIX), path=final, size_bytes=size)


def _add_sources(tar: tarfile.TarFile, sources: list[_Source]) -> None:
    for source in sources:
        for path, name in _walk(source):
            tar.add(path, arcname=name, recursive=False)


def create_backup(
    layout: Layout, deploy_id: str, *, env: Mapping[str, str] | None = None
) -> BackupInfo:
    """普段のバックアップを作る（3.1、3.4）。ない置き場所は飛ばす。失敗は `BackupFailed`（3.2）。"""
    final = backup_path(layout, deploy_id)
    sources = _sources(layout, env)
    return _write_archive(layout, final, lambda tar: _add_sources(tar, sources))


def create_sample_backup(layout: Layout, deploy_id: str, run: Runner) -> BackupInfo | None:
    """サンプルのバックアップを作る（9.2）。サンプルがなければ作らずに `None`。

    仮想環境そのものは写さず、`pip freeze` の出力だけを入れる。pip がなければ飛ばす。
    サンプルの場所には書かない。失敗は `BackupFailed`。
    """
    if not layout.sample_exists():
        return None
    final = sample_backup_path(layout, deploy_id)
    sources = _sample_sources(layout)
    pip = layout.sample_venv / "bin/pip"

    def fill(tar: tarfile.TarFile) -> None:
        _add_sources(tar, sources)
        if not pip.exists():
            return
        result = run([str(pip), "freeze"], timeout=PIP_FREEZE_TIMEOUT_S, check=True)
        info = tarfile.TarInfo(PIP_FREEZE_NAME)
        info.size = len(result.stdout)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(result.stdout))

    return _write_archive(layout, final, fill)


# --- 一覧と世代の削除 ---


def list_backups(layout: Layout) -> list[BackupInfo]:
    """普段のバックアップの一覧。新しい順。サンプルのバックアップは入れない。"""
    try:
        entries = list(os.scandir(layout.backups_dir))
    except FileNotFoundError:
        return []
    except OSError:
        raise BackupFailed() from None
    found: list[BackupInfo] = []
    for entry in entries:
        name = entry.name
        if not (name.startswith(BACKUP_PREFIX) and name.endswith(SUFFIX)):
            continue
        backup_id = name.removeprefix(BACKUP_PREFIX).removesuffix(SUFFIX)
        if not _ID_RE.fullmatch(backup_id) or not entry.is_file(follow_symlinks=False):
            continue
        try:
            size = entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        found.append(BackupInfo(backup_id=backup_id, path=Path(entry.path), size_bytes=size))
    found.sort(key=lambda info: info.backup_id, reverse=True)
    return found


def prune(layout: Layout, keep: int = KEEP_BACKUPS) -> None:
    """普段のバックアップを新しい順に `keep` 個残し、古いものを消す（3.5）。

    サンプルのバックアップは、初めの状態に戻すただ 1 つの手がかりなので消さない。
    """
    if keep < 1:
        raise ValueError("残す数は 1 以上にします")
    for info in list_backups(layout)[keep:]:
        try:
            info.path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            raise BackupFailed() from None


# --- 戻す ---


def _restore_targets(
    layout: Layout, include_sample: bool, env: Mapping[str, str] | None
) -> dict[str, _Source]:
    targets = {source.root: source for source in _sources(layout, env)}
    if not include_sample or not layout.sample_exists():
        # サンプルのデータは指定のときだけ。作業コピーがなければ作らない
        del targets[SAMPLE_DATA_ROOT]
    return targets


def _validate_members(
    members: list[tarfile.TarInfo], excludes: Mapping[str, frozenset[str]]
) -> None:
    """中の名前と種類を確かめる。外に出るパス・リンク・特別なファイルがあれば拒む。

    取り直せる素材や声のキャッシュの名前も拒む（戻すと、今ある素材と重なる）。
    """
    for member in members:
        path = PurePosixPath(member.name)
        parts = path.parts
        if path.is_absolute() or not parts or ".." in parts or parts[0] not in excludes:
            raise RestoreFailed("バックアップの中に、戻せない名前があります")
        if len(parts) > 1 and parts[1] in excludes[parts[0]]:
            raise RestoreFailed("バックアップの中に、戻せない名前があります")
        if not (member.isreg() or member.isdir()):
            raise RestoreFailed("バックアップの中に、戻せない種類のファイルがあります")


def _extract(tar: tarfile.TarFile, members: list[tarfile.TarInfo], root: str, dest: Path) -> None:
    """`root` の下のものを `dest` に書く。権限は本人の分だけ残す。

    ディレクトリは書いている間は 0700 にしておき、最後に深いものから権限を付ける。
    `dest` そのものは 0700 のまま（後で素材を移し入れる）。
    """
    ensure_private_dir(dest)
    dir_modes: list[tuple[Path, int]] = []
    for member in members:
        parts = PurePosixPath(member.name).parts
        if parts[0] != root or len(parts) == 1:
            continue
        target = dest.joinpath(*parts[1:])
        mode = stat.S_IMODE(member.mode) & 0o700
        if member.isdir():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            dir_modes.append((target, mode or 0o700))
            continue
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        source = tar.extractfile(member)
        if source is None:
            raise RestoreFailed()
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out, source:
            shutil.copyfileobj(source, out)
        os.chmod(target, mode or 0o600)
    for directory, mode in sorted(dir_modes, key=lambda item: len(item[0].parts), reverse=True):
        os.chmod(directory, mode)


def _old_path(source: _Source) -> Path:
    return source.path.with_name(source.path.name + _OLD_SUFFIX)


def _staging_path(source: _Source) -> Path:
    return source.path.with_name(source.path.name + _STAGING_SUFFIX)


def _move_excluded(source: _Source, old: Path) -> None:
    """除いた素材（voices など）を、残りのディレクトリから元の場所へ移す。

    元の場所にもう同じ名前があれば移さない。移せなければ `RestoreFailed`。
    そのときも、素材は残りのディレクトリに置いたままにする（消さない）。
    """
    for name in sorted(source.excludes):
        kept = old / name
        if not os.path.lexists(kept) or os.path.lexists(source.path / name):
            continue
        try:
            os.rename(kept, source.path / name)
        except OSError:
            raise RestoreFailed("取り直せる素材を元の場所に戻せませんでした") from None


def _discard_old(source: _Source, old: Path) -> None:
    """残りのディレクトリを消す。元の場所にない素材が残っていれば消さない。"""
    for name in source.excludes:
        if os.path.lexists(old / name) and not os.path.lexists(source.path / name):
            raise RestoreFailed("取り直せる素材を元の場所に戻せませんでした")
    _remove_tree(old)


def _recover_leftover(source: _Source) -> None:
    """前の復元が途中で止まって残ったもの（`<名前>.restore-old`）を片付ける。

    元の場所がなければ、残りを元の場所に戻す。
    元の場所があれば（入れ替えた後に止まった）、元の場所にない素材を移してから残りを消す。
    どちらもできなければ `RestoreFailed`。
    """
    old = _old_path(source)
    if not os.path.lexists(old):
        return
    try:
        if not os.path.lexists(source.path):
            os.rename(old, source.path)
            return
        if not source.path.is_dir() or source.path.is_symlink():
            raise RestoreFailed("前の復元の残りを片付けられませんでした")
        _move_excluded(source, old)
        _discard_old(source, old)
    except OSError:
        raise RestoreFailed("前の復元の残りを片付けられませんでした") from None


def _swap(source: _Source, staging: Path) -> None:
    """書き終えた一時のディレクトリと、元の場所を入れ替える。

    1. 元の場所を `<名前>.restore-old` にする
    2. 一時のディレクトリを元の場所にする（できなければ 1 を戻す）
    3. 除いた素材を残りから移す。移せなければ残りを消さずに `RestoreFailed`
    4. 残りを消す
    1 と 2 の間で電源が切れても、次の `recover_interrupted_restore` か復元の始めに戻る。
    """
    _recover_leftover(source)
    target = source.path
    old = _old_path(source)
    had_old = os.path.lexists(target)
    if had_old:
        os.rename(target, old)
    try:
        os.rename(staging, target)
    except OSError:
        if had_old:
            os.rename(old, target)
        raise
    if had_old:
        _move_excluded(source, old)
        _discard_old(source, old)


def recover_interrupted_restore(layout: Layout, *, env: Mapping[str, str] | None = None) -> None:
    """途中で止まった復元の残りを片付ける。残りがなければ何もしない。

    取り直せる素材はバックアップにないので、残りから元の場所へ戻すまで消さない。
    サービスを起動する前に呼ぶ（元の場所がない状態で起動しないように）。失敗は `RestoreFailed`。
    """
    for source in _sources(layout, env):
        _recover_leftover(source)


def restore_backup(
    layout: Layout,
    backup: Path,
    *,
    include_sample: bool,
    env: Mapping[str, str] | None = None,
) -> None:
    """普段のバックアップの中身を元の場所に戻す（3.7）。

    サンプルのデータは `include_sample` のときだけ戻す。

    サービスを止める・起動するのは呼ぶ側。ここはファイルだけを戻す。
    始めに前の復元の残りを片付ける。次に中身をすべて確かめ、
    置き場所ごとに一時のディレクトリへ書き終えてから入れ替える。
    書いている途中で失敗したら、一時のディレクトリを消し、どこも入れ替えない。
    バックアップになかった置き場所には触れない。取り直せる素材は消さない。失敗は `RestoreFailed`。
    """
    recover_interrupted_restore(layout, env=env)
    targets = _restore_targets(layout, include_sample, env)
    excludes = {source.root: source.excludes for source in _sources(layout, env)}
    staged: list[tuple[_Source, Path]] = []
    try:
        if not backup.is_file():
            raise RestoreFailed("バックアップのファイルがありません")
        with tarfile.open(backup, "r:gz") as tar:
            members = tar.getmembers()
            _validate_members(members, excludes)
            present = {PurePosixPath(m.name).parts[0] for m in members}
            for root, source in targets.items():
                if root not in present:
                    continue
                if source.path.is_symlink():
                    raise RestoreFailed("戻す先が記号のリンクです")
                staging = _staging_path(source)
                _remove_tree(staging)
                staged.append((source, staging))
                _extract(tar, members, root, staging)
        while staged:
            source, staging = staged[0]
            _swap(source, staging)
            staged.pop(0)
    except RestoreFailed:
        raise
    except (OSError, tarfile.TarError, EOFError):
        raise RestoreFailed() from None
    finally:
        for _source, staging in staged:
            _remove_tree(staging)


def _remove_tree(path: Path) -> None:
    """ディレクトリを消す。書けないディレクトリがあっても消せるように、先に 0700 にする。"""
    if not os.path.lexists(path):
        return
    if path.is_dir() and not path.is_symlink():
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
        for directory, subdirs, _files in os.walk(path):
            for name in subdirs:
                child = os.path.join(directory, name)
                if not os.path.islink(child):
                    with contextlib.suppress(OSError):
                        os.chmod(child, 0o700)
    shutil.rmtree(path, ignore_errors=True)
