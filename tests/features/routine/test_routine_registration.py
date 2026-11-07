"""日課を基盤に登録したことのテスト（1.1、9.1、11.3）。

本物のエントリーポイント（`pyproject.toml` の `osekkai.features`）から日課を見つけ、
模擬のデバイスで基盤を起動すると日課が開始され、待機画面に星が出ることを確かめる。
見本から作った設定では、日課のタスクは 0 個のまま起動する。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from importlib import metadata
from pathlib import Path

import pytest

from osekkai import __main__ as cli
from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.adapters.fake.probes import FakeProbes
from osekkai.core.config import ConfigLoader, example_config_bytes
from osekkai.core.feature import feature_models
from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.paths import Paths
from osekkai.core.registry import ENTRY_POINT_GROUP, FeatureRegistry
from osekkai.core.runtime import Runtime
from osekkai.core.supervisor import FeatureState
from osekkai.features.routine import RoutineFeature
from osekkai.features.routine.config import RoutineConfig
from osekkai.features.routine.texts import idle_info

WAIT_S = 3.0
ROUTINE_CHECK_NAMES = ["routine.holidays", "routine.quiet_hours", "routine.records"]


def _wait_for(condition: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def runtimes() -> Iterator[list[Runtime]]:
    made: list[Runtime] = []
    yield made
    for runtime in made:
        runtime.shutdown(budget=2.0)


def test_エントリーポイントに日課が登録されている() -> None:
    found = {ep.name: ep.value for ep in metadata.entry_points(group=ENTRY_POINT_GROUP)}
    assert found.get("routine") == "osekkai.features.routine:RoutineFeature"


def test_機能の発見で日課が見つかる() -> None:
    registry = FeatureRegistry()

    assert RoutineFeature in list(registry.feature_classes())
    assert list(registry.skipped()) == []


def test_見本から作った設定では日課はタスクが_0_個で誤りがない(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    models = feature_models(cls.spec for cls in FeatureRegistry().feature_classes())
    report = ConfigLoader(paths, models).load()

    assert report.created_from_example is True
    assert report.fatal_errors == []
    assert report.feature_errors == {}
    assert report.warnings == []
    config = report.features["routine"]
    assert isinstance(config, RoutineConfig)
    assert config.tasks == []
    assert config.call_name == ""


def _uncomment_routine(text: str) -> str:
    """見本の `# routine:` から後ろの行の頭の「# 」を消す（保護者が使い始めるときと同じ）。"""
    head, mark, tail = text.partition("# routine:")
    lines = (mark + tail).splitlines()
    return head + "\n".join(line.removeprefix("# ").removeprefix("#") for line in lines) + "\n"


def test_見本の日課の節はコメントを外すとそのまま正しく読める(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    text = example_config_bytes().decode("utf-8")
    paths.config_file.write_text(_uncomment_routine(text), encoding="utf-8")
    models = feature_models(cls.spec for cls in FeatureRegistry().feature_classes())
    report = ConfigLoader(paths, models).load()

    assert report.fatal_errors == []
    assert report.feature_errors == {}
    assert report.warnings == []
    config = report.features["routine"]
    assert isinstance(config, RoutineConfig)
    assert len(config.tasks) == 3
    assert config.tasks[-1].sleep is True
    assert config.tasks[-1].days == "everyday"


def test_模擬のデバイスで起動すると日課が開始され待機画面に星が出る(
    xdg_dirs: dict[str, Path],
    fakes: FakeDeviceSet,
    clock: FakeClock,
    runtimes: list[Runtime],
) -> None:
    runtime = Runtime(fakes.to_devices(), Paths(), clock, FeatureRegistry())
    runtimes.append(runtime)

    assert runtime.start() is None

    assert runtime.report is not None
    assert runtime.report.feature_errors == {}
    assert "routine" in runtime.started_features
    supervisor = runtime.supervisor
    assert supervisor is not None
    assert _wait_for(lambda: supervisor.states()["routine"] is FeatureState.RUNNING)
    stars = bytes(to_lcd_codes(idle_info(0))).decode("latin-1")

    def shows_stars() -> bool:
        row = bytes(fakes.display.rows[0]).decode("latin-1").rstrip()
        return row.endswith(" " + stars)

    assert _wait_for(shows_stars)


def test_self_check_に日課の_3_つの項目が並ぶ(
    xdg_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = Paths().config_file
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(example_config_bytes())
    monkeypatch.setattr(cli, "_build_probes", lambda fake_hardware: FakeProbes())

    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    names = [row["name"] for row in data["results"]]
    # 日課の項目は、基盤の項目の後に、この順で続けて並ぶ（ほかの機能の項目はその前後）
    start = names.index(ROUTINE_CHECK_NAMES[0])
    assert names[start : start + 3] == ROUTINE_CHECK_NAMES
    rows = {row["name"]: row for row in data["results"]}
    for name in ROUTINE_CHECK_NAMES:
        assert rows[name] == {"name": name, "status": "ok", "reason": ""}
