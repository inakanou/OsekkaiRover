"""状態のファイル（status.json）を書く（device-deployment 2.1〜2.3、6.7）。

動いているサービスの状態（版・pid・心拍・走行中・作業中・使えないデバイス・開始した機能）を、
外の道具が読めるように `Paths.runtime_dir / "status.json"` に 1 秒ごとに書く。
道具は、走行中や作業中なら入れ替えを止め、self-check はハードの項目をこれで確かめる。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from osekkai.core.display import FailureLog
from osekkai.core.paths import ensure_private_dir
from osekkai.core.types import Priority, Resource

if TYPE_CHECKING:
    from osekkai.core.arbiter import Arbiter
    from osekkai.core.clock import Clock
    from osekkai.core.motion import MotionService

logger = logging.getLogger(__name__)

STATUS_SCHEMA: Final = 1
STATUS_FILE_NAME: Final = "status.json"
STATUS_THREAD_NAME: Final = "osekkai-status"
STATUS_INTERVAL_S: Final = 1.0
# 基盤の内部の持ち主（待機画面など）は作業中に含めない
INTERNAL_OWNER_PREFIX: Final = "core."

_KEYS: Final = frozenset(
    {"schema", "pid", "version", "heartbeat", "driving", "busy", "unavailable", "features"}
)


class SnapshotUnreadable(ValueError):
    """状態のファイルの形が違って読めない。中身は持たない。"""


@dataclass(frozen=True)
class ServiceSnapshot:
    """動いているサービスの報告。JSON にしたものが状態のファイル。"""

    pid: int
    version: str
    heartbeat: float  # Clock.monotonic()
    driving: bool  # MotionService.driving
    busy: Mapping[str, str] = field(default_factory=dict)  # 資源の値 → 持ち主
    unavailable: tuple[str, ...] = ()  # 起動のときに使えなかったデバイスの名前
    features: tuple[str, ...] = ()  # 開始した機能の名前
    schema: int = STATUS_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "pid": self.pid,
            "version": self.version,
            "heartbeat": self.heartbeat,
            "driving": self.driving,
            "busy": dict(self.busy),
            "unavailable": list(self.unavailable),
            "features": list(self.features),
        }

    @classmethod
    def from_dict(cls, data: object) -> ServiceSnapshot:
        """形を確かめて読む。違えば `SnapshotUnreadable`。"""
        if not isinstance(data, dict) or set(data) != _KEYS:
            raise SnapshotUnreadable("キーが違います")
        schema = data["schema"]
        if not _is_int(schema) or schema != STATUS_SCHEMA:
            raise SnapshotUnreadable("schema が違います")
        pid = data["pid"]
        version = data["version"]
        heartbeat = data["heartbeat"]
        driving = data["driving"]
        busy = data["busy"]
        if not _is_int(pid) or pid <= 0:
            raise SnapshotUnreadable("pid の形が違います")
        if not isinstance(version, str):
            raise SnapshotUnreadable("version の形が違います")
        if not (_is_int(heartbeat) or isinstance(heartbeat, float)):
            raise SnapshotUnreadable("heartbeat の形が違います")
        if not isinstance(driving, bool):
            raise SnapshotUnreadable("driving の形が違います")
        if not isinstance(busy, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in busy.items()
        ):
            raise SnapshotUnreadable("busy の形が違います")
        return cls(
            schema=schema,
            pid=pid,
            version=version,
            heartbeat=float(heartbeat),
            driving=driving,
            busy=dict(busy),
            unavailable=_names(data["unavailable"], "unavailable"),
            features=_names(data["features"], "features"),
        )


def _is_int(value: object) -> bool:
    # bool は int の仲間なので除く
    return isinstance(value, int) and not isinstance(value, bool)


def _names(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SnapshotUnreadable(f"{key} の形が違います")
    return tuple(value)


def snapshot_to_json(snapshot: ServiceSnapshot) -> str:
    return json.dumps(snapshot.to_dict(), ensure_ascii=False)


def snapshot_from_json(text: str) -> ServiceSnapshot:
    """JSON の文字列から読む。JSON でないか形が違えば `SnapshotUnreadable`。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise SnapshotUnreadable("JSON ではありません") from error
    return ServiceSnapshot.from_dict(data)


