"""基盤が書くファイルと、道具の読み取りを、同じ見本でつなぐ結合テスト。

要件（device-deployment）: 2.1、2.2、6.8、7.1。設計: Testing Strategy > Integration Tests。

- 基盤が書く状態のファイル（status.json）を、道具の `service_status.read_status` が読む
- 道具が書く知らせ（notice.json）を、基盤の `Notice` と `NoticeBoard` が読む
- 基盤の `python -m osekkai --self-check --fake-hardware --json` の出力を、道具の
  `verify.parse_self_check` が読む

道具は osekkai を読み込まないので、形のずれはここで見つける。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Arbiter
from osekkai.core.config import MotionSettings
from osekkai.core.motion import MotionService
from osekkai.core.notice import NOTICE_FILE_NAME, Notice, NoticeBoard
from osekkai.core.status import STATUS_FILE_NAME, build_snapshot, write_snapshot
from osekkai.core.types import Priority, Resource
from rover_deploy.agent import _self_check_results
from rover_deploy.common import Running, Target
from rover_deploy.notices import send_reminder, version_notice, write_notice
from rover_deploy.service_status import judge, read_status
from rover_deploy.state import DeployState
from rover_deploy.verify import parse_self_check
from shared_fixtures import load_fixture

TAG = Target(kind="release", ref="v0.2.0", ref_kind="tag", sha="c" * 40)
SAMPLE = Target(kind="sample", ref=None, ref_kind=None, sha=None)
REPO_ROOT = Path(__file__).resolve().parents[2]


# --- 状態のファイル: 基盤が書き、道具が読む ---


def _core_status(tmp_path: Path, fakes: FakeDeviceSet, clock: FakeClock, *, busy: bool) -> Path:
    """基盤の部品（Arbiter・MotionService）から報告を作り、基盤の書き方で書く。"""
    motion = MotionService(fakes.servos, fakes.motors, MotionSettings(), clock)
    arbiter = Arbiter(clock, motion)
    arbiter.acquire("core.idle", Priority.IDLE, [Resource.VOICE])  # 基盤の内部は作業中にしない
    if busy:
        arbiter.acquire("patrol", Priority.INTERACTIVE, [Resource.DRIVE, Resource.DISPLAY])
        motion.drive(0, 30)
    snapshot = build_snapshot(
        arbiter=arbiter,
        motion=motion,
        version="v0.2.0-3-gabc1234",
        pid=4321,
        clock=clock,
        unavailable=["servos"],
        features=["patrol"],
    )
    path = tmp_path / "osekkai" / STATUS_FILE_NAME
    write_snapshot(path, snapshot)
    return path


def test_status_written_by_core_is_read_by_tool_as_driving_and_busy(
    tmp_path: Path, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    path = _core_status(tmp_path, fakes, clock, busy=True)

    # 見本と同じキーの集合で書かれている
    written = json.loads(path.read_text(encoding="utf-8"))
    assert set(written) == set(load_fixture("status_driving_busy.json"))

    status = read_status(path)
    assert status is not None
    assert status.pid == 4321
    assert status.version == "v0.2.0-3-gabc1234"
    assert status.heartbeat == clock.monotonic()
    assert status.driving is True
    assert dict(status.busy) == {"drive": "patrol", "display": "patrol"}
    assert status.unavailable == ("servos",)
    assert status.features == ("patrol",)

    verdict = judge(Running.CORE, status, 4321, now_monotonic=clock.monotonic())
    assert (verdict.ok, verdict.reason) == (False, "driving")


def test_status_written_by_core_when_idle_lets_tool_deploy(
    tmp_path: Path, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    path = _core_status(tmp_path, fakes, clock, busy=False)

    status = read_status(path)
    assert status is not None
    assert status.driving is False
    assert dict(status.busy) == {}  # 基盤の内部の持ち主（core.idle）は出さない

    verdict = judge(Running.CORE, status, 4321, now_monotonic=clock.monotonic() + 1.0)
    assert (verdict.ok, verdict.reason) == (True, "")
    # 心拍が 5 秒より古ければ確かめられない
    stale = judge(Running.CORE, status, 4321, now_monotonic=clock.monotonic() + 6.0)
    assert (stale.ok, stale.reason) == (False, "unknown")


# --- 知らせ: 道具が書き、基盤が読む ---


def test_version_notice_written_by_tool_matches_fixture_and_is_read_by_core(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "osekkai"
    fixture = load_fixture("notice_version.json")
    written_at = float(fixture["until"]) - 20.0

    write_notice(runtime_dir, version_notice(TAG, SAMPLE, written_at))

    path = runtime_dir / NOTICE_FILE_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == fixture
    assert Notice.from_dict(data) == Notice(
        rows=("v0.2.0", "ためし ふだん:さんぷる"), until=fixture["until"]
    )

    # 待機画面が読む口でも、期限の前は出て、期限の後は出ない
    clock = FakeClock(start=datetime.fromtimestamp(written_at, tz=UTC))
    board = NoticeBoard(path, clock)
    assert board.current() == Notice.from_dict(data)
    clock.advance(21.0)
    assert board.current() is None


def test_reminder_written_by_tool_matches_fixture_and_is_read_by_core(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "osekkai"
    state = DeployState(everyday=SAMPLE, current=TAG, trial_since=0.0)

    send_reminder(runtime_dir, state, 4 * 3600.0)

    path = runtime_dir / NOTICE_FILE_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == load_fixture("notice_reminder.json")
    board = NoticeBoard(path, FakeClock())
    notice = board.current()
    assert notice is not None and notice.until is None  # 消されるまで出し続ける


# --- self-check の JSON: 基盤が出し、道具が読む ---


def test_self_check_json_from_core_process_is_parsed_by_tool(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    for name, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_RUNTIME_DIR", "runtime"),
    ):
        (tmp_path / sub).mkdir(mode=0o700)
        env[name] = str(tmp_path / sub)

    result = subprocess.run(
        [sys.executable, "-m", "osekkai", "--self-check", "--fake-hardware", "--json"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    data = json.loads(result.stdout)  # 標準出力は JSON だけ（記録は標準エラー）
    assert set(data) == set(load_fixture("selfcheck_ok.json"))
    expected_failed = tuple(item["name"] for item in data["results"] if item["status"] == "failed")
    assert parse_self_check(result.stdout) == expected_failed
    assert result.returncode == (1 if expected_failed else 0)
    names = {item["name"] for item in data["results"]}
    assert {"core.config", "core.lcd", "core.servos"} <= names

    # 実機の道具の self-check のサブコマンドも、同じ出力を項目の一覧として受け取る
    items = _self_check_results(result.stdout, result.returncode)
    assert items == [
        {"name": item["name"], "status": item["status"], "reason": item["reason"]}
        for item in data["results"]
    ]
