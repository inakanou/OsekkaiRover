"""見つけてあいさつの設定（`presence:` の節）の形と検証（1.8、3.10、8.1〜8.3）。"""

from datetime import time
from pathlib import Path

import pytest

from osekkai.core.config import ConfigLoader, ConfigReport
from osekkai.core.paths import Paths
from osekkai.features.presence.config import PresenceConfig, model_paths

# 誤りの文言に出てはいけない、目立つ値
HIDDEN_TIME = "05:43"

MSG_FORMAT = '時刻は "HH:MM" のように書いてください'


def _load(xdg_dirs: dict[str, Path], text: str) -> ConfigReport:
    paths = Paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")
    return ConfigLoader(paths, {"presence": PresenceConfig}).load()


def _errors(report: ConfigReport) -> list[str]:
    return list(report.feature_errors.get("presence", []))


def _all_texts(report: ConfigReport, caplog: pytest.LogCaptureFixture) -> str:
    parts = [*report.fatal_errors, *report.warnings, *_errors(report)]
    parts.extend(r.getMessage() for r in caplog.records)
    return "\n".join(parts)


def _presence(report: ConfigReport) -> PresenceConfig:
    config = report.features["presence"]
    assert isinstance(config, PresenceConfig)
    return config


# --- 既定値と正しい設定 ---


def test_節がなければ既定値で読める(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "core: {}\n")
    assert _errors(report) == []
    config = _presence(report)
    assert config.enabled is True
    assert config.greet_hours == (time(7, 0), time(20, 30))
    assert config.away_minutes == 20
    assert config.okaeri_minutes == 120
    assert config.morning_end == time(11, 0)
    assert config.match_threshold == 0.38
    assert config.pan_reverse is False
    assert config.tilt_reverse is False
    assert config.gaze_reverse is False
    assert config.flip is False


def test_書いた値で読める(xdg_dirs: dict[str, Path]) -> None:
    text = (
        "presence:\n"
        '  greet_hours: ["06:30", "21:00"]\n'
        "  away_minutes: 30\n"
        "  okaeri_minutes: 90\n"
        '  morning_end: "10:15"\n'
        "  match_threshold: 0.5\n"
        "  pan_reverse: true\n"
        "  tilt_reverse: true\n"
        "  gaze_reverse: true\n"
        "  flip: true\n"
    )
    report = _load(xdg_dirs, text)
    assert _errors(report) == []
    assert report.warnings == []
    config = _presence(report)
    assert config.greet_hours == (time(6, 30), time(21, 0))
    assert config.away_minutes == 30
    assert config.okaeri_minutes == 90
    assert config.morning_end == time(10, 15)
    assert config.match_threshold == 0.5
    assert config.pan_reverse is True
    assert config.tilt_reverse is True
    assert config.gaze_reverse is True
    assert config.flip is True


def test_引用符のない時刻も時と分として読む(xdg_dirs: dict[str, Path]) -> None:
    # YAML 1.1 は 20:30 を 60 進の数（1230）として読むので、時と分に戻す
    report = _load(xdg_dirs, "presence:\n  greet_hours: [7:05, 20:30]\n  morning_end: 10:45\n")
    assert _errors(report) == []
    config = _presence(report)
    assert config.greet_hours == (time(7, 5), time(20, 30))
    assert config.morning_end == time(10, 45)


def test_日をまたぐ時間帯は読める(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, 'presence:\n  greet_hours: ["22:00", "06:00"]\n')
    assert _errors(report) == []
    assert _presence(report).greet_hours == (time(22, 0), time(6, 0))


def test_範囲の端は読める(xdg_dirs: dict[str, Path]) -> None:
    text = "presence:\n  away_minutes: 1\n  okaeri_minutes: 1\n  match_threshold: 0\n"
    report = _load(xdg_dirs, text)
    assert _errors(report) == []
    report = _load(
        xdg_dirs,
        "presence:\n  away_minutes: 600\n  okaeri_minutes: 1440\n  match_threshold: 1\n",
    )
    assert _errors(report) == []


def test_おかえりと見えなかった時間のしきい値が同じなら読める(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "presence:\n  away_minutes: 60\n  okaeri_minutes: 60\n")
    assert _errors(report) == []


