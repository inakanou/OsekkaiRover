"""試しに 1 つのタスクを動かすコマンドのテスト（10.1〜10.7）。

模擬のデバイス（`--fake-hardware`）と一時の XDG のディレクトリで動かす。待ちを短くするため、
短い時間の指定（`FAST_TIMINGS`）とおやすみの表示の秒数（`SLEEP_SHOW_S`）を差し替える。
"""

import argparse
import io
import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from routine_fixtures import routine_section, routine_yaml, task_dict

from osekkai.adapters.fake.console import build_console_set
from osekkai.core import motion
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.paths import Paths
from osekkai.core.ports import Devices
from osekkai.core.registry import FeatureRegistry
from osekkai.features.routine import trial
from osekkai.features.routine.config import TaskConfig
from osekkai.features.routine.records import TaskResult
from osekkai.features.routine.runner import Timings

CALL = "かおを あらおう"
PRAISE = "すごいね"
LCD = "ｶｵ ｱﾗｳ"
CALL_NAME = "てすとくん"
READY = "じゅんび してね"


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """短い時間の指定とおやすみの表示を、テストのために更に短くする。"""
    monkeypatch.setattr(trial, "FAST_TIMINGS", Timings(remind_s=lambda t: 0.05, later_s=0.05))
    monkeypatch.setattr(trial, "SLEEP_SHOW_S", 0.05)


def _write_config(tasks: list[dict[str, Any]], **fields: Any) -> None:
    paths = Paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    section = routine_section(tasks, call_name=CALL_NAME, **fields)
    paths.config_file.write_text(routine_yaml(section), encoding="utf-8")


def _wash(**fields: Any) -> dict[str, Any]:
    # 首を見回すのは実際の時間がかかるので、催促は既定でなしにする
    fields.setdefault("max_reminders", 0)
    return task_dict("wash", call=CALL, praise=PRAISE, lcd=LCD, **fields)


def _snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(directory.glob("*.json"))
        if p.is_file()
    }


def _routine_dir() -> Path:
    return Paths().state_dir_for("routine")


def _seed_records() -> Path:
    """本番の記録と星の控えを置く（星は 3 つ）。"""
    directory = _routine_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "records.json").write_text(
        json.dumps({"version": 1, "stars": 3, "days": {}}), encoding="utf-8"
    )
    (directory / "stars.json").write_text(json.dumps({"version": 1, "stars": 3}), encoding="utf-8")
    return directory


