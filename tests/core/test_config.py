"""設定の読み込みと検証（3.3、8.5、11.1〜11.7）。"""

import logging
from datetime import time
from importlib import resources
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from osekkai.core.config import (
    ConfigLoader,
    CoreConfig,
    FeatureConfig,
)
from osekkai.core.paths import Paths

REPO_EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "osekkai.example.yaml"
HIDDEN = "9876"


class Inner(BaseModel):
    level: int = Field(default=1, ge=1, le=3)


class GreetingConfig(FeatureConfig):
    child_name: str = ""
    pin: str = ""
    volume: int = Field(default=5, ge=0, le=10)
    inner: Inner = Field(default_factory=Inner)


class OtherConfig(FeatureConfig):
    count: int = Field(default=2, ge=0, le=5)


MODELS: dict[str, type[FeatureConfig]] = {"greeting": GreetingConfig, "other": OtherConfig}


def _write(paths: Paths, text: str) -> Path:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")
    return paths.config_file


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def test_同梱の見本とリポジトリの見本は同じ内容() -> None:
    packaged = resources.files("osekkai.core").joinpath("example_config.yaml").read_bytes()
    assert packaged == REPO_EXAMPLE.read_bytes()


def test_見本はそのまま既定値と同じに読める(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    _write(paths, REPO_EXAMPLE.read_text(encoding="utf-8"))
    report = ConfigLoader(paths, MODELS).load()
    assert report.fatal_errors == []
    assert report.warnings == []
    assert report.core == CoreConfig()


def test_ファイルがなければ見本から作って場所をログに残す(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    paths = Paths()
    assert not paths.config_file.exists()
    report = ConfigLoader(paths, MODELS).load()
    assert report.created_from_example is True
    assert paths.config_file.read_bytes() == REPO_EXAMPLE.read_bytes()
    assert (paths.config_dir.stat().st_mode & 0o777) == 0o700
    assert str(paths.config_file) in _logged(caplog)
    assert report.fatal_errors == []
    assert report.core == CoreConfig()
    assert report.features["greeting"] == GreetingConfig()


def test_作らない読み方ではファイルがなくても作らない(xdg_dirs: dict[str, Path]) -> None:
    """self-check の読み方。確かめるだけで、ファイルを作らない（device-deployment の実機確認）。"""
    paths = Paths()
    report = ConfigLoader(paths, MODELS).load(create_missing=False)
    assert not paths.config_file.exists()
    assert not paths.config_dir.exists()
    assert report.missing is True
    assert report.created_from_example is False
    assert report.fatal_errors != []


def test_作らない読み方でもファイルがあれば読む(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    _write(paths, "core:\n  cache_max_mb: 300\n")
    report = ConfigLoader(paths, MODELS).load(create_missing=False)
    assert report.missing is False
    assert report.core.cache_max_mb == 300


def test_ファイルがあれば見本から作らない(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    _write(paths, "core:\n  cache_max_mb: 300\n")
    report = ConfigLoader(paths, MODELS).load()
    assert report.created_from_example is False
    assert report.core.cache_max_mb == 300


def test_書いていない項目は既定値になる(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    _write(paths, "core:\n  voice:\n    pitch: 5\n")
    report = ConfigLoader(paths, MODELS).load()
    core = report.core
    assert core.voice.pitch == 5
    assert core.voice.speed == 1.05
    assert core.startup_voice_hours == (time(8, 0), time(20, 0))
    assert core.motion.allow_drive_in_dance is False
    assert core.motion.calibration.motor_direction == (1, 1)
    assert core.hardware.lcd_address == 0x27
    assert core.hardware.motor_driver == "auto"
    assert core.hardware.speaker_enable_pin == "auto"
    assert core.cache_max_mb == 200
    assert report.features["greeting"] == GreetingConfig()
    assert report.features["other"] == OtherConfig()
    assert report.features["greeting"].enabled is True


def test_空のファイルは既定値になる(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    _write(paths, "")
    report = ConfigLoader(paths, MODELS).load()
    assert report.fatal_errors == []
    assert report.core == CoreConfig()


def test_時刻と調整値とアドレスを読む(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    _write(
        paths,
        "core:\n"
        '  startup_voice_hours: ["07:30", "21:00"]\n'
        "  motion:\n"
        "    calibration: {steering: -3.5, pan: 2, tilt: 0, motor_direction: [1, -1]}\n"
        "  hardware: {lcd_address: 0x3f, extra_button_pin: D0, speaker_enable_pin: 20}\n",
    )
    report = ConfigLoader(paths, MODELS).load()
    assert report.fatal_errors == []
    core = report.core
    assert core.startup_voice_hours == (time(7, 30), time(21, 0))
    assert core.motion.calibration.steering == -3.5
    assert core.motion.calibration.motor_direction == (1, -1)
    assert core.hardware.lcd_address == 0x3F
    assert core.hardware.extra_button_pin == "D0"
    assert core.hardware.speaker_enable_pin == 20


def test_YAMLの書き方の誤りは致命の誤りで基盤は既定値になる(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    paths = Paths()
    _write(paths, f"core:\n  cache_max_mb: 300\ngreeting:\n  pin: [{HIDDEN}\n")
    report = ConfigLoader(paths, MODELS).load()
    assert report.fatal_errors
    assert report.core == CoreConfig()
    assert report.features == {}
    assert HIDDEN not in _logged(caplog)
    assert all(HIDDEN not in e for e in report.fatal_errors)


def test_トップレベルが対応表でなければ致命の誤り(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    _write(paths, "- a\n- b\n")
    report = ConfigLoader(paths, MODELS).load()
    assert report.fatal_errors
    assert report.core == CoreConfig()


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ("core:\n  voice:\n    pitch: 99\n", "core.voice.pitch"),
        ("core:\n  cache_max_mb: 1\n", "core.cache_max_mb"),
        ("core:\n  motion:\n    calibration:\n      steering: 25\n", "core.motion.calibration"),
        ("core:\n  motion:\n    calibration:\n      motor_direction: [1, 2]\n", "motor_direction"),
        ("core:\n  hardware:\n    lcd_address: 0x80\n", "core.hardware.lcd_address"),
        ("core:\n  hardware:\n    motor_driver: fast\n", "core.hardware.motor_driver"),
        ('core:\n  startup_voice_hours: ["25:00", "20:00"]\n', "core.startup_voice_hours"),
        ("core: 3\n", "core"),
    ],
)
def test_基盤の節の誤りは致命の誤りで場所を示す(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture, text: str, where: str
) -> None:
    paths = Paths()
    _write(paths, text)
    report = ConfigLoader(paths, MODELS).load()
    assert report.core == CoreConfig()
    assert any(where in e for e in report.fatal_errors), report.fatal_errors
    assert where in _logged(caplog)


def test_機能の範囲外の値はその機能だけの誤りになる(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    paths = Paths()
    _write(paths, "greeting:\n  volume: 11\n  inner:\n    level: 9\nother:\n  count: 3\n")
    report = ConfigLoader(paths, MODELS).load()
    assert report.fatal_errors == []
    assert set(report.feature_errors) == {"greeting"}
    errors = report.feature_errors["greeting"]
    assert any(e.startswith("greeting.volume:") for e in errors)
    assert any(e.startswith("greeting.inner.level:") for e in errors)
    assert "greeting" not in report.features
    assert report.features["other"] == OtherConfig(count=3)
    assert "greeting.volume" in _logged(caplog)


def test_機能の節が対応表でなければその機能の誤り(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    _write(paths, "greeting: 3\n")
    report = ConfigLoader(paths, MODELS).load()
    assert report.fatal_errors == []
    assert list(report.feature_errors) == ["greeting"]
    assert report.features["other"] == OtherConfig()


def test_enabledがfalseなら報告に出る(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    _write(paths, "greeting:\n  enabled: false\n")
    report = ConfigLoader(paths, MODELS).load()
    assert report.features["greeting"].enabled is False
    assert report.features["other"].enabled is True
    assert report.feature_errors == {}


def test_機能の値はそのモデルの型で返る(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    _write(paths, "greeting:\n  volume: 7\n")
    report = ConfigLoader(paths, MODELS).load()
    config = report.features["greeting"]
    assert isinstance(config, GreetingConfig)
    assert config.volume == 7


def test_知らない項目は警告にして続ける(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    paths = Paths()
    _write(
        paths,
        "core:\n"
        f"  colour: {HIDDEN}\n"
        "  voice:\n"
        f"    tone: {HIDDEN}\n"
        "  motion:\n"
        "    calibration:\n"
        f"      roll: {HIDDEN}\n"
        "greeting:\n"
        f"  nickname: {HIDDEN}\n"
        "  inner:\n"
        f"    depth: {HIDDEN}\n"
        f"unknown_feature:\n  pin: {HIDDEN}\n",
    )
    report = ConfigLoader(paths, MODELS).load()
    assert report.fatal_errors == []
    assert report.feature_errors == {}
    joined = "\n".join(report.warnings)
    for key in (
        "core.colour",
        "core.voice.tone",
        "core.motion.calibration.roll",
        "greeting.nickname",
        "greeting.inner.depth",
        "unknown_feature",
    ):
        assert key in joined
        assert key in _logged(caplog)
    assert HIDDEN not in joined
    assert HIDDEN not in _logged(caplog)


def test_誤りのログと報告に設定の値を出さない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    paths = Paths()
    _write(
        paths,
        "core:\n"
        f'  startup_voice_hours: ["{HIDDEN}", "20:00"]\n'
        f"  hardware:\n    motor_driver: x{HIDDEN}\n    lcd_address: {HIDDEN}\n"
        f"    speaker_enable_pin: p{HIDDEN}\n"
        f"greeting:\n  volume: {HIDDEN}\n  pin: [{HIDDEN}]\n  enabled: e{HIDDEN}\n",
    )
    report = ConfigLoader(paths, MODELS).load()
    assert report.fatal_errors
    assert report.feature_errors["greeting"]
    everything = "\n".join(
        [*report.fatal_errors, *report.warnings, *report.feature_errors["greeting"]]
    )
    assert HIDDEN not in everything
    assert HIDDEN not in _logged(caplog)


def test_指定したファイルを読み既定の場所は使わない(
    xdg_dirs: dict[str, Path], tmp_path: Path
) -> None:
    paths = Paths()
    _write(paths, "core:\n  cache_max_mb: 300\n")
    override = tmp_path / "other.yaml"
    override.write_text("core:\n  cache_max_mb: 400\n", encoding="utf-8")
    report = ConfigLoader(paths, MODELS).load(override)
    assert report.core.cache_max_mb == 400
    assert report.created_from_example is False


def test_指定したファイルがなければ致命の誤りで見本から作らない(
    xdg_dirs: dict[str, Path], tmp_path: Path
) -> None:
    paths = Paths()
    report = ConfigLoader(paths, MODELS).load(tmp_path / "missing.yaml")
    assert report.fatal_errors
    assert report.created_from_example is False
    assert not paths.config_file.exists()
    assert report.core == CoreConfig()
