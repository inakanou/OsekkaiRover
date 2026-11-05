import re
from pathlib import Path

import pytest

from rover_deploy.layout import CORE_UNIT_NAME, SAMPLE_UNIT_NAME

UNIT_DIR = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
UNIT = UNIT_DIR / "osekkai.service"
REMINDER_SERVICE = UNIT_DIR / "osekkai-trial-reminder.service"
REMINDER_TIMER = UNIT_DIR / "osekkai-trial-reminder.timer"

Unit = dict[str, dict[str, list[str]]]


def _parse(text: str) -> Unit:
    # systemd は同じキーを何度も書けるので、値をリストで持つ
    sections: Unit = {}
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
def unit() -> Unit:
    return _parse(UNIT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def reminder_service() -> Unit:
    return _parse(REMINDER_SERVICE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def reminder_timer() -> Unit:
    return _parse(REMINDER_TIMER.read_text(encoding="utf-8"))


def _one(unit: Unit, section: str, key: str) -> str:
    values = unit[section][key]
    assert len(values) == 1, f"{key} は 1 つだけのはず: {values}"
    return values[0]


def _python(command: str) -> str:
    return command.split()[0]


# --- 新しい基盤のユニット（osekkai.service） ---


def test_ユニットは新しい名前で置き_旧名のファイルはない() -> None:
    assert UNIT.name == CORE_UNIT_NAME
    assert UNIT.is_file()
    assert not (UNIT_DIR / SAMPLE_UNIT_NAME).exists()


def test_ユーザーサービスとして常駐し_osekkai_を起動する(unit: Unit) -> None:
    assert set(unit) == {"Unit", "Service", "Install"}
    assert _one(unit, "Service", "Type") == "simple"
    start = _one(unit, "Service", "ExecStart")
    assert start.endswith(" -m osekkai")
    assert _python(start).startswith("%h/")
    assert _one(unit, "Install", "WantedBy") == "default.target"


def test_版のリンクの下の作業コピーと仮想環境で動かす(unit: Unit) -> None:
    current = "%h/osekkai-rover/current"
    assert _one(unit, "Service", "WorkingDirectory") == current
    assert _python(_one(unit, "Service", "ExecStart")) == f"{current}/.venv/bin/python"
    assert _python(_one(unit, "Service", "ExecStopPost")) == f"{current}/.venv/bin/python"


def test_サンプルのユニットとは同時に動かない(unit: Unit) -> None:
    assert _one(unit, "Unit", "Conflicts").split() == [SAMPLE_UNIT_NAME]


def test_再生のサービスの後に起動する(unit: Unit) -> None:
    assert _one(unit, "Unit", "After").split() == ["pipewire-pulse.service"]
    assert _one(unit, "Unit", "Wants").split() == ["pipewire-pulse.service"]


def test_停止は_SIGTERM_を本体に送り_15_秒で打ち切る(unit: Unit) -> None:
    assert _one(unit, "Service", "KillMode") == "mixed"
    assert _one(unit, "Service", "KillSignal") == "SIGTERM"
    assert _one(unit, "Service", "TimeoutStopSec") == "15s"


def test_どんな終わり方でも同じ_python_で安全停止のコマンドを動かす(unit: Unit) -> None:
    stop_post = _one(unit, "Service", "ExecStopPost")
    # 引数なし（--strict を付けない）なので、いつも 0 で終わる
    assert stop_post.split()[1:] == ["-m", "osekkai.core.safe_stop"]
    assert _python(stop_post) == _python(_one(unit, "Service", "ExecStart"))


def test_異常終了では再起動し_二重起動の_75_では再起動しない(unit: Unit) -> None:
    assert _one(unit, "Service", "Restart") == "on-failure"
    assert _one(unit, "Service", "RestartSec") == "5s"
    assert _one(unit, "Service", "RestartPreventExitStatus").split() == ["75"]


def test_ログがすぐに届くように_python_の出力をバッファしない(unit: Unit) -> None:
    assert "PYTHONUNBUFFERED=1" in unit["Service"]["Environment"]


# --- 戻し忘れの確認（osekkai-trial-reminder） ---


def test_戻し忘れの確認は_写した道具の_remind_を_1_回だけ動かす(
    reminder_service: Unit,
) -> None:
    # タイマーから起こすので、[Install] は持たない
    assert set(reminder_service) == {"Unit", "Service"}
    assert _one(reminder_service, "Service", "Type") == "oneshot"
    assert _one(reminder_service, "Service", "WorkingDirectory") == "%h/osekkai-rover/tool"
    start = _one(reminder_service, "Service", "ExecStart")
    # 道具はシステムの python3 の標準ライブラリだけで動く
    assert start.split() == ["/usr/bin/python3", "-m", "rover_deploy.agent", "remind"]


def test_タイマーは起動の_10_分後と_その後_1_時間ごとに確認する(
    reminder_timer: Unit,
) -> None:
    assert set(reminder_timer) == {"Unit", "Timer", "Install"}
    assert _one(reminder_timer, "Timer", "OnBootSec") == "10min"
    assert _one(reminder_timer, "Timer", "OnUnitActiveSec") == "1h"
    assert _one(reminder_timer, "Timer", "Unit") == REMINDER_SERVICE.name
    assert REMINDER_TIMER.stem == REMINDER_SERVICE.stem
    assert _one(reminder_timer, "Install", "WantedBy") == "timers.target"
