"""ラジコンを基盤に登録したことのテスト（1.3、12.1、13.4、15.2）。

本物のエントリーポイント（`pyproject.toml` の `osekkai.features`）からラジコンを見つけ、
設定の見本の `remote:` の節が既定値と同じで pin を持たないこと、
模擬のデバイスで基盤を起動すると待ち受けて、止めれば閉じること、
無効の設定では開始しないこと、self-check に 3 つの項目が順に出ることを確かめる。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.request
from collections.abc import Callable, Iterator
from importlib import metadata
from pathlib import Path
from typing import Final

import pytest
import yaml

from osekkai import __main__ as cli
from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.adapters.fake.probes import FakeProbes
from osekkai.core.config import ConfigLoader, example_config_bytes
from osekkai.core.feature import feature_models
from osekkai.core.paths import Paths
from osekkai.core.registry import ENTRY_POINT_GROUP, FeatureRegistry
from osekkai.core.runtime import Runtime
from osekkai.core.supervisor import FeatureState
from osekkai.core.types import Priority, Resource
from osekkai.features.remote import RemoteFeature
from osekkai.features.remote.checks import REASON_DISABLED
from osekkai.features.remote.config import RemoteConfig

WAIT_S: Final = 5.0
PIN: Final = "4821"
LOOPBACK: Final = "127.0.0.1"
REMOTE_NAMES: Final = ["remote.setup", "remote.sensors", "remote.usb_camera"]
REPO = Path(__file__).resolve().parents[3]
EXAMPLE_FILES = [
    REPO / "config" / "osekkai.example.yaml",
    REPO / "src" / "osekkai" / "core" / "example_config.yaml",
]


@pytest.fixture
def runtimes() -> Iterator[list[Runtime]]:
    made: list[Runtime] = []
    yield made
    for runtime in made:
        runtime.shutdown(budget=2.0)


def _wait_for(condition: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def _free_port() -> int:
    with socket.create_server(("0.0.0.0", 0)) as sock:
        port: int = sock.getsockname()[1]
    return port


def _serving(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{LOOPBACK}:{port}/app.css", timeout=1.0) as r:
            return bool(r.status == 200)
    except OSError:
        return False


def _place_config(text: str) -> None:
    config = Paths().config_file
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text, encoding="utf-8")


def _enabled_example(port: int) -> str:
    text = example_config_bytes().decode("utf-8")
    head, mark, tail = text.partition("remote:\n  enabled: false\n")
    assert mark, "見本に remote の節がない"
    tail = tail.replace("  port: 8080", f"  port: {port}", 1)
    return head + f'remote:\n  enabled: true\n  pin: "{PIN}"\n' + tail


# --- 登録と発見 ---


def test_エントリーポイントにラジコンが登録されている() -> None:
    found = {ep.name: ep.value for ep in metadata.entry_points(group=ENTRY_POINT_GROUP)}
    assert found.get("remote") == "osekkai.features.remote:RemoteFeature"


def test_機能の発見でラジコンが_1_つだけ見つかる() -> None:
    registry = FeatureRegistry()
    classes = list(registry.feature_classes())

    assert [cls for cls in classes if cls.spec.name == "remote"] == [RemoteFeature]
    assert list(registry.skipped()) == []


def test_優先度は見つけてあいさつより高く_だるまさんと日課より低い() -> None:
    by_name = {cls.spec.name: cls.spec for cls in FeatureRegistry().feature_classes()}
    remote = by_name["remote"]
    assert remote.priority == Priority.REMOTE
    assert by_name["presence"].priority < remote.priority
    for name in ("daruma", "conversation", "routine", "voice"):
        assert remote.priority < by_name[name].priority, name
    assert remote.resources == frozenset(
        {Resource.DRIVE, Resource.NECK, Resource.VOICE, Resource.DISPLAY}
    )


# --- 設定の見本 ---


def test_2_つの設定の見本は同じ内容でどちらにもラジコンの節がある() -> None:
    first, second = (path.read_bytes() for path in EXAMPLE_FILES)
    assert first == second
    assert "remote" in yaml.safe_load(first)


def test_見本の節はだるまさんの後で日課の前() -> None:
    text = example_config_bytes().decode("utf-8")
    assert text.index("\ndaruma:") < text.index("\nremote:") < text.index("# routine:")


def test_見本の_remote_の節は既定値と同じで_pin_は書かない() -> None:
    data = yaml.safe_load(example_config_bytes().decode("utf-8"))
    section = data["remote"]
    assert set(section) == set(RemoteConfig.model_fields) - {"pin"}
    assert RemoteConfig.model_validate(section) == RemoteConfig()
    assert section["enabled"] is False


def test_見本から作った設定でラジコンは既定値になり開始しない(xdg_dirs: dict[str, Path]) -> None:
    models = feature_models(cls.spec for cls in FeatureRegistry().feature_classes())
    report = ConfigLoader(Paths(), models).load()

    assert report.created_from_example is True
    assert report.fatal_errors == []
    assert report.feature_errors == {}
    assert report.warnings == []
    assert report.features["remote"] == RemoteConfig()
    assert RemoteFeature not in [plan.feature_cls for plan in FeatureRegistry().plan(report)]


# --- 模擬のデバイスで起動する（15.2） ---


def test_模擬のデバイスで起動するとラジコンが待ち受け_止めると閉じる(
    xdg_dirs: dict[str, Path],
    fakes: FakeDeviceSet,
    clock: FakeClock,
) -> None:
    port = _free_port()
    _place_config(_enabled_example(port))
    runtime = Runtime(fakes.to_devices(), Paths(), clock, FeatureRegistry())
    try:
        assert runtime.start() is None
        assert runtime.report is not None
        assert runtime.report.feature_errors == {}
        assert "remote" in runtime.started_features
        supervisor = runtime.supervisor
        assert supervisor is not None
        assert _wait_for(lambda: supervisor.states()["remote"] is FeatureState.RUNNING)
        assert _wait_for(lambda: _serving(port))
    finally:
        runtime.shutdown(budget=2.0)
    assert not _serving(port)


def test_無効の設定で起動するとラジコンは開始しない(
    xdg_dirs: dict[str, Path],
    fakes: FakeDeviceSet,
    clock: FakeClock,
    runtimes: list[Runtime],
) -> None:
    _place_config(example_config_bytes().decode("utf-8"))
    runtime = Runtime(fakes.to_devices(), Paths(), clock, FeatureRegistry())
    runtimes.append(runtime)

    assert runtime.start() is None

    assert "remote" not in runtime.started_features


# --- self-check（13.4） ---


@pytest.fixture
def probes(xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_build_probes", lambda fake_hardware: FakeProbes())


def test_self_check_に_3_つの項目が順に出て_無効なら飛ばす(
    probes: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _place_config(example_config_bytes().decode("utf-8"))

    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    rows = [row for row in data["results"] if row["name"].startswith("remote.")]
    assert [row["name"] for row in rows] == REMOTE_NAMES
    assert all(row["status"] == "skipped" for row in rows)
    assert all(row["reason"] == REASON_DISABLED for row in rows)
