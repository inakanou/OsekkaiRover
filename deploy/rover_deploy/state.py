"""道具の状態・入れ替えの記録・入れ替えのロック（1.7、4.6、5.2、10.2、10.3）。

置き場所は `~/.local/state/osekkai-deploy/`（0700）。
状態は 0600 で、一時ファイルからの置き換えで書く。
記録には、手順の名前・固定の理由・例外の型の名前だけを書く。外のコマンドの出力や設定の値は書かない。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Literal, get_args

from rover_deploy.common import (
    DeployResult,
    Outcome,
    Target,
    dump_json,
    read_json_file,
    result_from_dict,
    result_to_dict,
    target_from_dict,
    target_to_dict,
    write_json_file,
)
from rover_deploy.layout import Layout, ensure_private_dir

STATE_SCHEMA = 1
KEEP_LOGS = 50  # 記録は最新 50 件を残す
LOG_SUFFIX = ".jsonl"

EventStatus = Literal["start", "ok", "failed", "skipped"]

_DEPLOY_ID_RE = re.compile(r"\d{8}T\d{6}Z")
_STEP_RE = re.compile(r"[a-z][a-z0-9_.-]*")


# --- 入れ替えの識別子 ---


def make_deploy_id(epoch: float) -> str:
    """入れ替えの識別子を作る。UTC の時刻で "20261104T210000Z" の形。"""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(epoch))


def validate_deploy_id(deploy_id: str) -> str:
    """入れ替えの識別子の形か確かめる（パスの外に出ないようにする）。だめなら `ValueError`。"""
    if not _DEPLOY_ID_RE.fullmatch(deploy_id):
        raise ValueError("入れ替えの識別子の形が違います")
    return deploy_id


# --- 道具の状態 ---


class StateBroken(ValueError):
    """状態のファイルが読めない。中身は写さない。"""


@dataclass(frozen=True)
class InProgress:
    """途中の入れ替え。終わったら消す。"""

    deploy_id: str
    previous: Target | None  # 始める前に動いていた版


@dataclass(frozen=True)
class LastDeploy:
    """最後の入れ替え。"""

    deploy_id: str
    outcome: Outcome
    finished_at: float


@dataclass(frozen=True)
class DeployState:
    """道具の状態（state.json）。"""

    everyday: Target | None = None  # 普段の版
    current: Target | None = None  # 今の版
    previous: Target | None = None  # 前の版
    trial_since: float | None = None  # 試しの版を始めた時刻
    reminded_at: float | None = None  # 戻し忘れを知らせた時刻
    sample_saved: str | None = None  # サンプルを保存したバックアップの識別子
    calibration_migrated: str | None = None  # 首の調整値の移し替えの結果
    in_progress: InProgress | None = None
    last_deploy: LastDeploy | None = None


def _target_or_none(value: Target | None) -> dict[str, object] | None:
    return None if value is None else target_to_dict(value)


def state_to_dict(state: DeployState) -> dict[str, object]:
    in_progress = state.in_progress
    last = state.last_deploy
    return {
        "schema": STATE_SCHEMA,
        "everyday": _target_or_none(state.everyday),
        "current": _target_or_none(state.current),
        "previous": _target_or_none(state.previous),
        "trial_since": state.trial_since,
        "reminded_at": state.reminded_at,
        "sample_saved": state.sample_saved,
        "calibration_migrated": state.calibration_migrated,
        "in_progress": None
        if in_progress is None
        else {
            "deploy_id": in_progress.deploy_id,
            "previous": _target_or_none(in_progress.previous),
        },
        "last_deploy": None
        if last is None
        else {
            "deploy_id": last.deploy_id,
            "outcome": last.outcome,
            "finished_at": last.finished_at,
        },
    }


def _target(data: Mapping[str, object], key: str) -> Target | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise StateBroken(f"{key} の形が違います")
    try:
        return target_from_dict(value)
    except ValueError as e:
        raise StateBroken(f"{key} の形が違います") from e


def _number(data: Mapping[str, object], key: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    # bool は int の仲間なので先に断る
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StateBroken(f"{key} の形が違います")
    return float(value)


def _text(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise StateBroken(f"{key} の形が違います")
    return value


def _dict(data: Mapping[str, object], key: str) -> dict[str, object] | None:
    value = data.get(key)
    if value is not None and not isinstance(value, dict):
        raise StateBroken(f"{key} の形が違います")
    return value


def state_from_dict(data: Mapping[str, object]) -> DeployState:
    """JSON の辞書から状態を作る。形が違えば `StateBroken`。ないキーは既定の値にする。"""
    if data.get("schema") != STATE_SCHEMA:
        raise StateBroken("状態のファイルの版が違います")
    in_progress_raw = _dict(data, "in_progress")
    in_progress = None
    if in_progress_raw is not None:
        deploy_id = _text(in_progress_raw, "deploy_id")
        if deploy_id is None:
            raise StateBroken("in_progress の形が違います")
        in_progress = InProgress(deploy_id=deploy_id, previous=_target(in_progress_raw, "previous"))
    last_raw = _dict(data, "last_deploy")
    last = None
    if last_raw is not None:
        deploy_id = _text(last_raw, "deploy_id")
        outcome = last_raw.get("outcome")
        finished_at = _number(last_raw, "finished_at")
        if deploy_id is None or outcome not in get_args(Outcome) or finished_at is None:
            raise StateBroken("last_deploy の形が違います")
        last = LastDeploy(
            deploy_id=deploy_id,
            outcome=outcome,  # type: ignore[arg-type]
            finished_at=finished_at,
        )
    return DeployState(
        everyday=_target(data, "everyday"),
        current=_target(data, "current"),
        previous=_target(data, "previous"),
        trial_since=_number(data, "trial_since"),
        reminded_at=_number(data, "reminded_at"),
        sample_saved=_text(data, "sample_saved"),
        calibration_migrated=_text(data, "calibration_migrated"),
        in_progress=in_progress,
        last_deploy=last,
    )


class StateStore:
    """道具の状態を読み書きする。"""

    def __init__(self, layout: Layout) -> None:
        self._layout = layout

    def default(self) -> DeployState:
        """状態のファイルがないときの状態。サンプルの作業コピーがあれば、普段の版はサンプル。"""
        if self._layout.sample_exists():
            return DeployState(everyday=Target(kind="sample", ref=None, ref_kind=None, sha=None))
        return DeployState()

    def load(self) -> DeployState:
        """状態を読む。なければ既定の状態。壊れていれば `StateBroken`。"""
        try:
            data = read_json_file(self._layout.state_file)
        except ValueError as e:
            raise StateBroken("状態のファイルが読めません") from e
        if data is None:
            return self.default()
        return state_from_dict(data)

    def save(self, state: DeployState) -> None:
        """状態を 0600 で、一時ファイルからの置き換えで書く。ディレクトリは 0700 で作る。"""
        ensure_private_dir(self._layout.state_dir)
        write_json_file(self._layout.state_file, state_to_dict(state))


# --- 入れ替えのロック（1.7） ---


class DeployLock:
    """入れ替えのロック。取れなければ待たずに断る。

    `flock` は開いたファイルごとに効くので、同じプロセスの別の `DeployLock` とも重ならない。
    プロセスが終われば、ロックは自然に外れる。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """ロックを取る。ほかが持っていれば `False`。"""
        if self._fd is not None:
            return True
        ensure_private_dir(self._path.parent)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        """ロックを外す。持っていなければ何もしない。"""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> bool:
        return self.try_acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


