"""おしゃべりの設定（`conversation:` の節）の形と検証（4.1、5.1、5.3、2.4、6.4、9.3、9.5）。"""

from datetime import time
from pathlib import Path

import pytest

from osekkai.core.config import ConfigLoader, ConfigReport
from osekkai.core.paths import Paths
from osekkai.features.conversation.config import ConversationConfig

# 誤りの文言に出てはいけない、目立つ値
HIDDEN_TIME = "05:43"
HIDDEN_SECONDS = "4.37"

MSG_FORMAT = '時刻は "HH:MM" のように書いてください'


def _load(xdg_dirs: dict[str, Path], text: str) -> ConfigReport:
    paths = Paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")
    return ConfigLoader(paths, {"conversation": ConversationConfig}).load()


def _errors(report: ConfigReport) -> list[str]:
    return list(report.feature_errors.get("conversation", []))


def _all_texts(report: ConfigReport, caplog: pytest.LogCaptureFixture) -> str:
    parts = [*report.fatal_errors, *report.warnings, *_errors(report)]
    parts.extend(r.getMessage() for r in caplog.records)
    return "\n".join(parts)


def _conversation(report: ConfigReport) -> ConversationConfig:
    config = report.features["conversation"]
    assert isinstance(config, ConversationConfig)
    return config


# --- 既定値と正しい設定 ---


def test_節がなければ既定値で読める(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "core: {}\n")
    assert _errors(report) == []
    config = _conversation(report)
    assert config.enabled is True
    assert config.call_name == ""
    assert config.call_name_variants == []
    assert config.hours == (time(7, 0), time(21, 30))
    assert config.daily_limit == 60
    assert config.max_turns == 10
    assert config.reply_timeout_seconds == 20.0


def test_書いた値で読める(xdg_dirs: dict[str, Path]) -> None:
    text = (
        "conversation:\n"
        "  call_name: ゆう\n"
        "  call_name_variants: [優, ユウくん]\n"
        '  hours: ["06:30", "20:00"]\n'
        "  daily_limit: 30\n"
        "  max_turns: 5\n"
        "  reply_timeout_seconds: 15\n"
    )
    report = _load(xdg_dirs, text)
    assert _errors(report) == []
    assert report.warnings == []
    config = _conversation(report)
    assert config.call_name == "ゆう"
    assert config.call_name_variants == ["優", "ユウくん"]
    assert config.hours == (time(6, 30), time(20, 0))
    assert config.daily_limit == 30
    assert config.max_turns == 5
    assert config.reply_timeout_seconds == 15.0


@pytest.mark.parametrize(
    ("line", "attr", "value"),
    [
        ("daily_limit: 1", "daily_limit", 1),
        ("daily_limit: 500", "daily_limit", 500),
        ("max_turns: 1", "max_turns", 1),
        ("max_turns: 30", "max_turns", 30),
        ("reply_timeout_seconds: 5", "reply_timeout_seconds", 5.0),
        ("reply_timeout_seconds: 60", "reply_timeout_seconds", 60.0),
    ],
)
def test_範囲の端は読める(xdg_dirs: dict[str, Path], line: str, attr: str, value: float) -> None:
    report = _load(xdg_dirs, f"conversation:\n  {line}\n")
    assert _errors(report) == []
    assert getattr(_conversation(report), attr) == value


def test_引用符のない時刻も時と分として読む(xdg_dirs: dict[str, Path]) -> None:
    # YAML 1.1 は 21:30 を 60 進の数（1290）として読むので、時と分に戻す
    report = _load(xdg_dirs, "conversation:\n  hours: [7:05, 21:30]\n")
    assert _errors(report) == []
    assert _conversation(report).hours == (time(7, 5), time(21, 30))


def test_日をまたぐ時間帯は読める(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, 'conversation:\n  hours: ["22:00", "06:00"]\n')
    assert _errors(report) == []
    assert _conversation(report).hours == (time(22, 0), time(6, 0))


# --- 誤り ---


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ("daily_limit: 0", "conversation.daily_limit"),
        ("daily_limit: 501", "conversation.daily_limit"),
        ("max_turns: 0", "conversation.max_turns"),
        ("max_turns: 31", "conversation.max_turns"),
        ("reply_timeout_seconds: 4.9", "conversation.reply_timeout_seconds"),
        ("reply_timeout_seconds: 60.1", "conversation.reply_timeout_seconds"),
    ],
)
def test_範囲の外は場所と理由だけを出す(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture, line: str, key: str
) -> None:
    report = _load(xdg_dirs, f"conversation:\n  {line}\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith(f"{key}: ")
    assert "conversation" not in report.features
    value = line.split(": ", 1)[1]
    assert value not in _all_texts(report, caplog)


@pytest.mark.parametrize(
    ("hours", "key", "reason"),
    [
        ("[7, 21:30]", "conversation.hours.0", MSG_FORMAT),
        ("[0, 21:30]", "conversation.hours.0", MSG_FORMAT),
        ("[7:00, 7.5]", "conversation.hours.1", MSG_FORMAT),
        ("[true, 21:30]", "conversation.hours.0", MSG_FORMAT),
        ("[1440, 21:30]", "conversation.hours.0", MSG_FORMAT),
        ('["07:00", "05:43:21"]', "conversation.hours.1", "時刻に秒は書けません"),
        ('["05:43+09:00", "21:30"]', "conversation.hours.0", "時刻にタイムゾーンは書けません"),
    ],
)
def test_時刻の書き方の誤りは場所と決まった理由だけを出す(
    xdg_dirs: dict[str, Path],
    caplog: pytest.LogCaptureFixture,
    hours: str,
    key: str,
    reason: str,
) -> None:
    report = _load(xdg_dirs, f"conversation:\n  hours: {hours}\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith(f"{key}: ")
    assert reason in errors[0]
    assert "conversation" not in report.features
    texts = _all_texts(report, caplog)
    assert HIDDEN_TIME not in texts
    assert "05:43:21" not in texts
    assert "1440" not in texts


def test_始めと終わりが同じ時間帯は誤り(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    report = _load(xdg_dirs, f'conversation:\n  hours: ["{HIDDEN_TIME}", "{HIDDEN_TIME}"]\n')
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith("conversation.hours: ")
    assert "始めと終わりが同じ" in errors[0]
    assert "conversation" not in report.features
    assert HIDDEN_TIME not in _all_texts(report, caplog)


def test_時間帯が_2_つでなければ誤り(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, 'conversation:\n  hours: ["07:00"]\n')
    errors = _errors(report)
    assert errors
    assert all(e.startswith("conversation.hours") for e in errors)


def test_誤りの文言に待つ時間の値が出ない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    report = _load(xdg_dirs, f"conversation:\n  reply_timeout_seconds: {HIDDEN_SECONDS}\n")
    assert _errors(report)
    assert HIDDEN_SECONDS not in _all_texts(report, caplog)


def test_呼び名の別の書き方が並びでなければ誤りで値を出さない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    report = _load(xdg_dirs, "conversation:\n  call_name_variants: {a: ﾋﾐﾂﾉﾅ}\n")
    errors = _errors(report)
    assert errors
    assert all(e.startswith("conversation.call_name_variants") for e in errors)
    assert "ﾋﾐﾂﾉﾅ" not in _all_texts(report, caplog)


def test_知らない項目は警告になる(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "conversation:\n  unknown_key: 1\n")
    assert _errors(report) == []
    assert "conversation.unknown_key: 知らない項目です" in report.warnings


def test_無効にできる(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "conversation:\n  enabled: false\n")
    assert _conversation(report).enabled is False
