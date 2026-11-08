"""起動の口の self-check のテスト（device-deployment 6.1、6.2、6.3、6.7、6.8）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from osekkai import __main__ as cli
from osekkai.adapters.fake.probes import FakeProbes
from osekkai.core import Feature, FeatureConfig, FeatureContext, FeatureSpec, Resource
from osekkai.core.config import ConfigLoader, example_config_bytes
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.paths import Paths
from osekkai.core.registry import FeatureRegistry
from osekkai.core.selfcheck import (
    REASON_SERVICE,
    REASON_SKIPPED_BUSY,
    Check,
    CheckContext,
    CheckResult,
    CheckStatus,
)
from osekkai.core.status import STATUS_FILE_NAME, ServiceSnapshot, write_snapshot
from osekkai.voice.checks import REASON_TYPED
from shared_fixtures import load_fixture

WAIT_S = 30.0
BASE_NAMES = [
    "core.config",
    "core.storage",
    "core.timesync",
    "core.voice_synth",
    "core.audio_sink",
    "core.lcd",
    "core.hat",
    "core.buttons",
    "core.servos",
]
HARDWARE_NAMES = ["core.lcd", "core.hat", "core.buttons", "core.servos"]
# 登録した機能の項目（本物の機能の発見を使うサブプロセスのテストで、基盤の項目の後に
# 機能の名前の順に並ぶ）。開発 PC と CI には faster-whisper がないので、見本の設定
# （recognizer: auto）では音声の入口は打った文字の聞き取りになり、どちらも飛ばす
CONVERSATION_NAMES = ["conversation.setup"]
PRESENCE_NAMES = ["presence.camera", "presence.models", "presence.faces"]
ROUTINE_NAMES = ["routine.holidays", "routine.quiet_hours", "routine.records"]
VOICE_NAMES = ["voice.mic", "voice.model"]
ALL_NAMES = [*BASE_NAMES, *CONVERSATION_NAMES, *PRESENCE_NAMES, *ROUTINE_NAMES, *VOICE_NAMES]


def _place_config() -> None:
    """self-check は設定ファイルを作らないので、見本から置いておく。"""
    config = Paths().config_file
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(example_config_bytes())


@pytest.fixture
def probes(xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> FakeProbes:
    _place_config()
    fake = FakeProbes()
    monkeypatch.setattr(cli, "_build_probes", lambda fake_hardware: fake)
    # 基盤の項目だけを見るため、登録された機能（日課など）は読み込まない
    monkeypatch.setattr(cli, "FeatureRegistry", lambda: FeatureRegistry([]))
    return fake


@pytest.fixture
def held_lock(xdg_dirs: dict[str, Path]) -> Iterator[InstanceLock]:
    lock = InstanceLock(Paths().lock_file)
    assert lock.acquire()
    yield lock
    lock.release()


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out
    data: dict[str, Any] = json.loads(out)
    return data


def _by_name(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in data["results"]}


def _touched_hardware(probes: FakeProbes) -> bool:
    return bool(probes.i2c_queries or probes.gpio_queries)


def _demo_feature(*checks: Check) -> type[Feature]:
    class _Demo:
        spec: ClassVar[FeatureSpec] = FeatureSpec(
            name="demo", priority=30, resources=frozenset({Resource.DISPLAY}), checks=checks
        )
        started: ClassVar[int] = 0

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            type(self).started += 1

        def run(self, stop: threading.Event) -> None:
            stop.wait()

    return _Demo


def _ok(_ctx: CheckContext) -> CheckResult:
    return CheckResult(CheckStatus.OK)


def _failed(_ctx: CheckContext) -> CheckResult:
    return CheckResult(CheckStatus.FAILED, "準備ができていない")


# --- 形と終了コード ---


def test_JSON_は共通の見本と同じキーで_よければ_0(
    probes: FakeProbes, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 0
    data = _json(capsys)
    sample = load_fixture("selfcheck_ok.json")
    assert set(data) == set(sample)
    assert all(set(row) == set(sample["results"][0]) for row in data["results"])
    assert data["schema"] == sample["schema"]
    assert data["ok"] is True
    assert [row["name"] for row in data["results"]] == BASE_NAMES
    assert all(row["status"] == "ok" for row in data["results"])


def test_設定ファイルがなくても作らず_設定をだめにする(
    probes: FakeProbes, capsys: pytest.CaptureFixture[str]
) -> None:
    config = Paths().config_file
    config.unlink()
    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 1
    assert not config.exists()
    rows = _by_name(_json(capsys))
    assert rows["core.config"]["status"] == "failed"


def test_だめがあれば_1_で_JSON_の_ok_は偽(
    probes: FakeProbes, capsys: pytest.CaptureFixture[str]
) -> None:
    probes.answering.clear()  # LCD も HAT も応えない
    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 1
    data = _json(capsys)
    assert data["ok"] is False
    rows = _by_name(data)
    assert rows["core.lcd"]["status"] == "failed"
    assert rows["core.config"]["status"] == "ok"


def test_JSON_でなければ_1_項目_1_行で出す(
    probes: FakeProbes, capsys: pytest.CaptureFixture[str]
) -> None:
    probes.synchronized = False
    assert cli.main(["--self-check", "--fake-hardware"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(BASE_NAMES)
    assert lines[0] == "よい core.config"
    assert lines[2].startswith("だめ core.timesync ")


def test_ロックを取れればハードを確かめ_終わったら放す(
    probes: FakeProbes, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 0
    assert _touched_hardware(probes)
    lock = InstanceLock(Paths().lock_file)
    assert lock.acquire()  # 放してある
    lock.release()


def test_ハードを確かめている間はロックを持つ(
    xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _place_config()
    held: list[bool] = []

    class _Probing(FakeProbes):
        def i2c_responds(self, address: int) -> bool:
            probe = InstanceLock(Paths().lock_file)
            held.append(not probe.acquire())
            probe.release()
            return super().i2c_responds(address)

    monkeypatch.setattr(cli, "_build_probes", lambda fake_hardware: _Probing())
    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 0
    assert held and all(held)


def test_ハードなしならロックを取れてもハードの項目を飛ばす(
    probes: FakeProbes, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--self-check", "--fake-hardware", "--no-hardware", "--json"]) == 0
    assert not _touched_hardware(probes)
    rows = _by_name(_json(capsys))
    for name in HARDWARE_NAMES:
        assert rows[name] == {"name": name, "status": "skipped", "reason": REASON_SKIPPED_BUSY}
    assert rows["core.voice_synth"]["status"] == "ok"  # ハードでない項目は動かす


# --- 動いているサービスがあるとき（6.7） ---


def test_ロックが取られていればハードに触らずサービスの報告で判定する(
    probes: FakeProbes, held_lock: InstanceLock, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = ServiceSnapshot(
        pid=os.getpid(), version="v0.2.0", heartbeat=1.0, driving=False, unavailable=("display",)
    )
    write_snapshot(Paths().runtime_dir / STATUS_FILE_NAME, snapshot)
    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 1
    assert not _touched_hardware(probes)
    rows = _by_name(_json(capsys))
    assert rows["core.lcd"] == {"name": "core.lcd", "status": "failed", "reason": REASON_SERVICE}
    for name in ["core.hat", "core.buttons", "core.servos"]:
        assert rows[name] == {"name": name, "status": "ok", "reason": REASON_SERVICE}
    assert rows["core.storage"]["status"] == "ok"
    assert held_lock.held  # 動いている方のロックはそのまま


def test_ロックが取られていて状態のファイルがなければハードの項目を飛ばす(
    probes: FakeProbes, held_lock: InstanceLock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 0
    assert not _touched_hardware(probes)
    rows = _by_name(_json(capsys))
    assert [rows[name]["status"] for name in HARDWARE_NAMES] == ["skipped"] * 4


def test_ロックが取れても状態のファイルは読まない(
    probes: FakeProbes, capsys: pytest.CaptureFixture[str]
) -> None:
    # 前の起動の残りのファイルがあっても、ロックを取れたら自分で確かめる
    snapshot = ServiceSnapshot(
        pid=1, version="v0.2.0", heartbeat=1.0, driving=False, unavailable=("display",)
    )
    write_snapshot(Paths().runtime_dir / STATUS_FILE_NAME, snapshot)
    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 0
    assert _by_name(_json(capsys))["core.lcd"]["reason"] == ""


# --- 機能の項目（6.2） ---


def test_機能の項目を基盤の項目の後に並べ_機能は開始しない(
    probes: FakeProbes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    demo = _demo_feature(Check("demo.ready", _ok), Check("demo.arm", _failed))
    monkeypatch.setattr(cli, "FeatureRegistry", lambda: FeatureRegistry([demo]))
    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 1
    data = _json(capsys)
    assert [row["name"] for row in data["results"]] == [*BASE_NAMES, "demo.ready", "demo.arm"]
    rows = _by_name(data)
    assert rows["demo.ready"]["status"] == "ok"
    assert rows["demo.arm"] == {
        "name": "demo.arm",
        "status": "failed",
        "reason": "準備ができていない",
    }
    assert demo.started == 0  # type: ignore[attr-defined]


# --- 作らないもの・壊れないこと ---


def test_デバイスも_Runtime_も作らない(
    probes: FakeProbes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("作ってはいけない")

    monkeypatch.setattr(cli, "build_devices", _forbidden)
    monkeypatch.setattr(cli, "Runtime", _forbidden)
    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 0


def test_設定を読めなくても止まらず_設定の項目をだめにする(
    probes: FakeProbes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _broken(
        self: ConfigLoader, override: Path | None = None, *, create_missing: bool = True
    ) -> None:
        raise PermissionError("hidden-path")

    monkeypatch.setattr(ConfigLoader, "load", _broken)
    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 1
    out, err = capsys.readouterr()
    rows = _by_name(json.loads(out))
    assert rows["core.config"]["status"] == "failed"
    assert "hidden-path" not in out + err


def test_表示とログに設定の値を出さない(
    probes: FakeProbes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "my.yaml"
    config.write_text("core:\n  voice:\n    sink: himitsu-sink-42\n", encoding="utf-8")
    assert cli.main(["--self-check", "--fake-hardware", "--config", str(config)]) == 1
    out, err = capsys.readouterr()
    assert "だめ core.audio_sink" in out
    assert "himitsu-sink-42" not in out + err


def test_実機の確かめを読み込めなくてもハードでない項目は確かめる(
    xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _place_config()
    monkeypatch.setattr(cli, "PI_PROBES_MODULE", "osekkai.adapters.no_such_module")
    assert cli.main(["--self-check", "--json"]) == 1
    rows = _by_name(_json(capsys))
    assert rows["core.config"]["status"] == "ok"
    assert rows["core.storage"]["status"] == "ok"
    assert rows["core.lcd"] == {
        "name": "core.lcd",
        "status": "failed",
        "reason": cli.HardwareUnavailable.__name__,
    }


# --- サブプロセス（6.8） ---


def test_サブプロセスで_JSON_を見本の形で出す(xdg_dirs: dict[str, Path]) -> None:
    _place_config()
    env = dict(os.environ)
    env.pop("JOURNAL_STREAM", None)
    result = subprocess.run(
        [sys.executable, "-m", "osekkai", "--self-check", "--fake-hardware", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=WAIT_S,
    )
    data = json.loads(result.stdout)
    sample = load_fixture("selfcheck_failed.json")
    assert set(data) == set(sample)
    assert all(set(row) == set(sample["results"][0]) for row in data["results"])
    assert result.returncode == (0 if data["ok"] else 1)
    assert [row["name"] for row in data["results"]] == ALL_NAMES
    rows = _by_name(data)
    for name in VOICE_NAMES:
        assert rows[name] == {"name": name, "status": "skipped", "reason": REASON_TYPED}
    # 入口の項目を飛ばしても、終了コードは 0 のまま
    assert result.returncode == 0
    assert data["ok"] is True
