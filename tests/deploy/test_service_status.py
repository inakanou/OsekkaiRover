"""動いているサービスの判定と、入れ替えてよいかの判断のテスト（2.1、2.2、2.3、2.5）。"""

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fake_runner import FakeRunner

from rover_deploy import service_status
from rover_deploy.common import Running
from rover_deploy.layout import CORE_UNIT_NAME, SAMPLE_UNIT_NAME, Layout
from rover_deploy.service_status import (
    STATUS_FILE_NAME,
    STATUS_STALE_S,
    SafetyVerdict,
    StatusSnapshot,
    check_safety,
    detect_running,
    judge,
    parse_status,
    read_status,
    service_reporting,
)
from rover_deploy.system import CommandTimedOut, UnexpectedOutput
from shared_fixtures import FIXTURES_DIR, STATUS_FIXTURES, load_fixture

PID = 4321  # 見本の pid
BEAT = 1234.5  # 見本の心拍
FRESH = BEAT + 1.0

IS_ACTIVE = ("systemctl", "--user", "is-active")
SHOW = ("systemctl", "--user", "show")


def snapshot(name: str) -> StatusSnapshot:
    parsed = parse_status(load_fixture(name))
    assert parsed is not None
    return parsed


def idle() -> StatusSnapshot:
    return snapshot("status_idle.json")


def make_layout(tmp_path: Path) -> Layout:
    runtime = tmp_path / "run"
    return Layout(env={"XDG_RUNTIME_DIR": str(runtime)}, home=tmp_path / "home", uid=1000)


def write_status(layout: Layout, data: object) -> None:
    layout.runtime_dir.mkdir(parents=True, exist_ok=True)
    (layout.runtime_dir / STATUS_FILE_NAME).write_text(json.dumps(data), encoding="utf-8")


def script_units(
    runner: FakeRunner, *, core: str = "inactive", sample: str = "inactive", main_pid: int = PID
) -> None:
    runner.script(
        [*IS_ACTIVE, CORE_UNIT_NAME], returncode=0 if core == "active" else 3, stdout=core
    )
    runner.script(
        [*IS_ACTIVE, SAMPLE_UNIT_NAME], returncode=0 if sample == "active" else 3, stdout=sample
    )
    runner.script(SHOW, stdout=f"MainPID={main_pid}\nNRestarts=0\nActiveState={core}\n")


# --- status.json の読み取り（共通の見本） ---


@pytest.mark.parametrize("name", STATUS_FIXTURES)
def test_共通の見本の状態を読める(name: str) -> None:
    data = load_fixture(name)
    parsed = parse_status(data)
    assert parsed is not None
    assert parsed.pid == data["pid"]
    assert parsed.version == data["version"]
    assert parsed.heartbeat == data["heartbeat"]
    assert parsed.driving is data["driving"]
    assert dict(parsed.busy) == data["busy"]


def test_見本のファイルを読める(tmp_path: Path) -> None:
    path = tmp_path / STATUS_FILE_NAME
    path.write_bytes((FIXTURES_DIR / "status_driving_busy.json").read_bytes())
    parsed = read_status(path)
    assert parsed is not None
    assert parsed.driving is True
    assert dict(parsed.busy) == {"drive": "demo", "display": "demo"}


def _broken(**change: Any) -> dict[str, Any]:
    data = load_fixture("status_idle.json")
    for key, value in change.items():
        if value is _DROP:
            del data[key]
        else:
            data[key] = value
    return data


_DROP = object()


@pytest.mark.parametrize(
    "data",
    [
        _broken(schema=2),
        _broken(schema=True),
        _broken(schema=_DROP),
        _broken(extra=1),
        _broken(pid="4321"),
        _broken(pid=0),
        _broken(pid=True),
        _broken(version=1),
        _broken(heartbeat="1234.5"),
        _broken(heartbeat=True),
        _broken(driving=1),
        _broken(busy=[]),
        _broken(busy={"drive": 1}),
        _broken(unavailable="servos"),
        _broken(features=[1]),
        [],
        "status",
    ],
)
def test_形の違う状態は読めないとする(data: object) -> None:
    assert parse_status(data) is None


