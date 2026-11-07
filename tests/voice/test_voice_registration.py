"""音声の入口を基盤に登録したことのテスト（8.1、9.1）。

本物のエントリーポイント（`pyproject.toml` の `osekkai.features`）から入口を見つけること、
設定の見本の `voice:` の節が既定値と同じで誤りなく読めること、
端末の偽物で基盤を起動すると、打った 1 行が押下と言葉になって、登録した機能に届くことを確かめる。
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from importlib import metadata
from pathlib import Path

import pytest
import yaml

from osekkai import __main__ as cli
from osekkai.adapters.fake.clock import FakeClock
from osekkai.adapters.fake.console import (
    ConsoleButtons,
    _reset_typed_input_for_tests,
    build_console_set,
    typed_input_active,
)
from osekkai.adapters.fake.probes import FakeProbes
from osekkai.core.config import ConfigLoader, example_config_bytes
from osekkai.core.feature import feature_models
from osekkai.core.paths import Paths
from osekkai.core.registry import ENTRY_POINT_GROUP, FeatureRegistry
from osekkai.core.runtime import Runtime
from osekkai.core.supervisor import FeatureState
from osekkai.voice import VoiceGatewayFeature, api
from osekkai.voice import checks as voice_checks
from osekkai.voice import feature as voice_feature
from osekkai.voice.checks import REASON_TYPED
from osekkai.voice.config import VoiceConfig
from osekkai.voice.intents import Heard

WAIT_S = 5.0
VOICE_CHECK_NAMES = ["voice.mic", "voice.model"]
REPO = Path(__file__).resolve().parents[2]
EXAMPLE_FILES = [
    REPO / "config" / "osekkai.example.yaml",
    REPO / "src" / "osekkai" / "core" / "example_config.yaml",
]


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    api._reset_for_tests()
    _reset_typed_input_for_tests()
    yield
    api._reset_for_tests()
    _reset_typed_input_for_tests()


@pytest.fixture
def no_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    """faster-whisper がない開発 PC と同じにする（入れてある PC でも同じ結果にする）。"""
    monkeypatch.setattr(voice_feature, "whisper_available", lambda: False)
    monkeypatch.setattr(voice_checks, "whisper_available", lambda: False)


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


# --- 登録と発見 ---


def test_エントリーポイントに入口が登録されている() -> None:
    found = {ep.name: ep.value for ep in metadata.entry_points(group=ENTRY_POINT_GROUP)}
    assert found.get("voice") == "osekkai.voice:VoiceGatewayFeature"


def test_機能の発見で入口が見つかる() -> None:
    registry = FeatureRegistry()

    assert VoiceGatewayFeature in list(registry.feature_classes())
    assert list(registry.skipped()) == []


# --- 設定の見本 ---


def test_2_つの設定の見本は同じ内容() -> None:
    first, second = (path.read_bytes() for path in EXAMPLE_FILES)
    assert first == second


def test_見本の_voice_の節は既定値と同じ() -> None:
    data = yaml.safe_load(example_config_bytes().decode("utf-8"))
    section = data["voice"]
    assert set(section) == set(VoiceConfig.model_fields)  # すべての項目を見本に書く
    assert VoiceConfig.model_validate(section) == VoiceConfig()


def test_見本から作った設定で入口は既定値になり誤りがない(xdg_dirs: dict[str, Path]) -> None:
    models = feature_models(cls.spec for cls in FeatureRegistry().feature_classes())
    report = ConfigLoader(Paths(), models).load()

    assert report.created_from_example is True
    assert report.fatal_errors == []
    assert report.feature_errors == {}
    assert report.warnings == []
    config = report.features["voice"]
    assert config == VoiceConfig()


# --- 端末の偽物で起動する（9.1） ---


def test_端末の偽物で起動すると打った_1_行が登録した機能に届く(
    xdg_dirs: dict[str, Path],
    no_whisper: None,
    runtimes: list[Runtime],
) -> None:
    read_fd, write_fd = os.pipe()
    inp = os.fdopen(read_fd, "r", encoding="utf-8")
    feed = os.fdopen(write_fd, "w", encoding="utf-8")
    clock = FakeClock()  # 9 時で時刻は確か（聞ける時間帯の中）
    console = build_console_set(clock, out=io.StringIO(), inp=inp)
    devices = console.to_devices()

    received: list[Heard] = []
    got = threading.Event()
    stop = threading.Event()  # 偽の機能の止める旗

    def on_heard(heard: Heard) -> None:
        received.append(heard)
        got.set()

    registration = api.register_words(
        "demo", 0, ("だるまさん",), on_heard, alive=lambda: not stop.is_set()
    )
    try:
        runtime = Runtime(devices, Paths(), clock, FeatureRegistry())
        runtimes.append(runtime)
        assert runtime.start() is None
        assert runtime.report is not None
        assert runtime.report.feature_errors == {}
        assert "voice" in runtime.started_features
        supervisor = runtime.supervisor
        assert supervisor is not None
        assert _wait_for(lambda: supervisor.states()["voice"] is FeatureState.RUNNING)
        assert _wait_for(typed_input_active)

        feed.write("だるまさん しよう\n")
        feed.flush()

        assert got.wait(WAIT_S)
        assert received == [Heard(text="だるまさん しよう", follow_up=False)]
    finally:
        stop.set()
        registration.remove()
        feed.close()  # 入力の終わりで読み手が終わる
        assert isinstance(console.buttons, ConsoleButtons)
        assert console.buttons.join_reader(WAIT_S)
        inp.close()


# --- self-check ---


def test_self_check_に入口の_2_つの項目が飛ばしたで並び_終了コードは_0(
    xdg_dirs: dict[str, Path],
    no_whisper: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = Paths().config_file
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(example_config_bytes())
    monkeypatch.setattr(cli, "_build_probes", lambda fake_hardware: FakeProbes())

    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    names = [row["name"] for row in data["results"]]
    assert names[-2:] == VOICE_CHECK_NAMES
    rows = {row["name"]: row for row in data["results"]}
    for name in VOICE_CHECK_NAMES:
        assert rows[name] == {"name": name, "status": "skipped", "reason": REASON_TYPED}
