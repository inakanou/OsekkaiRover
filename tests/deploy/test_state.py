"""道具の状態・入れ替えの記録・ロックのテスト（1.7、4.6、5.2、10.2、10.3）。"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from rover_deploy.common import DeployResult, Target
from rover_deploy.layout import Layout
from rover_deploy.state import (
    KEEP_LOGS,
    STATE_SCHEMA,
    DeployLock,
    DeployLog,
    DeployState,
    InProgress,
    LastDeploy,
    StateBroken,
    StateStore,
    exception_reason,
    latest_log_id,
    list_log_ids,
    make_deploy_id,
    read_log_events,
    read_log_result,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
SAMPLE = Target(kind="sample", ref=None, ref_kind=None, sha=None)
BRANCH = Target(kind="release", ref="feat/x", ref_kind="branch", sha=SHA)
TAG = Target(kind="release", ref="v0.2.0", ref_kind="tag", sha=SHA)
DEPLOY_ID = "20261104T210000Z"


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(env={}, home=tmp_path, uid=1000)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- 入れ替えの識別子 ---


def test_入れ替えの識別子は_UTC_の時刻() -> None:
    # 2026-11-04 21:00:00 UTC
    assert make_deploy_id(1793826000.0) == DEPLOY_ID
    assert make_deploy_id(1793826000.9) == DEPLOY_ID


# --- 道具の状態（5.2） ---


def test_状態のファイルがなくサンプルがあれば普段の版はサンプル(layout: Layout) -> None:
    (layout.sample_dir / ".git").mkdir(parents=True)
    state = StateStore(layout).load()
    assert state == DeployState(everyday=SAMPLE)


def test_状態のファイルもサンプルもなければ普段の版はない(layout: Layout) -> None:
    state = StateStore(layout).load()
    assert state.everyday is None
    assert state == DeployState()


def test_状態を書いて読み戻せる(layout: Layout) -> None:
    store = StateStore(layout)
    state = DeployState(
        everyday=SAMPLE,
        current=BRANCH,
        previous=SAMPLE,
        trial_since=1793833200.0,
        reminded_at=1793840000.5,
        sample_saved=DEPLOY_ID,
        calibration_migrated="nothing_to_migrate",
        in_progress=InProgress(deploy_id=DEPLOY_ID, previous=TAG),
        last_deploy=LastDeploy(deploy_id=DEPLOY_ID, outcome="succeeded", finished_at=1793833260.0),
    )
    store.save(state)
    assert store.load() == state


def test_状態のファイルの形は設計の通り(layout: Layout) -> None:
    StateStore(layout).save(DeployState(everyday=SAMPLE, current=BRANCH))
    data = json.loads(layout.state_file.read_text(encoding="utf-8"))
    assert data["schema"] == STATE_SCHEMA == 1
    assert set(data) == {
        "schema",
        "everyday",
        "current",
        "previous",
        "trial_since",
        "reminded_at",
        "sample_saved",
        "calibration_migrated",
        "in_progress",
        "last_deploy",
    }
    assert data["everyday"] == {"kind": "sample", "ref": None, "ref_kind": None, "sha": None}
    assert data["current"]["sha"] == SHA
    assert data["previous"] is None


def test_書いた普段の版は_null_でもそのまま読む(layout: Layout) -> None:
    (layout.sample_dir / ".git").mkdir(parents=True)
    store = StateStore(layout)
    store.save(DeployState(everyday=None))
    assert store.load().everyday is None


def test_状態は_0600_でディレクトリは_0700(layout: Layout) -> None:
    StateStore(layout).save(DeployState())
    assert mode(layout.state_file) == 0o600
    assert mode(layout.state_dir) == 0o700


def test_状態は一時ファイルからの置き換えで書く(layout: Layout) -> None:
    store = StateStore(layout)
    store.save(DeployState(current=TAG))
    before = layout.state_file.stat().st_ino
    store.save(DeployState(current=BRANCH))
    assert layout.state_file.stat().st_ino != before
    assert [p.name for p in layout.state_dir.iterdir()] == ["state.json"]
    assert store.load().current == BRANCH


@pytest.mark.parametrize(
    "text",
    [
        "{こわれた",
        "[]",
        '{"schema": 2}',
        '{"schema": 1, "everyday": "sample"}',
        '{"schema": 1, "everyday": {"kind": "x"}}',
        '{"schema": 1, "trial_since": "きのう"}',
        '{"schema": 1, "trial_since": true}',
        '{"schema": 1, "in_progress": {"deploy_id": 1}}',
        '{"schema": 1, "last_deploy": {"deploy_id": "x", "outcome": "?", "finished_at": 1}}',
    ],
)
def test_壊れた状態はわかる例外にする(layout: Layout, text: str) -> None:
    layout.state_dir.mkdir(parents=True)
    layout.state_file.write_text(text, encoding="utf-8")
    with pytest.raises(StateBroken):
        StateStore(layout).load()


def test_状態の例外は中身を写さない(layout: Layout) -> None:
    layout.state_dir.mkdir(parents=True)
    layout.state_file.write_text('{"schema": 1, "sample_saved": ["ひみつ"]}', encoding="utf-8")
    with pytest.raises(StateBroken) as info:
        StateStore(layout).load()
    assert "ひみつ" not in str(info.value)


def test_足りないキーは既定の値にする(layout: Layout) -> None:
    layout.state_dir.mkdir(parents=True)
    layout.state_file.write_text('{"schema": 1, "current": null}', encoding="utf-8")
    assert StateStore(layout).load() == DeployState()


# --- ロック（1.7） ---


def test_ロックは二重に取れない(layout: Layout) -> None:
    first = DeployLock(layout.lock_file)
    second = DeployLock(layout.lock_file)
    assert first.try_acquire()
    assert not second.try_acquire()
    first.release()
    assert second.try_acquire()
    second.release()


def test_ロックのファイルは_0600_でディレクトリは_0700(layout: Layout) -> None:
    lock = DeployLock(layout.lock_file)
    assert lock.try_acquire()
    assert mode(layout.lock_file) == 0o600
    assert mode(layout.state_dir) == 0o700
    lock.release()


def test_別のプロセスが持っているとロックは取れない(layout: Layout) -> None:
    lock = DeployLock(layout.lock_file)
    assert lock.try_acquire()
    code = (
        "import sys; from rover_deploy.state import DeployLock; from pathlib import Path;"
        "sys.exit(0 if DeployLock(Path(sys.argv[1])).try_acquire() else 7)"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "deploy")}
    held = subprocess.run([sys.executable, "-c", code, str(layout.lock_file)], env=env)
    assert held.returncode == 7
    lock.release()
    free = subprocess.run([sys.executable, "-c", code, str(layout.lock_file)], env=env)
    assert free.returncode == 0


def test_ロックは_with_で使える(layout: Layout) -> None:
    with DeployLock(layout.lock_file) as got:
        assert got
        assert not DeployLock(layout.lock_file).try_acquire()
    assert DeployLock(layout.lock_file).try_acquire()


def test_ロックを二度取ろうとしても持ったまま(layout: Layout) -> None:
    lock = DeployLock(layout.lock_file)
    assert lock.try_acquire()
    assert lock.try_acquire()
    assert lock.held
    lock.release()
    lock.release()  # 2 回目は何もしない
    assert not lock.held


# --- 入れ替えの記録（4.6、10.3） ---


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def test_記録は_1_行_1_件で最後の行が結果(layout: Layout) -> None:
    log = DeployLog(layout.log_dir, DEPLOY_ID, clock=Clock(100.0))
    log.event("prepare", "start")
    log.event("prepare", "ok")
    log.event("check", "failed", "応答がない")
    result = DeployResult(DEPLOY_ID, "rolled_back", SAMPLE, ("core.lcd: 応答がない",))
    log.result(result)
    lines = (layout.log_dir / f"{DEPLOY_ID}.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines[:3]] == [
        {"t": 101.0, "step": "prepare", "status": "start", "reason": ""},
        {"t": 102.0, "step": "prepare", "status": "ok", "reason": ""},
        {"t": 103.0, "step": "check", "status": "failed", "reason": "応答がない"},
    ]
    last = json.loads(lines[3])
    assert last["t"] == 104.0
    assert last["result"]["outcome"] == "rolled_back"
    assert read_log_result(layout.log_dir, DEPLOY_ID) == result
    assert len(read_log_events(layout.log_dir, DEPLOY_ID)) == 4


def test_記録は_0600_でディレクトリは_0700(layout: Layout) -> None:
    DeployLog(layout.log_dir, DEPLOY_ID).event("prepare", "start")
    assert mode(layout.log_dir / f"{DEPLOY_ID}.jsonl") == 0o600
    assert mode(layout.log_dir) == 0o700
    assert mode(layout.state_dir) == 0o700


def test_記録の状態の値は決まったものだけ(layout: Layout) -> None:
    log = DeployLog(layout.log_dir, DEPLOY_ID)
    with pytest.raises(ValueError):
        log.event("prepare", "done")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        log.event("こんにちは みんな", "ok")


def test_記録の識別子は決まった形だけ(layout: Layout) -> None:
    with pytest.raises(ValueError):
        DeployLog(layout.log_dir, "../state")
    with pytest.raises(ValueError):
        read_log_events(layout.log_dir, "../state")


def test_例外は型の名前だけを理由にする() -> None:
    err = RuntimeError("ひみつのなまえ sk-xxxx")
    reason = exception_reason("切り替えに失敗", err)
    assert reason == "切り替えに失敗（RuntimeError）"
    assert "ひみつ" not in reason


def test_記録は最新_50_件を残す(layout: Layout) -> None:
    assert KEEP_LOGS == 50
    ids = [make_deploy_id(1793826000.0 + i * 60) for i in range(KEEP_LOGS + 3)]
    for deploy_id in ids:
        DeployLog(layout.log_dir, deploy_id).event("prepare", "start")
    assert list_log_ids(layout.log_dir) == ids[3:]
    assert latest_log_id(layout.log_dir) == ids[-1]


def test_記録の一覧はほかのファイルを数えない(layout: Layout) -> None:
    DeployLog(layout.log_dir, DEPLOY_ID).event("prepare", "start")
    (layout.log_dir / "memo.txt").write_text("x", encoding="utf-8")
    assert list_log_ids(layout.log_dir) == [DEPLOY_ID]


def test_記録がなければ空(layout: Layout) -> None:
    assert list_log_ids(layout.log_dir) == []
    assert latest_log_id(layout.log_dir) is None
    assert read_log_events(layout.log_dir, DEPLOY_ID) == []
    assert read_log_result(layout.log_dir, DEPLOY_ID) is None


def test_書きかけの最後の行は読み飛ばす(layout: Layout) -> None:
    log = DeployLog(layout.log_dir, DEPLOY_ID, clock=Clock(0.0))
    log.event("prepare", "start")
    with (layout.log_dir / f"{DEPLOY_ID}.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"t": 2, "step": "pre')
    assert read_log_events(layout.log_dir, DEPLOY_ID) == [
        {"t": 1.0, "step": "prepare", "status": "start", "reason": ""}
    ]
    assert read_log_result(layout.log_dir, DEPLOY_ID) is None
