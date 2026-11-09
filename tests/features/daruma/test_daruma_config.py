"""だるまさんの設定（`daruma:` の節）の形と検証（3.4、7.1、7.3）。"""

from pathlib import Path

import pytest

from osekkai.core.config import ConfigLoader, ConfigReport
from osekkai.core.paths import Paths
from osekkai.features.daruma.config import DarumaConfig

# 誤りの文言に出てはいけない、目立つ値
HIDDEN_SECONDS = "0.537"


def _load(xdg_dirs: dict[str, Path], text: str) -> ConfigReport:
    paths = Paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")
    return ConfigLoader(paths, {"daruma": DarumaConfig}).load()


def _errors(report: ConfigReport) -> list[str]:
    return list(report.feature_errors.get("daruma", []))


def _all_texts(report: ConfigReport, caplog: pytest.LogCaptureFixture) -> str:
    parts = [*report.fatal_errors, *report.warnings, *_errors(report)]
    parts.extend(r.getMessage() for r in caplog.records)
    return "\n".join(parts)


def _daruma(report: ConfigReport) -> DarumaConfig:
    config = report.features["daruma"]
    assert isinstance(config, DarumaConfig)
    return config


# --- 既定値と正しい設定 ---


def test_節がなければ既定値で読める(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "core: {}\n")
    assert _errors(report) == []
    config = _daruma(report)
    assert config.enabled is True
    assert config.call_name == ""
    assert config.threshold == 0.01
    assert config.watch_seconds == 2.5
    assert config.max_rounds == 10  # 4.1
    assert config.end_before_task_min == 10


def test_書いた値で読める(xdg_dirs: dict[str, Path]) -> None:
    text = (
        "daruma:\n"
        "  call_name: ゆう\n"
        "  threshold: 0.05\n"
        "  watch_seconds: 3\n"
        "  max_rounds: 7\n"
        "  end_before_task_min: 15\n"
    )
    report = _load(xdg_dirs, text)
    assert _errors(report) == []
    assert report.warnings == []
    config = _daruma(report)
    assert config.call_name == "ゆう"
    assert config.threshold == 0.05
    assert config.watch_seconds == 3.0
    assert config.max_rounds == 7
    assert config.end_before_task_min == 15


def test_前の版のだれもいないの回数が残っていても読める(xdg_dirs: dict[str, Path]) -> None:
    # やめた項目は知らない項目として注意を出すだけで、遊びは始められる
    report = _load(xdg_dirs, "daruma:\n  max_rounds: 8\n  nobody_rounds: 5\n")
    assert _errors(report) == []
    assert report.warnings == ["daruma.nobody_rounds: 知らない項目です"]
    assert _daruma(report).max_rounds == 8
    assert "nobody_rounds" not in DarumaConfig.model_fields


@pytest.mark.parametrize(
    ("line", "attr", "value"),
    [
        ("threshold: 0.0001", "threshold", 0.0001),
        ("threshold: 0.9999", "threshold", 0.9999),
        ("watch_seconds: 1", "watch_seconds", 1.0),
        ("watch_seconds: 10", "watch_seconds", 10.0),
        ("max_rounds: 1", "max_rounds", 1),
        ("max_rounds: 50", "max_rounds", 50),
        ("end_before_task_min: 0", "end_before_task_min", 0),
        ("end_before_task_min: 120", "end_before_task_min", 120),
    ],
)
def test_範囲の端は読める(xdg_dirs: dict[str, Path], line: str, attr: str, value: float) -> None:
    report = _load(xdg_dirs, f"daruma:\n  {line}\n")
    assert _errors(report) == []
    assert getattr(_daruma(report), attr) == value


# --- 誤り ---


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ("threshold: 0", "daruma.threshold"),
        ("threshold: 1", "daruma.threshold"),
        ("threshold: -0.25", "daruma.threshold"),
        ("watch_seconds: 0.5", "daruma.watch_seconds"),
        ("watch_seconds: 10.5", "daruma.watch_seconds"),
        ("max_rounds: 0", "daruma.max_rounds"),
        ("max_rounds: 51", "daruma.max_rounds"),
        ("end_before_task_min: -1", "daruma.end_before_task_min"),
        ("end_before_task_min: 121", "daruma.end_before_task_min"),
    ],
)
def test_範囲の外は場所と理由だけを出して開始しない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture, line: str, key: str
) -> None:
    report = _load(xdg_dirs, f"daruma:\n  {line}\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith(f"{key}: ")
    assert len(errors[0]) > len(f"{key}: ")  # 理由がある
    assert "daruma" not in report.features
    value = line.split(": ", 1)[1]
    assert value not in _all_texts(report, caplog)


def test_誤りの文言に見る長さの値が出ない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    report = _load(xdg_dirs, f"daruma:\n  watch_seconds: {HIDDEN_SECONDS}\n")
    assert _errors(report)
    assert HIDDEN_SECONDS not in _all_texts(report, caplog)


def test_呼び名が文字でなければ誤りで値を出さない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    report = _load(xdg_dirs, "daruma:\n  call_name: {a: ﾋﾐﾂﾉﾅ}\n")
    errors = _errors(report)
    assert errors
    assert all(e.startswith("daruma.call_name") for e in errors)
    assert "ﾋﾐﾂﾉﾅ" not in _all_texts(report, caplog)


def test_回数は整数でなければ誤り(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "daruma:\n  max_rounds: 2.5\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith("daruma.max_rounds: ")


def test_知らない項目は警告になる(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "daruma:\n  unknown_key: 1\n")
    assert _errors(report) == []
    assert "daruma.unknown_key: 知らない項目です" in report.warnings


def test_無効にできる(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "daruma:\n  enabled: false\n")
    assert _daruma(report).enabled is False
