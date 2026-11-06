"""首の調整値の移し替えのテスト（9.3）。"""

import logging
import math
import os
import stat
from pathlib import Path

import pytest
import yaml

from osekkai.core.config import ConfigLoader
from osekkai.core.paths import Paths
from rover_deploy.calibration import MigrationResult, migrate

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "config" / "osekkai.example.yaml"
CORE_EXAMPLE = REPO / "src" / "osekkai" / "core" / "example_config.yaml"

DEFAULT_CALIBRATION = {"steering": 0.0, "pan": 0.0, "tilt": 0.0, "motor_direction": [1, 1]}


def write_conf(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sample" / "data" / "picar-x.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def config_path(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".config" / "osekkai" / "config.yaml"


def calibration_of(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    result: dict[str, object] = data["core"]["motion"]["calibration"]
    return result


def core_accepts(path: Path, tmp_path: Path) -> tuple[float, float, float, tuple[int, int]]:
    """基盤の読み込みで検証し、調整値を返す。"""
    paths = Paths({}, home=tmp_path / "loader-home", uid=1000)
    report = ConfigLoader(paths, {}).load(override=path)
    assert report.fatal_errors == []
    assert report.warnings == []
    c = report.core.motion.calibration
    return (c.steering, c.pan, c.tilt, c.motor_direction)


SAMPLE_FULL = (
    "# robot-hat config and calibration value of robots\n"
    "\n"
    "picarx_dir_servo = 3.5\n"
    "picarx_cam_pan_servo = 4\n"
    "picarx_cam_tilt_servo = -2.5\n"
    "picarx_dir_motor = [1, -1]\n"
)


def test_example_files_are_the_same() -> None:
    assert EXAMPLE.read_bytes() == CORE_EXAMPLE.read_bytes()


def test_creates_config_from_example_with_signs_converted(tmp_path: Path) -> None:
    conf = write_conf(tmp_path, SAMPLE_FULL)
    config = config_path(tmp_path)

    result = migrate(conf, config, EXAMPLE)

    assert result == MigrationResult("migrated")
    # かじ取りとモーターの向きはそのまま、首の左右と上下は符号を反転
    assert calibration_of(config) == {
        "steering": 3.5,
        "pan": -4.0,
        "tilt": 2.5,
        "motor_direction": [1, -1],
    }
    assert core_accepts(config, tmp_path) == (3.5, -4.0, 2.5, (1, -1))
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700


def test_only_the_four_value_lines_change_and_comments_remain(tmp_path: Path) -> None:
    conf = write_conf(tmp_path, SAMPLE_FULL)
    config = config_path(tmp_path)

    migrate(conf, config, EXAMPLE)

    before = EXAMPLE.read_text(encoding="utf-8").splitlines()
    after = config.read_text(encoding="utf-8").splitlines()
    assert len(before) == len(after)
    changed = [(b, a) for b, a in zip(before, after, strict=True) if b != a]
    assert len(changed) == 4
    for b, a in changed:
        assert b.split(":")[0] == a.split(":")[0]
    # 注釈はすべて残る
    for line in before:
        if "#" in line:
            comment = line[line.index("#") :]
            assert any(comment in a for a in after)
    motor = next(a for a in after if a.strip().startswith("motor_direction:"))
    assert motor.strip() == "motor_direction: [1, -1]     # 左右のモーターの向き（1 か -1）"


def test_rewrites_existing_default_config_in_place(tmp_path: Path) -> None:
    conf = write_conf(tmp_path, SAMPLE_FULL)
    config = config_path(tmp_path)
    config.parent.mkdir(parents=True)
    text = EXAMPLE.read_text(encoding="utf-8") + "\ngreeting:\n  enabled: true\n"
    config.write_text(text, encoding="utf-8")
    os.chmod(config, 0o644)

    result = migrate(conf, config, tmp_path / "no-example.yaml")

    assert result.status == "migrated"
    assert calibration_of(config) == {
        "steering": 3.5,
        "pan": -4.0,
        "tilt": 2.5,
        "motor_direction": [1, -1],
    }
    after = config.read_text(encoding="utf-8")
    assert after.endswith("\ngreeting:\n  enabled: true\n")
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert [p.name for p in config.parent.iterdir()] == ["config.yaml"]


def test_partial_keys_migrate_only_those(tmp_path: Path) -> None:
    conf = write_conf(tmp_path, "picarx_cam_tilt_servo = 7\n")
    config = config_path(tmp_path)

    assert migrate(conf, config, EXAMPLE).status == "migrated"
    assert calibration_of(config) == {**DEFAULT_CALIBRATION, "tilt": -7.0}


def test_zero_does_not_become_negative_zero(tmp_path: Path) -> None:
    conf = write_conf(tmp_path, "picarx_cam_pan_servo = 0\npicarx_dir_servo = -1.25\n")
    config = config_path(tmp_path)

    assert migrate(conf, config, EXAMPLE).status == "migrated"
    text = config.read_text(encoding="utf-8")
    assert "-0.0" not in text
    assert calibration_of(config) == {**DEFAULT_CALIBRATION, "steering": -1.25}


def test_small_values_stay_numbers(tmp_path: Path) -> None:
    conf = write_conf(tmp_path, "picarx_dir_servo = 0.00001\n")
    config = config_path(tmp_path)

    assert migrate(conf, config, EXAMPLE).status == "migrated"
    steering = calibration_of(config)["steering"]
    assert isinstance(steering, float)
    assert math.isclose(steering, 0.00001)


def test_range_edges_are_migrated(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        "picarx_dir_servo = -20\npicarx_cam_pan_servo = 20\n"
        "picarx_cam_tilt_servo = -20.0\npicarx_dir_motor = [-1, -1]\n",
    )
    config = config_path(tmp_path)

    assert migrate(conf, config, EXAMPLE).status == "migrated"
    assert core_accepts(config, tmp_path) == (-20.0, -20.0, 20.0, (-1, -1))


@pytest.mark.parametrize(
    "line",
    [
        "picarx_dir_servo = 20.5",
        "picarx_cam_pan_servo = -21",
        "picarx_cam_tilt_servo = 100",
        "picarx_dir_servo = nan",
        "picarx_cam_pan_servo = inf",
        "picarx_dir_motor = [1, 2]",
        "picarx_dir_motor = [0, 1]",
    ],
)
def test_out_of_range_writes_nothing(tmp_path: Path, line: str) -> None:
    conf = write_conf(tmp_path, f"{line}\npicarx_dir_servo = 1\n")
    config = config_path(tmp_path)

    assert migrate(conf, config, EXAMPLE).status == "out_of_range"
    assert not config.exists()
    assert not config.parent.exists()


def test_out_of_range_keeps_existing_config(tmp_path: Path) -> None:
    conf = write_conf(tmp_path, "picarx_cam_pan_servo = 30\n")
    config = config_path(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_bytes(EXAMPLE.read_bytes())

    assert migrate(conf, config, EXAMPLE).status == "out_of_range"
    assert config.read_bytes() == EXAMPLE.read_bytes()


@pytest.mark.parametrize(
    "text",
    [
        "# robot-hat config and calibration value of robots\n",
        "",
        "other_key = 3\n",
    ],
)
def test_no_keys_is_nothing_to_migrate(tmp_path: Path, text: str) -> None:
    conf = write_conf(tmp_path, text)
    config = config_path(tmp_path)

    assert migrate(conf, config, EXAMPLE).status == "nothing_to_migrate"
    assert not config.exists()


def test_missing_sample_conf_is_nothing_to_migrate(tmp_path: Path) -> None:
    config = config_path(tmp_path)

    assert migrate(tmp_path / "none.conf", config, EXAMPLE).status == "nothing_to_migrate"
    assert not config.exists()


@pytest.mark.parametrize(
    "text",
    [
        "picarx_dir_servo = abc\n",
        "picarx_dir_motor = 1, 1\n",
        "picarx_dir_motor = [1]\n",
        "picarx_dir_motor = [1.5, 1]\n",
    ],
)
def test_unparsable_sample_value_is_unreadable(tmp_path: Path, text: str) -> None:
    conf = write_conf(tmp_path, text)
    config = config_path(tmp_path)

    assert migrate(conf, config, EXAMPLE).status == "unreadable"
    assert not config.exists()


def test_sample_conf_not_utf8_is_unreadable(tmp_path: Path) -> None:
    conf = write_conf(tmp_path, "")
    conf.write_bytes(b"picarx_dir_servo = \xff\xfe\n")

    assert migrate(conf, config_path(tmp_path), EXAMPLE).status == "unreadable"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("steering: 0.0", "steering: 1.5"),
        ("pan: 0.0", "pan: -3"),
        ("tilt: 0.0", "tilt: 0.5"),
        ("motor_direction: [1, 1]", "motor_direction: [-1, 1]"),
    ],
)
def test_non_default_existing_config_is_already_set(tmp_path: Path, old: str, new: str) -> None:
    conf = write_conf(tmp_path, SAMPLE_FULL)
    config = config_path(tmp_path)
    config.parent.mkdir(parents=True)
    text = EXAMPLE.read_text(encoding="utf-8")
    assert old in text
    config.write_text(text.replace(old, new), encoding="utf-8")
    before = config.read_bytes()

    assert migrate(conf, config, EXAMPLE).status == "already_set"
    assert config.read_bytes() == before


def test_integer_zero_counts_as_default(tmp_path: Path) -> None:
    conf = write_conf(tmp_path, SAMPLE_FULL)
    config = config_path(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text(
        "core:\n  motion:\n    calibration:\n      steering: 0\n      pan: -0.0\n"
        "      tilt: 0\n      motor_direction: [ 1 , 1 ]\n",
        encoding="utf-8",
    )

    assert migrate(conf, config, EXAMPLE).status == "migrated"
    assert core_accepts(config, tmp_path) == (3.5, -4.0, 2.5, (1, -1))


def test_missing_keys_in_existing_block_are_added(tmp_path: Path) -> None:
    conf = write_conf(tmp_path, SAMPLE_FULL)
    config = config_path(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text(
        "core:\n"
        "  motion:\n"
        "    calibration:   # 注釈\n"
        "      steering: 0.0\n"
        "    pan_range: [-80.0, 80.0]\n"
        "  cache_max_mb: 200\n",
        encoding="utf-8",
    )

    assert migrate(conf, config, EXAMPLE).status == "migrated"
    assert core_accepts(config, tmp_path) == (3.5, -4.0, 2.5, (1, -1))
    text = config.read_text(encoding="utf-8")
    assert "    calibration:   # 注釈\n" in text
    assert text.endswith("    pan_range: [-80.0, 80.0]\n  cache_max_mb: 200\n")


@pytest.mark.parametrize(
    "text",
    [
        "core:\n  voice:\n    pitch: 3.0\n",
        "core:\n  motion:\n    calibration: {steering: 0.0}\n",
        "core:\n  motion:\n    calibration:\n",
        "core:\n  motion:\n    calibration:\n      steering: abc\n",
        "core:\n  motion:\n    calibration:\n      steering: 0.0\n      steering: 0.0\n",
        "core:\n  motion:\n    calibration:\n      motor_direction:\n        - 1\n        - 1\n",
        "core:\n  motion:\n    calibration:\n      steering: 0.0\n"
        "core:\n  motion:\n    calibration:\n      steering: 0.0\n",
        "core:\n\tmotion:\n",
        "",
    ],
)
def test_unlocatable_block_is_unreadable(tmp_path: Path, text: str) -> None:
    conf = write_conf(tmp_path, SAMPLE_FULL)
    config = config_path(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text(text, encoding="utf-8")

    assert migrate(conf, config, EXAMPLE).status == "unreadable"
    assert config.read_text(encoding="utf-8") == text


def test_missing_example_is_unreadable(tmp_path: Path) -> None:
    conf = write_conf(tmp_path, SAMPLE_FULL)
    config = config_path(tmp_path)

    assert migrate(conf, config, tmp_path / "none.yaml").status == "unreadable"
    assert not config.exists()


def test_values_are_not_printed_or_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    conf = write_conf(tmp_path, SAMPLE_FULL)
    caplog.set_level(logging.DEBUG)

    migrate(conf, config_path(tmp_path), EXAMPLE)

    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""
    assert caplog.records == []


def test_crlf_line_endings_are_kept(tmp_path: Path) -> None:
    conf = write_conf(tmp_path, SAMPLE_FULL)
    config = config_path(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_bytes(
        b"core:  # \xe5\x9f\xba\xe7\x9b\xa4\r\n  motion:\r\n    calibration:\r\n"
        b"      steering: 0.0\r\n      pan: 0.0\r\n"
    )

    assert migrate(conf, config, EXAMPLE).status == "migrated"
    data = config.read_bytes()
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert core_accepts(config, tmp_path) == (3.5, -4.0, 2.5, (1, -1))