def test_ない_壊れた状態のファイルは読めないとする(tmp_path: Path) -> None:
    path = tmp_path / STATUS_FILE_NAME
    assert read_status(path) is None
    path.write_text("{", encoding="utf-8")
    assert read_status(path) is None
    path.write_bytes(b"\xff\xfe")
    assert read_status(path) is None
    directory = tmp_path / "dir"
    directory.mkdir()
    assert read_status(directory) is None


# --- 判断（judge） ---


def test_止まっていれば進めてよい() -> None:
    verdict = judge(Running.NONE, None, None, now_monotonic=FRESH)
    assert verdict == SafetyVerdict(ok=True, running=Running.NONE, reason="", busy=())


def test_静かな基盤なら進めてよい() -> None:
    verdict = judge(Running.CORE, idle(), PID, now_monotonic=FRESH)
    assert verdict == SafetyVerdict(ok=True, running=Running.CORE, reason="", busy=())


def test_状態のファイルがなければ確かめられない() -> None:
    verdict = judge(Running.CORE, None, PID, now_monotonic=FRESH)
    assert (verdict.ok, verdict.reason) == (False, "unknown")


def test_pid_が_MainPID_と違えば確かめられない() -> None:
    assert judge(Running.CORE, idle(), PID + 1, now_monotonic=FRESH).reason == "unknown"
    assert judge(Running.CORE, idle(), None, now_monotonic=FRESH).reason == "unknown"


def test_古い心拍は確かめられない() -> None:
    edge = judge(Running.CORE, idle(), PID, now_monotonic=BEAT + STATUS_STALE_S)
    assert edge.ok is True
    stale = judge(Running.CORE, idle(), PID, now_monotonic=BEAT + STATUS_STALE_S + 0.01)
    assert (stale.ok, stale.reason) == (False, "unknown")


def test_先の時刻の心拍は確かめられない() -> None:
    near = judge(Running.CORE, idle(), PID, now_monotonic=BEAT - 0.1)
    assert near.ok is True
    future = judge(Running.CORE, idle(), PID, now_monotonic=BEAT - 60.0)
    assert (future.ok, future.reason) == (False, "unknown")


def test_走行中なら中止する() -> None:
    running = snapshot("status_driving_busy.json")
    verdict = judge(Running.CORE, running, PID, now_monotonic=FRESH)
    # 走行中は作業中より先に出す
    assert verdict == SafetyVerdict(ok=False, running=Running.CORE, reason="driving", busy=())


def test_作業中なら機能の名前を出して中止する() -> None:
    data = load_fixture("status_idle.json")
    data["busy"] = {"voice": "talk", "drive": "demo", "display": "demo"}
    parsed = parse_status(data)
    verdict = judge(Running.CORE, parsed, PID, now_monotonic=FRESH)
    assert verdict == SafetyVerdict(
        ok=False, running=Running.CORE, reason="busy", busy=("demo", "talk")
    )


@pytest.mark.parametrize("running", [Running.SAMPLE, Running.BOTH])
def test_サンプルが動いていれば確かめられない(running: Running) -> None:
    verdict = judge(running, idle(), PID, now_monotonic=FRESH)
    assert verdict == SafetyVerdict(ok=False, running=running, reason="sample_running", busy=())


# --- 心拍の確かめ（入れた後の確認でも使う） ---


def test_報告している基盤を見分ける() -> None:
    assert service_reporting(idle(), PID, now_monotonic=FRESH) is True
    assert service_reporting(None, PID, now_monotonic=FRESH) is False
    assert service_reporting(idle(), None, now_monotonic=FRESH) is False
    assert service_reporting(idle(), PID + 1, now_monotonic=FRESH) is False
    assert service_reporting(idle(), PID, now_monotonic=BEAT + STATUS_STALE_S + 1) is False


# --- systemctl から求める ---


@pytest.mark.parametrize(
    ("core", "sample", "expected"),
    [
        ("inactive", "inactive", Running.NONE),
        ("active", "inactive", Running.CORE),
        ("inactive", "active", Running.SAMPLE),
        ("active", "active", Running.BOTH),
        ("activating", "failed", Running.CORE),
    ],
)
def test_動いているサービスを求める(
    fake_runner: FakeRunner, core: str, sample: str, expected: Running
) -> None:
    script_units(fake_runner, core=core, sample=sample)
    assert detect_running(fake_runner) is expected


