"""朝と夜のお話を基盤に登録したことのテスト（1.2、8.4、8.6）。

本物のエントリーポイント（`pyproject.toml` の `osekkai.features`）からお話を見つけ、
申告が正しく（声と画面だけ、見守りより少し上の優先度）、設定の見本の `talk:` の節が
既定値（使わない）と同じで警告なく読め、無効なら開始する計画に入らないことを確かめる。
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

import yaml

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.config import ConfigLoader, example_config_bytes
from osekkai.core.feature import feature_models, validate_spec
from osekkai.core.paths import Paths
from osekkai.core.registry import ENTRY_POINT_GROUP, FeatureRegistry
from osekkai.core.runtime import Runtime
from osekkai.core.types import Priority, Resource
from osekkai.features.talk import TalkFeature
from osekkai.features.talk.checks import TALK_CHECKS
from osekkai.features.talk.config import TalkConfig
from osekkai.features.talk.flow import RESOURCES

REPO = Path(__file__).resolve().parents[3]
EXAMPLE_FILES = [
    REPO / "config" / "osekkai.example.yaml",
    REPO / "src" / "osekkai" / "core" / "example_config.yaml",
]


def _place_config(text: str) -> None:
    config = Paths().config_file
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text, encoding="utf-8")


def _enabled_example() -> str:
    text = example_config_bytes().decode("utf-8")
    head, mark, tail = text.partition("talk:\n  enabled: false\n")
    assert mark, "見本に talk の節がない"
    return head + "talk:\n  enabled: true\n" + tail


# --- 登録と発見 ---


def test_エントリーポイントにお話が登録されている() -> None:
    found = {ep.name: ep.value for ep in metadata.entry_points(group=ENTRY_POINT_GROUP)}
    assert found.get("talk") == "osekkai.features.talk:TalkFeature"


def test_機能の発見でお話が_1_つだけ見つかる() -> None:
    registry = FeatureRegistry()
    classes = list(registry.feature_classes())

    assert [cls for cls in classes if cls.spec.name == "talk"] == [TalkFeature]
    assert list(registry.skipped()) == []


def test_申告は声と画面だけで_見守りより少し上の優先度() -> None:
    spec = TalkFeature.spec

    assert validate_spec(spec) == []
    assert spec.name == "talk"
    assert spec.priority == Priority.BACKGROUND + 5
    assert spec.resources == RESOURCES == frozenset({Resource.VOICE, Resource.DISPLAY})
    assert Resource.DRIVE not in spec.resources  # 8.6
    assert Resource.NECK not in spec.resources
    assert spec.config_model is TalkConfig
    assert spec.checks == TALK_CHECKS


def test_優先度は見つけてあいさつより上で_ほかの機能より下() -> None:
    by_name = {cls.spec.name: cls.spec for cls in FeatureRegistry().feature_classes()}
    talk = by_name["talk"].priority
    assert by_name["presence"].priority < talk
    for name in ("remote", "daruma", "conversation", "routine", "voice"):
        assert talk < by_name[name].priority, name


# --- 設定の見本 ---


def test_2_つの設定の見本は同じ内容でどちらにもお話の節がある() -> None:
    first, second = (path.read_bytes() for path in EXAMPLE_FILES)
    assert first == second
    assert "talk" in yaml.safe_load(first)


def test_見本の節はラジコンの後で日課の前() -> None:
    text = example_config_bytes().decode("utf-8")
    assert text.index("\nremote:") < text.index("\ntalk:") < text.index("# routine:")


def test_見本の_talk_の節は既定値と同じで使わない() -> None:
    data = yaml.safe_load(example_config_bytes().decode("utf-8"))
    section = data["talk"]
    # 現在地と注意報の地域は、家ごとの値なので見本では行の頭を「# 」にしておく
    assert set(section) == set(TalkConfig.model_fields) - {"location", "warning_area"}
    assert TalkConfig.model_validate(section) == TalkConfig()
    assert section["enabled"] is False
    assert section["call_name"] == ""
    assert section["topics"] == []


def test_見本の書きかけの現在地と地域は行の頭を消せばそのまま読める() -> None:
    text = example_config_bytes().decode("utf-8")
    section = text[text.index("\ntalk:") : text.index("# routine:")]
    lines = [
        line.replace("  # ", "  ", 1)
        for line in section.splitlines()
        if line.startswith(("talk:", "  # location:", "  # warning_area:"))
    ]
    data = yaml.safe_load("\n".join(lines))["talk"]
    config = TalkConfig.model_validate(data)
    assert config.location is not None
    assert config.warning_area is not None
    # 地域のコードは文字として読む（引用符で囲む）
    assert isinstance(data["warning_area"]["office"], str)
    assert isinstance(data["warning_area"]["city"], str)


def test_見本から作った設定でお話は既定値になり開始しない(xdg_dirs: dict[str, Path]) -> None:
    models = feature_models(cls.spec for cls in FeatureRegistry().feature_classes())
    report = ConfigLoader(Paths(), models).load()

    assert report.created_from_example is True
    assert report.fatal_errors == []
    assert report.feature_errors == {}
    assert report.warnings == []
    assert report.features["talk"] == TalkConfig()
    assert TalkFeature not in [plan.feature_cls for plan in FeatureRegistry().plan(report)]


def test_使うと書けばお話を開始する計画に入る(xdg_dirs: dict[str, Path]) -> None:
    _place_config(_enabled_example())
    models = feature_models(cls.spec for cls in FeatureRegistry().feature_classes())
    report = ConfigLoader(Paths(), models).load()

    assert report.feature_errors == {}
    assert report.warnings == []
    assert TalkFeature in [plan.feature_cls for plan in FeatureRegistry().plan(report)]


def test_見本のまま起動するとお話は開始しない(
    xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    runtime = Runtime(fakes.to_devices(), Paths(), clock, FeatureRegistry())
    try:
        assert runtime.start() is None
        assert "talk" not in runtime.started_features
    finally:
        runtime.shutdown(budget=2.0)
