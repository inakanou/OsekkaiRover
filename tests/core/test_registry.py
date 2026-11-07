"""機能の発見と、開始してよい機能の選び出しのテスト（3.1、3.2、3.3、3.4、3.6、11.4）。"""

import logging
import threading
from collections.abc import Callable
from typing import Any, ClassVar

import pytest

from osekkai.core import Feature, FeatureConfig, FeatureContext, FeatureSpec, Priority, Resource
from osekkai.core import registry as registry_module
from osekkai.core.config import ConfigReport, CoreConfig
from osekkai.core.registry import ENTRY_POINT_GROUP, FeaturePlan, FeatureRegistry


class GreetConfig(FeatureConfig):
    word: str = "おはよう"


def make_feature(
    name: str,
    priority: int = Priority.SCHEDULED,
    config_model: type[FeatureConfig] = FeatureConfig,
) -> type[Feature]:
    class _Feature:
        spec: ClassVar[FeatureSpec] = FeatureSpec(
            name=name,
            priority=priority,
            resources=frozenset({Resource.VOICE}),
            config_model=config_model,
        )

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            self.ctx = ctx

        def run(self, stop: threading.Event) -> None:
            stop.wait()

    _Feature.__name__ = f"Feature_{name}"
    return _Feature


def report(
    features: dict[str, FeatureConfig],
    *,
    fatal_errors: list[str] | None = None,
    feature_errors: dict[str, list[str]] | None = None,
) -> ConfigReport:
    return ConfigReport(
        core=CoreConfig(),
        features=features,
        fatal_errors=fatal_errors or [],
        feature_errors=feature_errors or {},
    )


def names(plans: list[FeaturePlan] | Any) -> list[str]:
    return [plan.feature_cls.spec.name for plan in plans]


class FakeEntryPoint:
    def __init__(self, name: str, load: Callable[[], object]) -> None:
        self.name = name
        self.value = f"fake.{name}:Feature"
        self._load = load

    def load(self) -> object:
        return self._load()


def patch_entry_points(monkeypatch: pytest.MonkeyPatch, eps: list[FakeEntryPoint]) -> list[str]:
    groups: list[str] = []

    def fake_entry_points(*, group: str) -> list[FakeEntryPoint]:
        groups.append(group)
        return eps

    monkeypatch.setattr(registry_module, "entry_points", fake_entry_points)
    return groups


def test_渡したクラスを受け付けて設定とともに開始の計画にする() -> None:
    greet = make_feature("greet", config_model=GreetConfig)
    radio = make_feature("radio", priority=Priority.REMOTE)
    registry = FeatureRegistry([greet, radio])

    assert list(registry.feature_classes()) == [greet, radio]
    plans = registry.plan(report({"greet": GreetConfig(word="やあ"), "radio": FeatureConfig()}))

    assert [plan.feature_cls for plan in plans] == [greet, radio]
    assert isinstance(plans[0].config, GreetConfig)
    assert plans[0].config.word == "やあ"
    assert list(registry.skipped()) == []


def test_計画は変えられない() -> None:
    plan = FeaturePlan(make_feature("greet"), FeatureConfig())
    with pytest.raises(AttributeError):
        plan.config = FeatureConfig()  # type: ignore[misc]


def test_帯の外の優先度を断る(caplog: pytest.LogCaptureFixture) -> None:
    ok = make_feature("ok")
    with caplog.at_level(logging.WARNING, logger="osekkai"):
        registry = FeatureRegistry([make_feature("zero", priority=0), ok, make_feature("high", 95)])

    assert list(registry.feature_classes()) == [ok]
    skipped = dict(registry.skipped())
    assert set(skipped) == {"Feature_zero", "Feature_high"}
    assert "優先度" in skipped["Feature_zero"]
    assert "Feature_zero" in caplog.text
    assert "Feature_high" in caplog.text


@pytest.mark.parametrize("bad_name", ["Greet", "1greet", "greet-me", "", "core"])
def test_名前の形が誤った機能を断る(bad_name: str) -> None:
    registry = FeatureRegistry([make_feature(bad_name)])

    assert list(registry.feature_classes()) == []
    assert [label for label, _ in registry.skipped()] == [f"Feature_{bad_name}"]


def test_申告のないものは機能として受け付けない() -> None:
    class NotAFeature:
        pass

    ok = make_feature("ok")
    registry = FeatureRegistry([NotAFeature, ok])  # type: ignore[list-item]

    assert list(registry.feature_classes()) == [ok]
    assert [label for label, _ in registry.skipped()] == ["NotAFeature"]