# --- 入れ替えの記録（4.6、10.3） ---


def exception_reason(fixed: str, error: BaseException) -> str:
    """固定の理由に、例外の型の名前だけを添える。例外の文言は写さない（10.3）。"""
    return f"{fixed}（{type(error).__name__}）"


def _log_path(log_dir: Path, deploy_id: str) -> Path:
    return log_dir / f"{validate_deploy_id(deploy_id)}{LOG_SUFFIX}"


class DeployLog:
    """1 回の入れ替えの記録（`log/<deploy_id>.jsonl`）。1 行 1 件で、最後の行が結果。

    作るときに記録のファイルを 0600 で作り、古い記録を消して最新 `keep` 件を残す。
    """

    def __init__(
        self,
        log_dir: Path,
        deploy_id: str,
        *,
        clock: Callable[[], float] = time.time,
        keep: int = KEEP_LOGS,
    ) -> None:
        self._path = _log_path(log_dir, deploy_id)
        self._clock = clock
        ensure_private_dir(log_dir)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        prune_logs(log_dir, keep)

    @property
    def path(self) -> Path:
        return self._path

    def _append(self, data: Mapping[str, object]) -> None:
        line = (dump_json(data) + "\n").encode("utf-8")
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def event(self, step: str, status: EventStatus, reason: str = "") -> None:
        """手順の経過を 1 行書く。`reason` は固定の理由（例外なら `exception_reason`）。"""
        if not _STEP_RE.fullmatch(step):
            raise ValueError("手順の名前の形が違います")
        if status not in get_args(EventStatus):
            raise ValueError("手順の状態の形が違います")
        self._append({"t": self._clock(), "step": step, "status": status, "reason": reason})

    def result(self, result: DeployResult) -> None:
        """最後の行に結果を書く。"""
        self._append({"t": self._clock(), "result": result_to_dict(result)})


def list_log_ids(log_dir: Path) -> list[str]:
    """記録の識別子を古い順に返す。識別子の形でないファイルは数えない。"""
    try:
        names = os.listdir(log_dir)
    except FileNotFoundError:
        return []
    ids = [
        name[: -len(LOG_SUFFIX)]
        for name in names
        if name.endswith(LOG_SUFFIX) and _DEPLOY_ID_RE.fullmatch(name[: -len(LOG_SUFFIX)])
    ]
    return sorted(ids)


def latest_log_id(log_dir: Path) -> str | None:
    ids = list_log_ids(log_dir)
    return ids[-1] if ids else None


def prune_logs(log_dir: Path, keep: int = KEEP_LOGS) -> None:
    """古い記録を消して、最新 `keep` 件を残す。"""
    ids = list_log_ids(log_dir)
    for deploy_id in ids[: max(len(ids) - keep, 0)]:
        with contextlib.suppress(FileNotFoundError):
            (log_dir / f"{deploy_id}{LOG_SUFFIX}").unlink()


def read_log_events(log_dir: Path, deploy_id: str) -> list[dict[str, object]]:
    """記録の行を読む。なければ空。読めない行（書きかけの行など）は読み飛ばす。"""
    path = _log_path(log_dir, deploy_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    events: list[dict[str, object]] = []
    for line in raw.splitlines():
        try:
            data = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            events.append(data)
    return events


def read_log_result(log_dir: Path, deploy_id: str) -> DeployResult | None:
    """記録の結果を読む。まだ結果の行がなければ `None`。"""
    for event in reversed(read_log_events(log_dir, deploy_id)):
        raw = event.get("result")
        if isinstance(raw, dict):
            try:
                return result_from_dict(raw)
            except ValueError:
                return None
    return None