# --- 誤り ---


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ("away_minutes: 0", "presence.away_minutes"),
        ("away_minutes: 601", "presence.away_minutes"),
        ("okaeri_minutes: 0", "presence.okaeri_minutes"),
        ("okaeri_minutes: 1441", "presence.okaeri_minutes"),
        ("match_threshold: -0.01", "presence.match_threshold"),
        ("match_threshold: 1.01", "presence.match_threshold"),
        ("pan_reverse: maybe", "presence.pan_reverse"),
        ("flip: sometimes", "presence.flip"),
    ],
)
def test_範囲の外と形の誤りは場所を出し値を出さない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture, line: str, key: str
) -> None:
    report = _load(xdg_dirs, f"presence:\n  {line}\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith(f"{key}: ")
    assert "presence" not in report.features
    value = line.split(": ", 1)[1]
    assert value not in _all_texts(report, caplog)


@pytest.mark.parametrize(
    ("section", "key", "reason"),
    [
        ("greet_hours: [7, 20:30]", "presence.greet_hours.0", MSG_FORMAT),
        ("greet_hours: [59, 20:30]", "presence.greet_hours.0", MSG_FORMAT),
        ("greet_hours: [0, 20:30]", "presence.greet_hours.0", MSG_FORMAT),
        ("greet_hours: [1440, 20:30]", "presence.greet_hours.0", MSG_FORMAT),
        ("greet_hours: [7:00, 7.5]", "presence.greet_hours.1", MSG_FORMAT),
        ("greet_hours: [true, 20:30]", "presence.greet_hours.0", MSG_FORMAT),
        ('greet_hours: ["07:00", "05:43:21"]', "presence.greet_hours.1", "時刻に秒は書けません"),
        (
            'greet_hours: ["05:43+09:00", "20:30"]',
            "presence.greet_hours.0",
            "時刻にタイムゾーンは書けません",
        ),
        ("morning_end: 11", "presence.morning_end", MSG_FORMAT),
        ("morning_end: 10.5", "presence.morning_end", MSG_FORMAT),
        ("morning_end: false", "presence.morning_end", MSG_FORMAT),
        ('morning_end: "05:43:21"', "presence.morning_end", "時刻に秒は書けません"),
        ('morning_end: "05:43+09:00"', "presence.morning_end", "時刻にタイムゾーンは書けません"),
    ],
)
def test_時刻の書き方の誤りは場所と決まった理由だけを出す(
    xdg_dirs: dict[str, Path],
    caplog: pytest.LogCaptureFixture,
    section: str,
    key: str,
    reason: str,
) -> None:
    report = _load(xdg_dirs, f"presence:\n  {section}\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith(f"{key}: ")
    assert reason in errors[0]
    assert "presence" not in report.features
    texts = _all_texts(report, caplog)
    assert HIDDEN_TIME not in texts
    assert "05:43:21" not in texts
    assert "1440" not in texts


def test_始めと終わりが同じ時間帯は誤り(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    report = _load(xdg_dirs, f'presence:\n  greet_hours: ["{HIDDEN_TIME}", "{HIDDEN_TIME}"]\n')
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith("presence.greet_hours: ")
    assert "始めと終わりが同じ" in errors[0]
    assert "presence" not in report.features
    assert HIDDEN_TIME not in _all_texts(report, caplog)


def test_時間帯が_2_つでなければ誤り(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, 'presence:\n  greet_hours: ["07:00"]\n')
    errors = _errors(report)
    assert errors
    assert all(e.startswith("presence.greet_hours") for e in errors)


def test_おかえりのしきい値が見えなかった時間より短いと誤り(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    report = _load(xdg_dirs, "presence:\n  away_minutes: 377\n  okaeri_minutes: 211\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith("presence.okaeri_minutes: ")
    assert "away_minutes 以上" in errors[0]
    assert "presence" not in report.features
    texts = _all_texts(report, caplog)
    assert "377" not in texts
    assert "211" not in texts


def test_知らない項目は警告になる(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "presence:\n  unknown_key: 1\n")
    assert _errors(report) == []
    assert "presence.unknown_key: 知らない項目です" in report.warnings


def test_無効にできる(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "presence:\n  enabled: false\n")
    assert _presence(report).enabled is False


# --- モデルの置き場所 ---


def test_モデルの置き場所はデータの置き場所の_models_の下(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    detect, recognize = model_paths(paths)
    assert detect == paths.data_dir / "models" / "face_detection_yunet_2023mar.onnx"
    assert recognize == paths.data_dir / "models" / "face_recognition_sface_2021dec.onnx"
