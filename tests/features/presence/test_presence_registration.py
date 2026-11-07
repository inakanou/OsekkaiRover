"""見つけてあいさつの登録（エントリーポイント）と設定の見本のテスト（8.1、6.5）。"""

import json
from importlib import resources
from pathlib import Path

import pytest
import yaml

from osekkai import __main__ as cli
from osekkai.adapters.fake.probes import FakeProbes
from osekkai.core.config import ConfigLoader, example_config_bytes
from osekkai.core.feature import feature_models
from osekkai.core.paths import Paths
from osekkai.core.registry import FeatureRegistry
from osekkai.core.selfcheck import REASON_SKIPPED_BUSY
from osekkai.features.presence import PresenceFeature
from osekkai.features.presence.checks import REASON_NO_LIBRARY
from osekkai.features.presence.config import PresenceConfig

REPO_EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "osekkai.example.yaml"
PRESENCE_CHECK_NAMES = ["presence.camera", "presence.models", "presence.faces"]


def _example() -> dict[str, object]:
    data = yaml.safe_load(REPO_EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_機能の発見で見つけてあいさつが見つかる() -> None:
    registry = FeatureRegistry()
    classes = list(registry.feature_classes())
    # ほかの機能（日課など）も登録されている。見つけてあいさつは 1 つだけ見つかる
    assert [cls for cls in classes if cls.spec.name == "presence"] == [PresenceFeature]
    assert list(registry.skipped()) == []


def test_同梱の見本にも見つけてあいさつの節がある() -> None:
    packaged = resources.files("osekkai.core").joinpath("example_config.yaml").read_bytes()
    assert packaged == REPO_EXAMPLE.read_bytes()
    assert "presence" in yaml.safe_load(packaged)


def test_見本の節は既定値だけで_項目はすべて書いてある() -> None:
    section = _example()["presence"]
    assert isinstance(section, dict)
    assert set(section) == set(PresenceConfig.model_fields)
    assert PresenceConfig.model_validate(section) == PresenceConfig()


def test_見本を発見した機能で読むと誤りも警告もない(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_bytes(REPO_EXAMPLE.read_bytes())
    models = feature_models(cls.spec for cls in FeatureRegistry().feature_classes())
    report = ConfigLoader(paths, models).load()
    assert report.fatal_errors == []
    assert report.feature_errors == {}
    assert report.warnings == []
    assert report.features["presence"] == PresenceConfig()
    plans = FeatureRegistry().plan(report)
    assert PresenceFeature in [plan.feature_cls for plan in plans]


@pytest.fixture
def placed(xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """見本の設定を置き、本物の機能の発見と偽の確かめで self-check を動かす。"""
    config = Paths().config_file
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(example_config_bytes())
    monkeypatch.setattr(cli, "_build_probes", lambda fake_hardware: FakeProbes())


def _rows(capsys: pytest.CaptureFixture[str]) -> tuple[list[str], dict[str, dict[str, str]]]:
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    names = [row["name"] for row in data["results"]]
    return names, {row["name"]: row for row in data["results"]}


def test_self_check_に見つけてあいさつの_3_つの項目が並び_終了コードは_0(
    placed: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--self-check", "--fake-hardware", "--json"]) == 0
    names, rows = _rows(capsys)
    start = names.index(PRESENCE_CHECK_NAMES[0])
    assert names[start : start + 3] == PRESENCE_CHECK_NAMES
    # 開発 PC と CI にはカメラのライブラリがないので、カメラとモデルは飛ばす
    for name in ["presence.camera", "presence.models"]:
        assert rows[name] == {"name": name, "status": "skipped", "reason": REASON_NO_LIBRARY}
    assert rows["presence.faces"] == {"name": "presence.faces", "status": "ok", "reason": ""}


def test_self_check_の文字の出力にも_3_つの項目が出る(
    placed: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--self-check", "--fake-hardware"]) == 0
    lines = capsys.readouterr().out.splitlines()
    start = lines.index(f"飛ばした presence.camera {REASON_NO_LIBRARY}")
    assert lines[start : start + 3] == [
        f"飛ばした presence.camera {REASON_NO_LIBRARY}",
        f"飛ばした presence.models {REASON_NO_LIBRARY}",
        "よい presence.faces",
    ]


def test_ハードなしの_self_check_ではカメラの項目を飛ばす(
    placed: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--self-check", "--fake-hardware", "--no-hardware", "--json"]) == 0
    _, rows = _rows(capsys)
    assert rows["presence.camera"] == {
        "name": "presence.camera",
        "status": "skipped",
        "reason": REASON_SKIPPED_BUSY,
    }
