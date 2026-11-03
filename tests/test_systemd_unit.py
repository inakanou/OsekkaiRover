import re
from pathlib import Path

import pytest

UNIT = Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "osekkai-rover.service"


def _parse(text: str) -> dict[str, dict[str, list[str]]]:
    # systemd は同じキーを何度も書けるので、値をリストで持つ
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        header = re.fullmatch(r"\[(.+)\]", line)
        if header:
            current = sections.setdefault(header.group(1), {})
            continue
        assert current is not None, f"節の外に設定がある: {line}"
        key, sep, value = line.partition("=")
        assert sep, f"= がない行: {line}"
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


@pytest.fixture(scope="module")
def unit() -> dict[str, dict[str, list[str]]]:
    return _parse(UNIT.read_text(encoding="utf-8"))


def _one(unit: dict[str, dict[str, list[str]]], section: str, key: str) -> str:
    values = unit[section][key]
    assert len(values) == 1, f"{key} は 1 つだけのはず: {values}"
    return values[0]


def _python(command: str) -> str:
    return command.split()[0]


def test_ユーザーサービスとして常駐し_osekkai_を起動する(
    unit: dict[str, dict[str, list[str]]],
) -> None:
    assert set(unit) == {"Unit", "Service", "Install"}
    assert _one(unit, "Service", "Type") == "simple"
    start = _one(unit, "Service", "ExecStart")
    assert start.endswith(" -m osekkai")
    assert _python(start).startswith("%h/")
    assert _one(unit, "Install", "WantedBy") == "default.target"


def test_停止は_SIGTERM_を本体に送り_15_秒で打ち切る(
    unit: dict[str, dict[str, list[str]]],
) -> None:
    assert _one(unit, "Service", "KillMode") == "mixed"
    assert _one(unit, "Service", "KillSignal") == "SIGTERM"
    assert _one(unit, "Service", "TimeoutStopSec") == "15s"


def test_どんな終わり方でも同じ_python_で安全停止のコマンドを動かす(
    unit: dict[str, dict[str, list[str]]],
) -> None:
    stop_post = _one(unit, "Service", "ExecStopPost")
    assert stop_post.endswith(" -m osekkai.core.safe_stop")
    assert _python(stop_post) == _python(_one(unit, "Service", "ExecStart"))


def test_異常終了では再起動し_二重起動の_75_では再起動しない(
    unit: dict[str, dict[str, list[str]]],
) -> None:
    assert _one(unit, "Service", "Restart") == "on-failure"
    assert _one(unit, "Service", "RestartSec") == "5s"
    assert _one(unit, "Service", "RestartPreventExitStatus").split() == ["75"]


def test_ログがすぐに届くように_python_の出力をバッファしない(
    unit: dict[str, dict[str, list[str]]],
) -> None:
    assert "PYTHONUNBUFFERED=1" in unit["Service"]["Environment"]
