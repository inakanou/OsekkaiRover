"""入れた後の確認（4.1、4.3）。

新しい基盤の版は、次を順に確かめる。
1. 起動を待つ（`ActiveState=active` まで）
2. 基盤が状態を報告し始めるのを待つ（status.json があり、pid が MainPID と同じで、心拍が新しい）。
   報告の前に self-check を行うと、self-check がロックを持っている間にサービスが 75 で終わり、
   起動し直さない。1 と 2 は合わせて `START_TIMEOUT_S` の中で待つ
3. 版の self-check（`--json`）を行い、だめの項目を集める
4. `ALIVE_S` の間、`ActiveState=active` のまま、MainPID と再起動の回数が変わらない
5. journal に、起動の後の版のログ（`版: <git describe>`）がある

サンプルは、動き続けることだけを確かめる。

失敗の項目の名前は固定の文字だけにする（self-check の理由・journal の文・コマンドの出力は写さない）:
- "start": 時間内に起動しなかった
- "status": 時間内に状態を報告しなかった（self-check は行わない）
- "self_check:<項目の名前>": self-check のだめの項目
- "self_check": self-check の結果が読めない・時間切れ・終了コードと結果が合わない
- "alive": 動き続けなかった（止まった・MainPID か再起動の回数が変わった・確かめられない）
- "version_log": journal に版のログがない・journal を読めない・版の名前を求められない

版のログの文言と self-check の `--json` の形は、基盤と同じものをここにも持つ（道具は osekkai を
読み込まない）。共通の見本（tests/fixtures/）で両方が同じであることを確かめる。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rover_deploy.layout import CORE_UNIT_NAME, SAMPLE_UNIT_NAME, Layout
from rover_deploy.service_status import core_main_pid, read_status, service_reporting, status_path
from rover_deploy.system import (
    CommandError,
    Runner,
    UnitInfo,
    git_describe,
    journal_since,
    show_unit,
)

START_TIMEOUT_S = 30.0  # 起動と、状態の報告の始まりを待つ時間
ALIVE_S = 30.0  # 動き続けることを確かめる時間
POLL_S = 1.0  # systemctl に聞き直す間隔
SELF_CHECK_TIMEOUT_S = 120.0  # 声の合成の項目だけで最大 40 秒かかる

# 基盤の osekkai.core.runtime.VERSION_LOG_PREFIX と同じ文言
VERSION_LOG_PREFIX = "版: "
SELFCHECK_SCHEMA = 1
_FAILED = "failed"
_STATUSES = frozenset({"ok", _FAILED, "skipped"})
# 項目の名前として受け付ける形（これ以外の文字は記録に写さない）
_CHECK_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")

FAILED_START = "start"
FAILED_STATUS = "status"
FAILED_SELF_CHECK = "self_check"
FAILED_ALIVE = "alive"
FAILED_VERSION_LOG = "version_log"


class Clock(Protocol):
    """単調な時計と、待つ口。テストでは偽物に差し替えて、本当には待たない。"""

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """本当の時計（time.monotonic と time.sleep）。"""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    failed: tuple[str, ...]  # "self_check:core.lcd"、"alive"、"version_log" など


def _report(failed: list[str]) -> VerifyReport:
    return VerifyReport(ok=not failed, failed=tuple(failed))


def _show(run: Runner, unit: str) -> UnitInfo | None:
    try:
        return show_unit(run, unit)
    except CommandError:
        return None


def _wait_active(run: Runner, unit: str, clock: Clock, deadline: float) -> UnitInfo | None:
    """`ActiveState=active` で MainPID があるまで待つ。時間内にならなければ `None`。"""
    while True:
        info = _show(run, unit)
        if info is not None and info.active_state == "active" and info.main_pid is not None:
            return info
        if clock.monotonic() >= deadline:
            return None
        clock.sleep(POLL_S)


def _wait_reporting(run: Runner, layout: Layout, clock: Clock, deadline: float) -> bool:
    """基盤が状態を報告し始めるまで待つ。"""
    path = status_path(layout)
    while True:
        if service_reporting(
            read_status(path), core_main_pid(run), now_monotonic=clock.monotonic()
        ):
            return True
        if clock.monotonic() >= deadline:
            return False
        clock.sleep(POLL_S)


def _stays_alive(run: Runner, unit: str, base: UnitInfo, clock: Clock) -> bool:
    """`ALIVE_S` の間、動いたままで、MainPID と再起動の回数が `base` と同じか。"""
    end = clock.monotonic() + ALIVE_S
    while True:
        info = _show(run, unit)
        if (
            info is None
            or info.active_state != "active"
            or info.main_pid != base.main_pid
            or info.n_restarts != base.n_restarts
        ):
            return False
        if clock.monotonic() >= end:
            return True
        clock.sleep(POLL_S)


def parse_self_check(text: str) -> tuple[str, ...] | None:
    """self-check の `--json` の出力から、だめの項目の名前を返す。形が違えば `None`。

    形は基盤の `osekkai.core.selfcheck.results_to_dict` と同じ（キーは schema・ok・results、
    各項目は name・status・reason）。理由（reason）は返さない。
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or set(data) != {"schema", "ok", "results"}:
        return None
    schema = data["schema"]
    if isinstance(schema, bool) or schema != SELFCHECK_SCHEMA:
        return None
    if not isinstance(data["ok"], bool) or not isinstance(data["results"], list):
        return None
    failed: list[str] = []
    for item in data["results"]:
        if not isinstance(item, dict) or set(item) != {"name", "status", "reason"}:
            return None
        name, status = item["name"], item["status"]
        if not isinstance(name, str) or not _CHECK_NAME_RE.fullmatch(name):
            return None
        if not isinstance(status, str) or status not in _STATUSES:
            return None
        if not isinstance(item["reason"], str):
            return None
        if status == _FAILED:
            failed.append(name)
    if data["ok"] == bool(failed):
        return None  # ok と項目が合わない
    return tuple(failed)