def test_止まっていれば状態のファイルを見ずに進める(
    fake_runner: FakeRunner, tmp_path: Path
) -> None:
    script_units(fake_runner)
    verdict = check_safety(fake_runner, make_layout(tmp_path), now_monotonic=FRESH)
    assert verdict == SafetyVerdict(ok=True, running=Running.NONE, reason="", busy=())
    assert not any(argv[:3] == SHOW for argv in fake_runner.argvs)


def test_基盤の状態のファイルと_MainPID_で判断する(fake_runner: FakeRunner, tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    write_status(layout, load_fixture("status_driving_busy.json"))
    script_units(fake_runner, core="active")
    verdict = check_safety(fake_runner, layout, now_monotonic=FRESH)
    assert (verdict.running, verdict.reason) == (Running.CORE, "driving")


def test_MainPID_が違えば確かめられない(fake_runner: FakeRunner, tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    write_status(layout, load_fixture("status_idle.json"))
    script_units(fake_runner, core="active", main_pid=PID + 1)
    assert check_safety(fake_runner, layout, now_monotonic=FRESH).reason == "unknown"


def test_状態のファイルがなければ確かめられない_systemctl(
    fake_runner: FakeRunner, tmp_path: Path
) -> None:
    script_units(fake_runner, core="active")
    assert check_safety(fake_runner, make_layout(tmp_path), now_monotonic=FRESH).reason == "unknown"


def test_止まっているか分からなければ進めない(fake_runner: FakeRunner, tmp_path: Path) -> None:
    fake_runner.script([*IS_ACTIVE, CORE_UNIT_NAME], returncode=3, stdout="")
    fake_runner.script([*IS_ACTIVE, SAMPLE_UNIT_NAME], returncode=3, stdout="inactive")
    verdict = check_safety(fake_runner, make_layout(tmp_path), now_monotonic=FRESH)
    assert (verdict.ok, verdict.reason) == (False, "unknown")


def test_systemctl_が答えなければ進めない(fake_runner: FakeRunner, tmp_path: Path) -> None:
    fake_runner.raise_on(IS_ACTIVE, CommandTimedOut("systemctl"))
    verdict = check_safety(fake_runner, make_layout(tmp_path), now_monotonic=FRESH)
    assert (verdict.ok, verdict.reason) == (False, "unknown")


def test_MainPID_を読めなければ確かめられない(fake_runner: FakeRunner, tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    write_status(layout, load_fixture("status_idle.json"))
    fake_runner.script([*IS_ACTIVE, CORE_UNIT_NAME], stdout="active")
    fake_runner.script([*IS_ACTIVE, SAMPLE_UNIT_NAME], returncode=3, stdout="inactive")
    fake_runner.raise_on(SHOW, UnexpectedOutput("systemctl"))
    verdict = check_safety(fake_runner, layout, now_monotonic=FRESH)
    assert (verdict.running, verdict.ok, verdict.reason) == (Running.CORE, False, "unknown")


def test_既定では自分の単調な時計と比べる(
    fake_runner: FakeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = make_layout(tmp_path)
    write_status(layout, load_fixture("status_idle.json"))
    script_units(fake_runner, core="active")
    monkeypatch.setattr(time, "monotonic", lambda: FRESH)
    assert check_safety(fake_runner, layout).ok is True
    monkeypatch.setattr(time, "monotonic", lambda: BEAT + 600.0)
    assert check_safety(fake_runner, layout).reason == "unknown"


# --- 時刻を見ない（2.5） ---


@pytest.mark.parametrize(
    "wall",
    [
        0.0,
        1793833200.0,  # 夜中
        1793876400.0,  # 昼
        1793908800.0,  # 夜
    ],
)
def test_時刻によって判断を変えない(
    fake_runner: FakeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wall: float
) -> None:
    monkeypatch.setattr(time, "time", lambda: wall)
    layout = make_layout(tmp_path)
    write_status(layout, load_fixture("status_idle.json"))
    script_units(fake_runner, core="active")
    expected = SafetyVerdict(ok=True, running=Running.CORE, reason="", busy=())
    assert check_safety(fake_runner, layout, now_monotonic=FRESH) == expected
    assert judge(Running.NONE, None, None, now_monotonic=FRESH).ok is True


def test_道具は壁時計を読まない() -> None:
    source = Path(service_status.__file__).read_text(encoding="utf-8")
    for name in ("time.time", "datetime", "localtime", "strftime"):
        assert name not in source
