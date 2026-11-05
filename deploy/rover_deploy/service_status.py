"""動いているサービスの判定と、入れ替えてよいかの判断（2.1、2.2、2.3、2.5）。

実機で動いているサービスを systemctl から求め、新しい基盤なら状態のファイル（status.json）を読む。
走行中・作業中・確かめられないときは、入れ替えを始めない。時刻（時間帯）は見ない。

状態のファイルの形は、基盤の `osekkai.core.status.ServiceSnapshot` と同じにする。
道具は osekkai を読み込まないので、ここで同じ形を読む。形を変えるときは両方を直し、
共通の見本（tests/fixtures/）で確かめる。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from rover_deploy.common import Running, read_json_file
from rover_deploy.layout import CORE_UNIT_NAME, SAMPLE_UNIT_NAME, Layout
from rover_deploy.system import CommandError, Runner, is_active, show_unit

STATUS_SCHEMA = 1
STATUS_FILE_NAME = "status.json"
STATUS_STALE_S = 5.0  # これより古い心拍は、確かめられないとする
# 心拍が今より先にあってよい幅。同じ機械の単調な時計なので、ふつうは先にならない
FUTURE_TOLERANCE_S = 1.0

_KEYS = frozenset(
    {"schema", "pid", "version", "heartbeat", "driving", "busy", "unavailable", "features"}
)

Reason = Literal["", "driving", "busy", "unknown", "sample_running"]


@dataclass(frozen=True)
class StatusSnapshot:
    """状態のファイルの中身。基盤の ServiceSnapshot と同じ形。"""

    pid: int
    version: str
    heartbeat: float  # 基盤の単調な時計（CLOCK_MONOTONIC）の値
    driving: bool
    busy: Mapping[str, str] = field(default_factory=dict)  # 資源の値 → 持ち主（機能の名前）
    unavailable: tuple[str, ...] = ()
    features: tuple[str, ...] = ()


@dataclass(frozen=True)
class SafetyVerdict:
    """入れ替えてよいかの判断。"""

    ok: bool
    running: Running
    reason: Reason
    busy: tuple[str, ...] = ()  # 作業中の機能の名前（2.2）


def _is_int(value: object) -> bool:
    # bool は int の仲間なので除く
    return isinstance(value, int) and not isinstance(value, bool)


def _names(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


def parse_status(data: object) -> StatusSnapshot | None:
    """状態のファイルの辞書を読む。形が違えば `None`。"""
    if not isinstance(data, dict) or set(data) != _KEYS:
        return None
    schema = data["schema"]
    pid = data["pid"]
    version = data["version"]
    heartbeat = data["heartbeat"]
    driving = data["driving"]
    busy = data["busy"]
    if not _is_int(schema) or schema != STATUS_SCHEMA:
        return None
    if not _is_int(pid) or pid <= 0:
        return None
    if not isinstance(version, str):
        return None
    if not (_is_int(heartbeat) or isinstance(heartbeat, float)):
        return None
    if not isinstance(driving, bool):
        return None
    if not isinstance(busy, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in busy.items()
    ):
        return None
    unavailable = _names(data["unavailable"])
    features = _names(data["features"])
    if unavailable is None or features is None:
        return None
    return StatusSnapshot(
        pid=pid,
        version=version,
        heartbeat=float(heartbeat),
        driving=driving,
        busy=dict(busy),
        unavailable=unavailable,
        features=features,
    )


def read_status(path: Path) -> StatusSnapshot | None:
    """状態のファイルを読む。ない・読めない・形が違えば `None`。"""
    try:
        data = read_json_file(path)
    except (OSError, ValueError):
        return None
    if data is None:
        return None
    return parse_status(data)


def status_path(layout: Layout) -> Path:
    return layout.runtime_dir / STATUS_FILE_NAME


def service_reporting(
    snapshot: StatusSnapshot | None, main_pid: int | None, *, now_monotonic: float
) -> bool:
    """基盤が今、状態を報告しているか。

    状態のファイルがあり、pid が MainPID と同じで、心拍が新しいとき True。
    入れた後の確認でも、サービスが報告を始めるのを待つのに使う。
    """
    if snapshot is None or main_pid is None or snapshot.pid != main_pid:
        return False
    age = now_monotonic - snapshot.heartbeat
    return -FUTURE_TOLERANCE_S <= age <= STATUS_STALE_S


def judge(
    running: Running,
    snapshot: StatusSnapshot | None,
    main_pid: int | None,
    *,
    now_monotonic: float,
) -> SafetyVerdict:
    """入れ替えてよいかを決める。時刻（時間帯）は見ない（2.5）。"""
    if running is Running.NONE:
        return SafetyVerdict(ok=True, running=running, reason="")
    if running is not Running.CORE:
        # サンプルは状態を出さないので確かめられない（2.3）
        return SafetyVerdict(ok=False, running=running, reason="sample_running")
    if snapshot is None or not service_reporting(snapshot, main_pid, now_monotonic=now_monotonic):
        return SafetyVerdict(ok=False, running=running, reason="unknown")
    if snapshot.driving:
        return SafetyVerdict(ok=False, running=running, reason="driving")
    if snapshot.busy:
        owners = tuple(sorted(set(snapshot.busy.values())))
        return SafetyVerdict(ok=False, running=running, reason="busy", busy=owners)
    return SafetyVerdict(ok=True, running=running, reason="")


def detect_running(run: Runner) -> Running:
    """どのサービスが動いているかを求める。分からなければ `CommandError` の仲間を投げる。"""
    core = is_active(run, CORE_UNIT_NAME)
    sample = is_active(run, SAMPLE_UNIT_NAME)
    if core and sample:
        return Running.BOTH
    if core:
        return Running.CORE
    if sample:
        return Running.SAMPLE
    return Running.NONE


def core_main_pid(run: Runner) -> int | None:
    """新しい基盤の MainPID。動いていないか読めなければ `None`。"""
    try:
        return show_unit(run, CORE_UNIT_NAME).main_pid
    except CommandError:
        return None


def check_safety(
    run: Runner, layout: Layout, *, now_monotonic: float | None = None
) -> SafetyVerdict:
    """実機で、今入れ替えてよいかを調べる。

    動いているかが分からないときは、止まっているとみなさず、確かめられないとする（2.3）。
    心拍は、同じ機械の単調な時計（time.monotonic）と比べる。
    """
    try:
        running = detect_running(run)
    except CommandError:
        # どちらが動いているか分からないので、両方が動いているかもしれないとして止める
        return SafetyVerdict(ok=False, running=Running.BOTH, reason="unknown")
    if running is not Running.CORE:
        return judge(running, None, None, now_monotonic=_now(now_monotonic))
    main_pid = core_main_pid(run)
    snapshot = read_status(status_path(layout))
    return judge(running, snapshot, main_pid, now_monotonic=_now(now_monotonic))


def _now(now_monotonic: float | None) -> float:
    return time.monotonic() if now_monotonic is None else now_monotonic