@pytest.fixture
def no_devices(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """デバイスを作ろうとしたら記録する（作らずに終える場面で使う）。"""
    built: list[str] = []

    def fake_build(*args: object, **kwargs: object) -> object:
        built.append("built")
        raise AssertionError("デバイスを作ってはいけない")

    monkeypatch.setattr(trial, "build_devices", fake_build)
    return built


# --- 動かす ---


def test_短い時間で押さなければスキップまで進み_記録と星のファイルが変わらない(
    xdg_dirs: dict[str, Path], quick: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config([_wash(max_reminders=1, notice="もうすぐだよ")])
    directory = _seed_records()
    before = _snapshot(directory)
    assert set(before) == {"records.json", "stars.json"}

    assert trial.main(["--try", "wash", "--fast", "--fake-hardware"]) == 0

    out, err = capsys.readouterr()
    assert out.strip().splitlines()[-1] == "スキップ"
    assert f"[声] {CALL}" in out  # 本番と同じ進み方で声かけした（10.1）
    assert _snapshot(directory) == before  # 記録も星も変わらない（10.2）
    # ログには、タスクの id と結果の種類だけを出す（11.4）
    assert "wash" in err and "skipped" in err
    for hidden in (CALL_NAME, CALL, PRAISE, LCD, "もうすぐだよ", "07:00", "7:00"):
        assert hidden not in err


class _PressingConsole(io.StringIO):
    """模擬のデバイスの端末の出力。決まった声が出たら、USR ボタンを 1 回押す。

    押すのは別のスレッドから（話している途中の呼び出しの中で押さない）。
    """

    def __init__(self, press_after: list[str]) -> None:
        super().__init__()
        self._press_after = list(press_after)
        self.buttons: Any = None
        self.presses: list[threading.Thread] = []

    def write(self, text: str) -> int:
        if self._press_after and text.startswith(f"[声] {self._press_after[0]}"):
            self._press_after.pop(0)
            thread = threading.Thread(target=self.buttons.press, args=("usr",), daemon=True)
            self.presses.append(thread)
            thread.start()
        return super().write(text)


@pytest.fixture
def pressing_console(monkeypatch: pytest.MonkeyPatch) -> _PressingConsole:
    """準備の言葉と声かけのあとに押す、模擬のデバイスを作る。"""
    # ほめるときのダンスとうなずきを短くする（見回しはそのまま。2 回の押下の間を空けるため）
    monkeypatch.setattr(motion, "DANCE_STEP", 0.01)
    monkeypatch.setattr(motion, "NOD_STEP", 0.01)
    console = _PressingConsole([READY, CALL])

    def fake_build(args: argparse.Namespace, paths: Paths, registry: FeatureRegistry) -> Devices:
        assert args.fake_hardware
        devices = build_console_set(time, out=console, inp=io.StringIO(""))
        console.buttons = devices.buttons
        return devices.to_devices()

    monkeypatch.setattr(trial, "build_devices", fake_build)
    return console


def test_準備のあとに押して声かけで押せばできたで終え_星は増やさずに今の数を見せる(
    xdg_dirs: dict[str, Path],
    quick: None,
    pressing_console: _PressingConsole,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config([_wash(ready=READY)])
    directory = _routine_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "records.json").write_text(
        json.dumps({"version": 1, "stars": 4, "days": {}}), encoding="utf-8"
    )
    (directory / "stars.json").write_text(json.dumps({"version": 1, "stars": 4}), encoding="utf-8")
    before = _snapshot(directory)

    assert trial.main(["--try", "wash", "--fake-hardware"]) == 0

    out = capsys.readouterr().out
    assert out.strip().splitlines()[-1] == trial.RESULT_LABELS[TaskResult.DONE]
    assert len(pressing_console.presses) == 2  # 準備と声かけで 1 回ずつ押した（10.1）
    shown = pressing_console.getvalue()
    lines = shown.splitlines()
    assert lines.index(f"[声] {READY}") < lines.index(f"[声] {CALL}")
    assert f"[声] {PRAISE}" in lines
    # ほめる表示は今の星の数のまま（試しでは増やさない。10.2）
    assert "ﾔｯﾀﾈ! ★4" in shown
    assert "★5" not in shown
    assert _snapshot(directory) == before  # 記録も星も変わらない（10.2）


def test_記録のファイルがなければ試しの後もないまま(
    xdg_dirs: dict[str, Path], quick: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config([_wash()])
    assert trial.main(["--try", "wash", "--fast", "--fake-hardware"]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "スキップ"
    directory = _routine_dir()
    assert not (directory / "records.json").exists()
    assert not (directory / "stars.json").exists()


def test_寝るタスクはおやすみの表示を見せて_静かな状態を残さない(
    xdg_dirs: dict[str, Path], quick: None, capsys: pytest.CaptureFixture[str]
) -> None:
    bed = task_dict(
        "bed", at="21:00", call="ねようね", praise="おやすみ", lcd="ﾈﾙ", sleep=True, max_reminders=0
    )
    _write_config([_wash(), bed], wake={"weekday": "06:30", "holiday": "07:30"})

    assert trial.main(["--try", "bed", "--fast", "--fake-hardware"]) == 0

    out, _ = capsys.readouterr()
    assert "ｵﾔｽﾐ" in out  # おやすみの表示まで見せた（10.3）
    assert out.strip().splitlines()[-1] == "スキップ"
    quiet_file = Paths().state_dir_for("core") / "quiet.json"
    if quiet_file.exists():
        assert json.loads(quiet_file.read_text(encoding="utf-8"))["quiet"] is False


def test_短い時間の指定は催促を_10_秒_あとでを_30_秒にする() -> None:
    task = TaskConfig.model_validate(task_dict("wash"))
    assert trial.FAST_TIMINGS.remind_s(task) == 10.0
    assert trial.FAST_TIMINGS.later_s == 30.0
    assert trial.timings_for(fast=True) is trial.FAST_TIMINGS
    normal = trial.timings_for(fast=False)
    assert normal.remind_s(task) == task.remind_min * 60.0


# --- 始めずに終える ---


@pytest.fixture
def held_lock(xdg_dirs: dict[str, Path]) -> Iterator[InstanceLock]:
    lock = InstanceLock(Paths().lock_file)
    assert lock.acquire()
    yield lock
    lock.release()


def test_ロボットが動いていれば理由を出して_75_で終え_デバイスを作らない(
    held_lock: InstanceLock, no_devices: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config([_wash()])
    assert trial.main(["--try", "wash", "--fake-hardware"]) == 75
    assert trial.LOCK_BUSY_MESSAGE in capsys.readouterr().err
    assert no_devices == []


def test_ない_id_ならタスクの_id_の一覧を出して_2_で終える(
    xdg_dirs: dict[str, Path], no_devices: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    bed = task_dict("brush", at="19:00", call="はみがき", praise="えらい", lcd="ﾊﾐｶﾞｷ")
    _write_config([_wash(), bed])
    assert trial.main(["--try", "nope", "--fake-hardware"]) == 2
    err = capsys.readouterr().err
    assert "wash" in err and "brush" in err
    assert no_devices == []
    # 試しの後はロックを放している
    probe = InstanceLock(Paths().lock_file)
    assert probe.acquire()
    probe.release()


def test_日課の設定の誤りなら理由を出して_1_で終える(
    xdg_dirs: dict[str, Path], no_devices: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config([task_dict("Bad-Id")])
    assert trial.main(["--try", "Bad-Id", "--fake-hardware"]) == 1
    err = capsys.readouterr().err
    assert "routine" in err
    assert CALL_NAME not in err
    assert no_devices == []


def test_設定ファイルを指定できる(
    xdg_dirs: dict[str, Path], tmp_path: Path, no_devices: list[str]
) -> None:
    config = tmp_path / "my.yaml"
    config.write_text(routine_yaml(routine_section([_wash()])), encoding="utf-8")
    # 指定したファイルの id を見る（ない id なので 2）
    assert trial.main(["--try", "nope", "--fake-hardware", "--config", str(config)]) == 2
    # 指定したときは、既定の場所に見本から作らない
    assert not Paths().config_file.exists()


def test_日課が無効なら理由を出して_1_で終える(
    xdg_dirs: dict[str, Path], no_devices: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config([_wash()], enabled=False)
    assert trial.main(["--try", "wash", "--fake-hardware"]) == 1
    assert trial.DISABLED_MESSAGE in capsys.readouterr().err


def test_python_m_で使い方を出せる() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "osekkai.features.routine", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    for flag in ("--try", "--fast", "--fake-hardware", "--config"):
        assert flag in result.stdout
