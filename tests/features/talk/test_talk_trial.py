"""試しのお話のコマンドのテスト（11.3〜11.5）。

模擬のデバイス（`--fake-hardware`）と一時の XDG のディレクトリで動かす。偽のハードでは
外へ問い合わせない偽の天気・注意報・豆知識を使う（12.2）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from osekkai.core.instance_lock import InstanceLock
from osekkai.core.paths import Paths
from osekkai.core.quiet import QuietMode
from osekkai.features.talk import texts, trial
from osekkai.features.talk.adapters.canned import CannedAlerts, CannedFacts, CannedWeather
from osekkai.features.talk.config import TalkConfig
from osekkai.features.talk.model import TalkKind
from osekkai.features.talk.topics import topic_hash

HIDDEN_NAME = "てすとくん"
HIDDEN_TOPIC = "きょうりゅう"


def _section(**fields: Any) -> dict[str, Any]:
    section: dict[str, Any] = {
        "enabled": True,
        "call_name": HIDDEN_NAME,
        "location": {"latitude": 35.68, "longitude": 139.69},
        "warning_area": {"office": "130000", "city": "1311300"},
        "topics": [HIDDEN_TOPIC],
    }
    section.update(fields)
    return section


def _write_config(**fields: Any) -> None:
    paths = Paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump({"talk": _section(**fields)}, allow_unicode=True, sort_keys=False)
    paths.config_file.write_text(text, encoding="utf-8")


def _talk_dir() -> Path:
    return Paths().state_dir_for("talk")


def _seed_record() -> Path:
    """本番の記録を置く（前後の日も含めて話し済み、直前の好きなものあり）。"""
    directory = _talk_dir()
    directory.mkdir(parents=True, exist_ok=True)
    today = date.today()  # テストの側だけ。日付が日本とずれても前後の日で覆う
    days = {
        (today + timedelta(days=offset)).isoformat(): {"night": "spoken", "morning": "spoken"}
        for offset in (-1, 0, 1)
    }
    data = {"version": 1, "days": days, "last_topic": topic_hash(HIDDEN_TOPIC)}
    path = directory / "record.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(directory.glob("*"))
        if p.is_file()
    }


def _voice_lines(out: str) -> list[str]:
    return [line.removeprefix("[声] ") for line in out.splitlines() if line.startswith("[声] ")]


def _voice_text(out: str) -> str:
    """声の全体。声の口は「。」で文を分けて合成するので、つないで見る。"""
    return "".join(_voice_lines(out))


@pytest.fixture
def no_devices(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """デバイスを作ろうとしたら記録する（作らずに終える場面で使う）。"""
    built: list[str] = []

    def fake_build(*args: object, **kwargs: object) -> object:
        built.append("built")
        raise AssertionError("デバイスを作ってはいけない")

    monkeypatch.setattr(trial, "build_devices", fake_build)
    return built


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """本物の出どころを選んだら失敗にする（偽のハードでは外へ問い合わせない。12.2）。"""

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("偽のハードで本物の出どころを選んではいけない")

    monkeypatch.setattr(trial, "choose_sources", refuse)


# --- 話す ---


@pytest.mark.parametrize(
    ("option", "kind"), [("night", TalkKind.NIGHT), ("morning", TalkKind.MORNING)]
)
def test_偽のハードで夜と朝のお話を時刻にかかわらず_1_回話し_記録を変えない(
    xdg_dirs: dict[str, Path],
    no_network: None,
    capsys: pytest.CaptureFixture[str],
    option: str,
    kind: TalkKind,
) -> None:
    _write_config()
    _seed_record()  # 本番なら話し済みの日でも話す（11.3）
    before = _snapshot(_talk_dir())

    assert trial.main(["--try", option, "--fake-hardware"]) == 0

    out, err = capsys.readouterr()
    voices = _voice_lines(out)
    spoken = _voice_text(out)
    opener = texts.fill_name(texts.OPENERS[kind], HIDDEN_NAME)
    closer = texts.fill_name(texts.CLOSERS[kind], HIDDEN_NAME)
    assert spoken.startswith(opener)
    assert spoken.endswith(closer)
    assert spoken.count(opener) == 1  # 1 回だけ話した（11.3）
    # 夜はあしたの、朝は今日の天気（偽の天気）。好きなものの豆知識（偽の豆知識）
    assert any(line.startswith(texts.DAY_WORDS[kind]) for line in voices)
    assert any(line.startswith(HIDDEN_TOPIC) for line in voices)
    assert out.strip().splitlines()[-1] == trial.SPOKEN_LABEL
    # その日の記録と直前の好きなものを変えない（11.4）
    assert _snapshot(_talk_dir()) == before
    # ログには呼び名・好きなものを出さない
    for hidden in (HIDDEN_NAME, HIDDEN_TOPIC):
        assert hidden not in err


def test_記録のファイルがなければ試しの後もないまま(
    xdg_dirs: dict[str, Path], no_network: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config()
    assert trial.main(["--try", "night", "--fake-hardware"]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == trial.SPOKEN_LABEL
    assert not (_talk_dir() / "record.json").exists()


def test_天気も好きなものもなければ決まった短い言葉だけを話す(
    xdg_dirs: dict[str, Path], no_network: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(location=None, warning_area=None, topics=[])
    assert trial.main(["--try", "morning", "--fake-hardware"]) == 0
    out = capsys.readouterr().out
    assert _voice_text(out) == texts.FALLBACKS[TalkKind.MORNING]


# --- 静かな状態 ---


def test_静かな状態なら話さず_話さなかったことを表示して_0_で終える(
    xdg_dirs: dict[str, Path],
    no_network: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config()
    monkeypatch.setattr(QuietMode, "is_quiet", property(lambda self: True))

    assert trial.main(["--try", "night", "--fake-hardware"]) == 0

    out = capsys.readouterr().out
    assert _voice_lines(out) == []
    assert out.strip().splitlines()[-1] == trial.QUIET_MESSAGE
    assert trial.QUIET_MESSAGE == "おやすみモードなので はなしませんでした"
    assert not (_talk_dir() / "record.json").exists()


def test_おやすみモードのまま止めたロボットでも話さない(
    xdg_dirs: dict[str, Path], no_network: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config()
    core = Paths().state_dir_for("core")
    core.mkdir(parents=True, exist_ok=True)
    (core / "quiet.json").write_text(
        json.dumps({"quiet": True, "changed_at": "2026-11-20T21:00:00+09:00"}), encoding="utf-8"
    )

    assert trial.main(["--try", "morning", "--fake-hardware"]) == 0

    out = capsys.readouterr().out
    assert _voice_lines(out) == []
    assert out.strip().splitlines()[-1] == trial.QUIET_MESSAGE


# --- 始めずに終える ---


@pytest.fixture
def held_lock(xdg_dirs: dict[str, Path]) -> Iterator[InstanceLock]:
    lock = InstanceLock(Paths().lock_file)
    assert lock.acquire()
    yield lock
    lock.release()


def test_ロボットが動いていれば理由を出して_75_で終え_デバイスを作らない(
    held_lock: InstanceLock, no_devices: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config()
    assert trial.main(["--try", "night", "--fake-hardware"]) == 75
    assert trial.LOCK_BUSY_MESSAGE in capsys.readouterr().err
    assert no_devices == []


def test_お話が無効なら理由を出して_1_で終え_デバイスを作らない(
    xdg_dirs: dict[str, Path], no_devices: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(enabled=False)
    assert trial.main(["--try", "night", "--fake-hardware"]) == 1
    assert trial.DISABLED_MESSAGE in capsys.readouterr().err
    assert no_devices == []
    # 試しの後はロックを放している
    probe = InstanceLock(Paths().lock_file)
    assert probe.acquire()
    probe.release()


def test_お話の設定の誤りなら値を出さずに理由を出して_1_で終える(
    xdg_dirs: dict[str, Path], no_devices: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(rain_percent=0)
    assert trial.main(["--try", "night", "--fake-hardware"]) == 1
    err = capsys.readouterr().err
    assert "talk" in err
    assert HIDDEN_NAME not in err and HIDDEN_TOPIC not in err
    assert no_devices == []


def test_設定ファイルを指定できる(
    xdg_dirs: dict[str, Path], tmp_path: Path, no_devices: list[str]
) -> None:
    config = tmp_path / "my.yaml"
    text = yaml.safe_dump({"talk": _section(enabled=False)}, allow_unicode=True)
    config.write_text(text, encoding="utf-8")
    # 指定したファイルを読む（無効なので 1）
    assert trial.main(["--try", "night", "--fake-hardware", "--config", str(config)]) == 1
    assert not Paths().config_file.exists()


def test_夜と朝のほかは使い方の誤りにする(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        trial.main(["--try", "noon"])
    assert raised.value.code == 2


# --- 部品 ---


def test_天気の日は夜があした_朝が今日() -> None:
    today = date(2026, 11, 20)
    assert trial.weather_day(TalkKind.NIGHT, today) == date(2026, 11, 21)
    assert trial.weather_day(TalkKind.MORNING, today) == today


def test_偽のハードの出どころは外へ問い合わせない偽物で_設定のないものは使わない() -> None:
    config = TalkConfig.model_validate(_section())
    sources = trial.fake_sources(config)
    assert isinstance(sources.weather, CannedWeather)
    assert isinstance(sources.alerts, CannedAlerts)
    assert isinstance(sources.facts, CannedFacts)
    bare = trial.fake_sources(
        TalkConfig.model_validate(_section(location=None, warning_area=None, topics=[]))
    )
    assert (bare.weather, bare.alerts, bare.facts) == (None, None, None)


def test_python_m_で使い方を出せる() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "osekkai.features.talk", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    for flag in ("--try", "--fake-hardware", "--config"):
        assert flag in result.stdout
