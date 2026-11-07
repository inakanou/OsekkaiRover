"""音声の入口の設定（`voice:` の節）の形と検証（1.3、5.1、8.1〜8.3）。"""

from datetime import time
from pathlib import Path

import pytest

from osekkai.core.config import ConfigLoader, ConfigReport
from osekkai.core.paths import Paths
from osekkai.voice.config import VoiceConfig

# 誤りの文言に出てはいけない、目立つ値
HIDDEN_TIME = "05:43"
HIDDEN_SECONDS = "17.43"


def _load(xdg_dirs: dict[str, Path], text: str) -> ConfigReport:
    paths = Paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")
    return ConfigLoader(paths, {"voice": VoiceConfig}).load()


def _errors(report: ConfigReport) -> list[str]:
    return list(report.feature_errors.get("voice", []))


def _all_texts(report: ConfigReport, caplog: pytest.LogCaptureFixture) -> str:
    parts = [*report.fatal_errors, *report.warnings, *_errors(report)]
    parts.extend(r.getMessage() for r in caplog.records)
    return "\n".join(parts)


def _voice(report: ConfigReport) -> VoiceConfig:
    config = report.features["voice"]
    assert isinstance(config, VoiceConfig)
    return config


# --- 既定値と正しい設定 ---


def test_節がなければ既定値で読める(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "core: {}\n")
    assert _errors(report) == []
    config = _voice(report)
    assert config.enabled is True
    assert config.mic_device == "plughw:CARD=U0x46d0x825,DEV=0"
    assert config.listen_seconds == 5.0
    assert config.model == "base"
    assert config.hours == (time(7, 0), time(21, 30))
    assert config.follow_up_seconds == 30.0
    assert config.recognizer == "auto"
    assert config.max_gain == 20.0


def test_書いた値で読める(xdg_dirs: dict[str, Path]) -> None:
    text = (
        "voice:\n"
        "  mic_device: plughw:CARD=Test,DEV=0\n"
        "  listen_seconds: 7.5\n"
        "  model: small\n"
        '  hours: ["06:30", "20:00"]\n'
        "  follow_up_seconds: 60\n"
        "  recognizer: typed\n"
        "  max_gain: 10\n"
    )
    report = _load(xdg_dirs, text)
    assert _errors(report) == []
    assert report.warnings == []
    config = _voice(report)
    assert config.mic_device == "plughw:CARD=Test,DEV=0"
    assert config.listen_seconds == 7.5
    assert config.model == "small"
    assert config.hours == (time(6, 30), time(20, 0))
    assert config.follow_up_seconds == 60.0
    assert config.recognizer == "typed"
    assert config.max_gain == 10.0


def test_引用符のない時刻も時と分として読む(xdg_dirs: dict[str, Path]) -> None:
    # YAML 1.1 は 21:30 を 60 進の数（1290）として読むので、時と分に戻す
    report = _load(xdg_dirs, "voice:\n  hours: [7:05, 21:30]\n")
    assert _errors(report) == []
    assert _voice(report).hours == (time(7, 5), time(21, 30))


def test_日をまたぐ時間帯は読める(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, 'voice:\n  hours: ["22:00", "06:00"]\n')
    assert _errors(report) == []
    assert _voice(report).hours == (time(22, 0), time(6, 0))


@pytest.mark.parametrize("kind", ["auto", "whisper", "typed"])
def test_聞き取りの選び方を受け付ける(xdg_dirs: dict[str, Path], kind: str) -> None:
    report = _load(xdg_dirs, f"voice:\n  recognizer: {kind}\n")
    assert _errors(report) == []
    assert _voice(report).recognizer == kind


# --- 誤り ---


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ("listen_seconds: 1.9", "voice.listen_seconds"),
        ("listen_seconds: 15.1", "voice.listen_seconds"),
        ("follow_up_seconds: 4", "voice.follow_up_seconds"),
        ("follow_up_seconds: 121", "voice.follow_up_seconds"),
        ("max_gain: 0.5", "voice.max_gain"),
        ("max_gain: 51", "voice.max_gain"),
        ("model: large", "voice.model"),
        ("recognizer: cloud", "voice.recognizer"),
    ],
)
def test_範囲の外と知らない選び方は誤り(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture, line: str, key: str
) -> None:
    report = _load(xdg_dirs, f"voice:\n  {line}\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith(f"{key}: ")
    assert "voice" not in report.features
    value = line.split(": ", 1)[1]
    assert value not in _all_texts(report, caplog)


@pytest.mark.parametrize(
    ("hours", "key", "reason"),
    [
        ("[7, 21:30]", "voice.hours.0", '時刻は "HH:MM" のように書いてください'),
        ("[59, 21:30]", "voice.hours.0", '時刻は "HH:MM" のように書いてください'),
        ("[0, 21:30]", "voice.hours.0", '時刻は "HH:MM" のように書いてください'),
        ("[7:00, 7.5]", "voice.hours.1", '時刻は "HH:MM" のように書いてください'),
        ("[true, 21:30]", "voice.hours.0", '時刻は "HH:MM" のように書いてください'),
        ('["07:00", "05:43:21"]', "voice.hours.1", "時刻に秒は書けません"),
        ('["05:43+09:00", "21:30"]', "voice.hours.0", "時刻にタイムゾーンは書けません"),
    ],
)
def test_時刻の書き方の誤りは場所と決まった理由だけを出す(
    xdg_dirs: dict[str, Path],
    caplog: pytest.LogCaptureFixture,
    hours: str,
    key: str,
    reason: str,
) -> None:
    report = _load(xdg_dirs, f"voice:\n  hours: {hours}\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith(f"{key}: ")
    assert reason in errors[0]
    assert "voice" not in report.features
    texts = _all_texts(report, caplog)
    assert HIDDEN_TIME not in texts
    assert "05:43:21" not in texts


def test_1440_以上の整数の時刻は誤り(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "voice:\n  hours: [1440, 21:30]\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith("voice.hours.0: ")
    assert "1440" not in errors[0]


def test_始めと終わりが同じ時間帯は誤り(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    report = _load(xdg_dirs, f'voice:\n  hours: ["{HIDDEN_TIME}", "{HIDDEN_TIME}"]\n')
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith("voice")
    assert "hours" in errors[0]
    assert "始めと終わりが同じ" in errors[0]
    assert "voice" not in report.features
    assert HIDDEN_TIME not in _all_texts(report, caplog)


def test_時間帯が_2_つでなければ誤り(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, 'voice:\n  hours: ["07:00"]\n')
    errors = _errors(report)
    assert errors
    assert all(e.startswith("voice.hours") for e in errors)


def test_誤りの文言に録音の長さの値が出ない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    report = _load(xdg_dirs, f"voice:\n  listen_seconds: {HIDDEN_SECONDS}\n")
    assert _errors(report)
    assert HIDDEN_SECONDS not in _all_texts(report, caplog)


def test_知らない項目は警告になる(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "voice:\n  unknown_key: 1\n")
    assert _errors(report) == []
    assert "voice.unknown_key: 知らない項目です" in report.warnings


def test_無効にできる(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "voice:\n  enabled: false\n")
    assert _voice(report).enabled is False