def write_snapshot(path: Path, snapshot: ServiceSnapshot) -> None:
    """0600 の一時ファイルに書いてから置き換える。失敗したら例外を投げる。

    置き場所は実行時のディレクトリ（ふつうはメモリの上）で、毎秒書くので fsync はしない。
    """
    text = snapshot_to_json(snapshot)
    directory = path.parent
    ensure_private_dir(directory)
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def read_snapshot(path: Path) -> ServiceSnapshot | None:
    """状態のファイルを読む。ない・読めない・形が違う → `None`。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return snapshot_from_json(text)
    except SnapshotUnreadable:
        return None


def busy_resources(arbiter: Arbiter) -> dict[str, str]:
    """資源の値 → 持ち主。基盤の内部の持ち主（`core.` で始まる）と、見守りの帯
    （`Priority.BACKGROUND` 以下）で走行のほかの資源を借りている機能は除く。

    見守りの機能（見つけてあいさつなど）はいつも首などを借りているが、中断を知らせれば
    すぐ返すので、入れ替えを断る「作業中」には数えない。走行の資源は、安全のため
    どの帯でも数える。
    """
    busy: dict[str, str] = {}
    for resource in Resource:
        lease = arbiter.lease_for(resource)
        if lease is None or lease.owner.startswith(INTERNAL_OWNER_PREFIX):
            continue
        if lease.priority <= Priority.BACKGROUND and resource is not Resource.DRIVE:
            continue
        busy[resource.value] = lease.owner
    return busy


def build_snapshot(
    *,
    arbiter: Arbiter,
    motion: MotionService,
    version: str,
    pid: int,
    clock: Clock,
    unavailable: Iterable[str],
    features: Iterable[str],
) -> ServiceSnapshot:
    """今の状態から報告を作る。Runtime は、これを包んだ関数を StatusReporter に渡す。"""
    return ServiceSnapshot(
        pid=pid,
        version=version,
        heartbeat=clock.monotonic(),
        driving=motion.driving,
        busy=busy_resources(arbiter),
        unavailable=tuple(unavailable),
        features=tuple(features),
    )


class StatusReporter:
    """状態のファイルを `interval` 秒ごとに書くスレッド。

    書けないときは最初の 1 回だけ警告して続ける（基盤の異常にしない）。
    止めるとファイルを消し、その後は書かない。1 回だけ使う。
    待つのは止める合図の `threading.Event` で、テストでは `write_once()` で 1 回ずつ進める。
    """

    def __init__(
        self,
        path: Path,
        snapshot: Callable[[], ServiceSnapshot],
        *,
        interval: float = STATUS_INTERVAL_S,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self._path = path
        self._snapshot = snapshot
        self.interval = interval
        self._lock = threading.Lock()
        self._failures = FailureLog(logger)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def write_once(self) -> bool:
        """1 回書く。書けたら真。止めた後は書かない。"""
        with self._lock:
            if self._stop.is_set():
                return False
            try:
                write_snapshot(self._path, self._snapshot())
            except Exception as error:
                self._failures.failed("状態のファイルを書けませんでした: %s", type(error).__name__)
                return False
            self._failures.succeeded()
            return True

    def start(self) -> None:
        """すぐに 1 回書いてから、スレッドで書き続ける。"""
        if self._thread is not None or self._stop.is_set():
            return
        self.write_once()
        self._thread = threading.Thread(target=self._run, name=STATUS_THREAD_NAME, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """書くのをやめて、ファイルを消す。例外を外に出さない。"""
        self._stop.set()
        with self._lock:
            try:
                self._path.unlink(missing_ok=True)
            except OSError as error:
                logger.warning("状態のファイルを消せませんでした: %s", type(error).__name__)
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.write_once()
