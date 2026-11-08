"""おしゃべりを基盤に登録したことのテスト（9.1、9.3、9.4、9.5）。

本物のエントリーポイント（`pyproject.toml` の `osekkai.features`）からおしゃべりを見つけ、
申告が正しく、設定の見本の `conversation:` の節が既定値と同じで誤りなく読めること、
模擬のデバイスで基盤を起動すると受け手になり、無効の設定では開始せず受け手にならないことを確かめる。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from importlib import metadata
from pathlib import Path

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
from osekkai.core.types import Priority
from osekkai.features.conversation import ConversationFeature
from osekkai.features.conversation import checks as conversation_checks
from osekkai.features.conversation.checks import REASON_DISABLED, REASON_NO_LIBRARY
from osekkai.features.conversation.config import ConversationConfig
from osekkai.features.conversation.flow import RESOURCES
from osekkai.voice import api

WAIT_S = 3.0
REPO = Path(__file__).resolve().parents[3]
EXAMPLE_FILES = [
    REPO / "config" / "osekkai.example.yaml",
    REPO / "src" / "osekkai" / "core" / "example_config.yaml",
]


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    api._reset_for_tests()
    yield
    api._reset_for_tests()


@pytest.fixture
def no_library(monkeypatch: pytest.MonkeyPatch) -> None:
    """anthropic がない開発 PC と同じにする（入れてある PC でも同じ結果にする）。"""
    monkeypatch.setattr(conversation_checks, "anthropic_available", lambda: False)


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
        time.sleep(0.01)
    return condition()


def _place_config(text: str) -> None:
    config = Paths().config_file
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text, encoding="utf-8")


def _disabled_example() -> str:
    text = example_config_bytes().decode("utf-8")
    head, mark, tail = text.partition("conversation:\n  enabled: true\n")
    assert mark, "見本に conversation の節がない"
    return head + "conversation:\n  enabled: false\n" + tail


# --- 登録と発見 ---


def test_エントリーポイントにおしゃべりが登録されている() -> None:
    found = {ep.name: ep.value for ep in metadata.entry_points(group=ENTRY_POINT_GROUP)}
    assert found.get("conversation") == "osekkai.features.conversation:ConversationFeature"


def test_機能の発見でおしゃべりが_1_つだけ見つかる() -> None:
    registry = FeatureRegistry()
    classes = list(registry.feature_classes())

    assert [cls for cls in classes if cls.spec.name == "conversation"] == [ConversationFeature]
    assert list(registry.skipped()) == []


def test_申告は名前と優先度と資源と設定と項目が正しい() -> None:
    spec = ConversationFeature.spec

    assert spec.name == "conversation"
    assert spec.priority is Priority.INTERACTIVE
    assert spec.resources == RESOURCES
    assert spec.config_model is ConversationConfig
    assert [check.name for check in spec.checks] == ["conversation.setup"]


# --- 設定の見本 ---


def test_2_つの設定の見本は同じ内容でどちらにもおしゃべりの節がある() -> None:
    first, second = (path.read_bytes() for path in EXAMPLE_FILES)
    assert first == second
    assert "conversation" in yaml.safe_load(first)


def test_見本の節は見つけてあいさつの後で日課の前() -> None:
    text = example_config_bytes().decode("utf-8")
    assert text.index("\npresence:") < text.index("\nconversation:") < text.index("# routine:")


def test_見本の_conversation_の節は既定値と同じで_項目はすべて書いてある() -> None:
    data = yaml.safe_load(example_config_bytes().decode("utf-8"))
    section = data["conversation"]
    assert set(section) == set(ConversationConfig.model_fields)
    assert ConversationConfig.model_validate(section) == ConversationConfig()
    assert section["call_name"] == ""


def test_見本から作った設定でおしゃべりは既定値になり誤りがない(xdg_dirs: dict[str, Path]) -> None:
    models = feature_models(cls.spec for cls in FeatureRegistry().feature_classes())
    report = ConfigLoader(Paths(), models).load()

    assert report.created_from_example is True
    assert report.fatal_errors == []
    assert report.feature_errors == {}
    assert report.warnings == []
    assert report.features["conversation"] == ConversationConfig()
    assert ConversationFeature in [plan.feature_cls for plan in FeatureRegistry().plan(report)]


def test_無効の設定ではおしゃべりを開始する計画に入らない(xdg_dirs: dict[str, Path]) -> None:
    _place_config(_disabled_example())
    models = feature_models(cls.spec for cls in FeatureRegistry().feature_classes())
    report = ConfigLoader(Paths(), models).load()

    assert report.feature_errors == {}
    config = report.features["conversation"]
    assert isinstance(config, ConversationConfig)
    assert config.enabled is False
    assert ConversationFeature not in [plan.feature_cls for plan in FeatureRegistry().plan(report)]


# --- 模擬のデバイスで起動する（9.1） ---


def test_模擬のデバイスで起動するとおしゃべりが開始され受け手になる(
    xdg_dirs: dict[str, Path],
    fakes: FakeDeviceSet,
    clock: FakeClock,
    runtimes: list[Runtime],
) -> None:
    runtime = Runtime(fakes.to_devices(), Paths(), clock, FeatureRegistry())
    runtimes.append(runtime)

    assert runtime.start() is None

    assert "conversation" in runtime.started_features
    supervisor = runtime.supervisor
    assert supervisor is not None
    assert _wait_for(lambda: supervisor.states()["conversation"] is FeatureState.RUNNING)
    assert _wait_for(lambda: api.registry().owner_alive("conversation"))


def test_無効の設定で起動するとおしゃべりは開始せず受け手にならない(
    xdg_dirs: dict[str, Path],
    fakes: FakeDeviceSet,
    clock: FakeClock,
    runtimes: list[Runtime],
) -> None:
    _place_config(_disabled_example())
    runtime = Runtime(fakes.to_devices(), Paths(), clock, FeatureRegistry())
    runtimes.append(runtime)

    assert runtime.start() is None

    assert runtime.report is not None
    assert runtime.report.feature_errors == {}
    assert "conversation" not in runtime.started_features
    assert "voice" in runtime.started_features
    supervisor = runtime.supervisor
    assert supervisor is not None
    assert _wait_for(lambda: supervisor.states()["voice"] is FeatureState.RUNNING)
    time.sleep(0.1)
    assert api.registry().owner_alive("conversation") is False


# --- self-check（9.4） ---


@pytest.fixture
def probes(xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_build_probes", lambda fake_hardware: FakeProbes())


def _rows(capsys: pytest.CaptureFixture[str]) -> dict[str, dict[str, str]]:
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    return {row["name"]: row for row in data["results"]}


def test_self_check_にライブラリがないので飛ばしたで出て_終了コードは_0(
    probes: None, no_library: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _place_config(example_config_bytes().decode("utf-8"))

    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 0

    rows = _rows(capsys)
    assert rows["conversation.setup"] == {
        "name": "conversation.setup",
        "status": "skipped",
        "reason": REASON_NO_LIBRARY,
    }


def test_self_check_は無効の設定なら無効で飛ばす(
    probes: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _place_config(_disabled_example())

    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 0

    rows = _rows(capsys)
    assert rows["conversation.setup"] == {
        "name": "conversation.setup",
        "status": "skipped",
        "reason": REASON_DISABLED,
    }