def test_同じ名前の後の方を除いてログに残す(caplog: pytest.LogCaptureFixture) -> None:
    first = make_feature("greet")
    second = make_feature("greet", priority=Priority.BACKGROUND)
    with caplog.at_level(logging.WARNING, logger="osekkai"):
        registry = FeatureRegistry([first, second])

    assert list(registry.feature_classes()) == [first]
    assert list(registry.skipped()) == [("greet", "名前が重なる")]
    assert "greet" in caplog.text
    assert "名前が重なる" in caplog.text


def test_無効の機能を開始しない(caplog: pytest.LogCaptureFixture) -> None:
    greet = make_feature("greet")
    radio = make_feature("radio")
    registry = FeatureRegistry([greet, radio])

    with caplog.at_level(logging.INFO, logger="osekkai"):
        plans = registry.plan(
            report({"greet": FeatureConfig(enabled=False), "radio": FeatureConfig()})
        )

    assert names(plans) == ["radio"]
    assert list(registry.skipped()) == [("greet", "無効")]
    assert "greet" in caplog.text


def test_設定に誤りのある機能を開始せずほかは開始する() -> None:
    greet = make_feature("greet", config_model=GreetConfig)
    radio = make_feature("radio")
    registry = FeatureRegistry([greet, radio])

    plans = registry.plan(
        report({"radio": FeatureConfig()}, feature_errors={"greet": ["greet.word: 誤り"]})
    )

    assert names(plans) == ["radio"]
    assert list(registry.skipped()) == [("greet", "設定の誤り")]


def test_設定の全体の誤りがあればどの機能も開始しない() -> None:
    registry = FeatureRegistry([make_feature("greet"), make_feature("radio")])

    plans = registry.plan(
        report({"greet": FeatureConfig(), "radio": FeatureConfig()}, fatal_errors=["core: 誤り"])
    )

    assert list(plans) == []
    assert list(registry.skipped()) == [("greet", "設定の誤り"), ("radio", "設定の誤り")]


def test_設定の報告にない機能は既定の設定で開始する() -> None:
    greet = make_feature("greet", config_model=GreetConfig)
    registry = FeatureRegistry([greet])

    plans = registry.plan(report({}))

    assert names(plans) == ["greet"]
    assert isinstance(plans[0].config, GreetConfig)
    assert plans[0].config.word == "おはよう"


def test_発見の理由は計画のたびに消えず計画の理由は入れ替わる() -> None:
    registry = FeatureRegistry([make_feature("greet"), make_feature("greet"), make_feature("a")])

    registry.plan(report({"a": FeatureConfig(enabled=False)}))
    registry.plan(report({"a": FeatureConfig()}))

    assert list(registry.skipped()) == [("greet", "名前が重なる")]


def test_エントリーポイントから読み込み失敗の機能を除いてほかは受け付ける(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    greet = make_feature("greet")
    radio = make_feature("radio")

    def broken() -> object:
        raise ImportError("ひみつの詳細")

    groups = patch_entry_points(
        monkeypatch,
        [
            FakeEntryPoint("radio", lambda: radio),
            FakeEntryPoint("broken", broken),
            FakeEntryPoint("greet", lambda: greet),
            FakeEntryPoint("strange", lambda: 42),
        ],
    )
    with caplog.at_level(logging.WARNING, logger="osekkai"):
        registry = FeatureRegistry()

    assert groups == [ENTRY_POINT_GROUP]
    assert ENTRY_POINT_GROUP == "osekkai.features"
    # エントリーポイントの名前の順に並べる
    assert list(registry.feature_classes()) == [greet, radio]
    skipped = dict(registry.skipped())
    assert skipped["broken"] == "読み込めない"
    assert set(skipped) == {"broken", "strange"}
    assert "broken" in caplog.text
    assert "ImportError" in caplog.text
    assert "ひみつの詳細" not in caplog.text


def test_エントリーポイントの機能にも名前と重複の決まりを当てる(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = make_feature("greet")
    second = make_feature("greet")
    patch_entry_points(
        monkeypatch,
        [
            FakeEntryPoint("b_greet", lambda: second),
            FakeEntryPoint("a_greet", lambda: first),
            FakeEntryPoint("c_bad", lambda: make_feature("greet", priority=95)),
        ],
    )

    registry = FeatureRegistry()

    assert list(registry.feature_classes()) == [first]
    skipped = list(registry.skipped())
    assert ("greet", "名前が重なる") in skipped
    assert [label for label, _ in skipped if label == "c_bad"] == ["c_bad"]


def test_エントリーポイントが宣言されていなければ機能はない(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 本物のエントリーポイントには日課と音声の入口が登録されているので、空の群に差し替える
    groups = patch_entry_points(monkeypatch, [])
    registry = FeatureRegistry()

    assert list(registry.feature_classes()) == []
    assert list(registry.plan(report({}))) == []
    assert groups == [ENTRY_POINT_GROUP]