def _self_check(run: Runner, release_dir: Path) -> list[str]:
    """版の self-check を行い、失敗の項目を返す。"""
    argv = [
        str(release_dir / ".venv/bin/python"),
        "-m",
        "osekkai",
        "--self-check",
        "--json",
    ]
    try:
        result = run(argv, timeout=SELF_CHECK_TIMEOUT_S, cwd=release_dir)
    except CommandError:
        return [FAILED_SELF_CHECK]
    names = parse_self_check(result.text)
    # 終了コードは、だめがあれば 1、なければ 0（基盤の selfcheck.exit_code）
    if names is None or result.returncode != (1 if names else 0):
        return [FAILED_SELF_CHECK]
    return [f"{FAILED_SELF_CHECK}:{name}" for name in names]


def has_version_log(lines: list[str], expected_describe: str) -> bool:
    """journal の文に、`版: <expected_describe>` の 1 行があるか。

    基盤は journal には「<水準> <ロガーの名前>: <文>」の形で書くので、文そのものか、
    「: 」の後に文が来る行を探す。版の名前の続き（"-dirty" など）が付いた行は別の版とみなす。
    """
    message = VERSION_LOG_PREFIX + expected_describe
    return any(line == message or line.endswith(": " + message) for line in lines)


def _version_logged(run: Runner, release_dir: Path, expected: str | None, since: str) -> bool:
    try:
        name = git_describe(run, release_dir) if expected is None else expected
        lines = journal_since(run, CORE_UNIT_NAME, since)
    except CommandError:
        return False
    return has_version_log(lines, name)


def journal_since_arg(since_epoch: float) -> str:
    """journalctl の `--since` に渡す時刻（`@<エポック秒>`）。秒の端数は切り捨てて前に寄せる。"""
    return f"@{int(since_epoch)}"


def verify_release(
    release_dir: Path,
    run: Runner,
    clock: Clock,
    layout: Layout,
    *,
    since_epoch: float,
    expected_describe: str | None = None,
) -> VerifyReport:
    """新しい基盤の版を入れた後の確認（4.1、4.3）。

    呼ぶ側は、`osekkai.service` を起動する直前の壁時計の時刻（エポック秒）を `since_epoch` に渡す。
    `expected_describe` を渡さなければ、版のディレクトリで `git describe --tags --always --dirty`
    を行って求める（基盤と同じコマンド）。
    """
    deadline = clock.monotonic() + START_TIMEOUT_S
    base = _wait_active(run, CORE_UNIT_NAME, clock, deadline)
    if base is None:
        return _report([FAILED_START])
    if not _wait_reporting(run, layout, clock, deadline):
        # 報告の前に self-check を行うと、ロックを取り合ってサービスを止めてしまう
        return _report([FAILED_STATUS])
    failed = _self_check(run, release_dir)
    if not _stays_alive(run, CORE_UNIT_NAME, base, clock):
        failed.append(FAILED_ALIVE)
    if not _version_logged(run, release_dir, expected_describe, journal_since_arg(since_epoch)):
        failed.append(FAILED_VERSION_LOG)
    return _report(failed)


def verify_sample(run: Runner, clock: Clock) -> VerifyReport:
    """サンプルを入れた後の確認。動き続けることだけを確かめる。"""
    base = _wait_active(run, SAMPLE_UNIT_NAME, clock, clock.monotonic() + START_TIMEOUT_S)
    if base is None:
        return _report([FAILED_START])
    if not _stays_alive(run, SAMPLE_UNIT_NAME, base, clock):
        return _report([FAILED_ALIVE])
    return _report([])
