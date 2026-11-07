"""基盤の確認の項目のテスト（device-deployment 6.1、6.5、6.6）。偽の確かめを使う。"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from osekkai.adapters.fake.probes import FakeProbes
from osekkai.checks import (
    REASON_CALIBRATION,
    REASON_CONFIG_MISSING,
    REASON_NO_ANSWER,
    REASON_NO_SINK,
    REASON_NO_WAV,
    REASON_NOT_SYNCED,
    REASON_NOT_WRITABLE,
    REASON_PIN_BUSY,
    base_checks,
    config_reason,
)
from osekkai.core.config import (
    Calibration,
    ConfigReport,
    CoreConfig,
    HardwareSettings,
    MotionSettings,
    VoiceSettings,
)
from osekkai.core.paths import Paths
from osekkai.core.selfcheck import (
    REASON_SKIPPED_BUSY,
    CheckContext,
    CheckResult,
    CheckStatus,
    SelfCheckRunner,
    collect_checks,
)
from osekkai.core.status import ServiceSnapshot

BASE_NAMES = [
    "core.config",
    "core.storage",
    "core.timesync",
    "core.voice_synth",
    "core.audio_sink",
    "core.lcd",
    "core.hat",
    "core.buttons",
    "core.servos",
]
HIDDEN_SINK = "hidden-sink-name"


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
        home=tmp_path,
        uid=1000,
    )


def _ctx(
    tmp_path: Path,
    *,
    core: CoreConfig | None = None,
    report: ConfigReport | None = None,
    hardware_allowed: bool = True,
    service: ServiceSnapshot | None = None,
) -> CheckContext:
    return CheckContext(
        paths=_paths(tmp_path),
        report=report or ConfigReport(core=core or CoreConfig(), features={}),
        service=service,
        hardware_allowed=hardware_allowed,
    )


def _run(probes: FakeProbes, ctx: CheckContext) -> dict[str, CheckResult]:
    return dict(SelfCheckRunner().run(base_checks(probes), ctx))


def _one(probes: FakeProbes, ctx: CheckContext, name: str) -> CheckResult:
    return _run(probes, ctx)[name]


# --- 並びと印 ---


def test_基盤の項目の名前と並び() -> None:
    assert [c.name for c in base_checks(FakeProbes())] == BASE_NAMES


def test_ハードの項目の印とデバイスの名前() -> None:
    checks = {c.name: c for c in base_checks(FakeProbes())}
    hardware = {n: c.device for n, c in checks.items() if c.hardware}
    assert hardware == {
        "core.lcd": "display",
        "core.hat": "hat",
        "core.buttons": "buttons",
        "core.servos": "servos",
    }
    for name in ("core.config", "core.storage", "core.timesync", "core.voice_synth"):
        assert checks[name].hardware is False
    assert checks["core.audio_sink"].hardware is False


def test_基盤の項目は機能の項目の前に並べられる() -> None:
    assert [c.name for c in collect_checks(base_checks(FakeProbes()), [])] == BASE_NAMES


def test_すべてよいとき(tmp_path: Path) -> None:
    results = _run(FakeProbes(), _ctx(tmp_path))
    assert {n: r.status for n, r in results.items()} == dict.fromkeys(BASE_NAMES, CheckStatus.OK)


# --- 設定 ---


def test_設定の致命の誤りはだめで件数だけを出す(tmp_path: Path) -> None:
    report = ConfigReport(
        core=CoreConfig(),
        features={},
        fatal_errors=["core.voice.sink: hidden-value は違います", "core.cache_max_mb: 1"],
    )
    result = _one(FakeProbes(), _ctx(tmp_path, report=report), "core.config")
    assert result == CheckResult(CheckStatus.FAILED, "設定の誤りがある（2 件）")
    assert "hidden" not in result.reason


def test_設定ファイルがなければだめ(tmp_path: Path) -> None:
    report = ConfigReport(
        core=CoreConfig(), features={}, fatal_errors=["設定ファイルがありません"], missing=True
    )
    result = _one(FakeProbes(), _ctx(tmp_path, report=report), "core.config")
    assert result == CheckResult(CheckStatus.FAILED, REASON_CONFIG_MISSING)


def test_機能の節の誤りもだめにする(tmp_path: Path) -> None:
    report = ConfigReport(
        core=CoreConfig(),
        features={},
        feature_errors={"hello": ["hello.name: 子どもの名前"], "dance": ["a", "b"]},
    )
    result = _one(FakeProbes(), _ctx(tmp_path, report=report), "core.config")
    assert result == CheckResult(CheckStatus.FAILED, config_reason(3))
    assert "hello" not in result.reason
    assert "名前" not in result.reason


def test_警告だけならよい(tmp_path: Path) -> None:
    report = ConfigReport(core=CoreConfig(), features={}, warnings=["core.unknown_key"])
    assert _one(FakeProbes(), _ctx(tmp_path, report=report), "core.config").status is (
        CheckStatus.OK
    )


# --- 保存先 ---


def test_保存先に書いて消せる(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert _one(FakeProbes(), ctx, "core.storage").status is CheckStatus.OK
    # 一時ファイルは残さない（ディレクトリは作ってよい）
    assert list(ctx.paths.state_dir.iterdir()) == []
    assert list(ctx.paths.cache_dir.iterdir()) == []


def test_保存先に書けなければだめ(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.paths.cache_dir.parent.mkdir(parents=True, exist_ok=True)
    ctx.paths.cache_dir.write_text("ディレクトリではない")
    result = _one(FakeProbes(), ctx, "core.storage")
    assert result == CheckResult(CheckStatus.FAILED, REASON_NOT_WRITABLE)


# --- 時刻 ---


def test_時刻が同期していなければだめ(tmp_path: Path) -> None:
    result = _one(FakeProbes(synchronized=False), _ctx(tmp_path), "core.timesync")
    assert result == CheckResult(CheckStatus.FAILED, REASON_NOT_SYNCED)


# --- 声の合成 ---


def test_声の合成は設定を使い一時ディレクトリにWAVを作って消す(tmp_path: Path) -> None:
    probes = FakeProbes()
    voice = VoiceSettings(pitch=1.0)
    ctx = _ctx(tmp_path, core=CoreConfig(voice=voice))
    assert _one(probes, ctx, "core.voice_synth").status is CheckStatus.OK
    assert len(probes.synthesized) == 1
    settings, out = probes.synthesized[0]
    assert settings == voice
    assert not out.exists()  # 一時ディレクトリごと消す
    assert not out.is_relative_to(tmp_path)  # 保存先には作らない
    assert probes.played == 0  # 鳴らさない


def test_WAVができなければだめ(tmp_path: Path) -> None:
    result = _one(FakeProbes(synth_writes_wav=False), _ctx(tmp_path), "core.voice_synth")
    assert result == CheckResult(CheckStatus.FAILED, REASON_NO_WAV)


def test_空のWAVはだめ(tmp_path: Path) -> None:
    class EmptyWav(FakeProbes):
        def synthesize(self, settings: VoiceSettings, out: Path) -> None:
            with wave.open(str(out), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(48000)

    result = _one(EmptyWav(), _ctx(tmp_path), "core.voice_synth")
    assert result == CheckResult(CheckStatus.FAILED, REASON_NO_WAV)


def test_合成の例外は型の名前だけになる(tmp_path: Path) -> None:
    result = _one(FakeProbes(synth_ok=False), _ctx(tmp_path), "core.voice_synth")
    assert result == CheckResult(CheckStatus.FAILED, "RuntimeError")


def test_合成は時間に余裕を持たせる() -> None:
    checks = {c.name: c for c in base_checks(FakeProbes())}
    assert checks["core.voice_synth"].timeout_s >= 30


# --- 再生先 ---


def test_設定の再生先があればよい(tmp_path: Path) -> None:
    probes = FakeProbes(sinks={HIDDEN_SINK}, default_sink=False)
    ctx = _ctx(tmp_path, core=CoreConfig(voice=VoiceSettings(sink=HIDDEN_SINK)))
    assert _one(probes, ctx, "core.audio_sink").status is CheckStatus.OK
    assert probes.sink_queries == [HIDDEN_SINK]


def test_空の再生先は既定の再生先を見る(tmp_path: Path) -> None:
    probes = FakeProbes(default_sink=True)
    assert _one(probes, _ctx(tmp_path), "core.audio_sink").status is CheckStatus.OK
    assert probes.sink_queries == [""]
    result = _one(FakeProbes(default_sink=False), _ctx(tmp_path), "core.audio_sink")
    assert result == CheckResult(CheckStatus.FAILED, REASON_NO_SINK)


def test_再生先がなければだめで名前を出さない(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, core=CoreConfig(voice=VoiceSettings(sink=HIDDEN_SINK)))
    result = _one(FakeProbes(sinks=set()), ctx, "core.audio_sink")
    assert result == CheckResult(CheckStatus.FAILED, REASON_NO_SINK)


# --- LCD・HAT ---


def test_LCDは設定のアドレスに尋ねる(tmp_path: Path) -> None:
    probes = FakeProbes(answering={0x3F})
    ctx = _ctx(tmp_path, core=CoreConfig(hardware=HardwareSettings(lcd_address=0x3F)))
    assert _one(probes, ctx, "core.lcd").status is CheckStatus.OK
    result = _one(FakeProbes(answering={0x14}), ctx, "core.lcd")
    assert result == CheckResult(CheckStatus.FAILED, REASON_NO_ANSWER)
    assert "3f" not in result.reason.lower()


def test_HATはどれかのアドレスが応えればよい(tmp_path: Path) -> None:
    assert _one(FakeProbes(answering={0x15}), _ctx(tmp_path), "core.hat").status is (CheckStatus.OK)
    result = _one(FakeProbes(answering={0x27}), _ctx(tmp_path), "core.hat")
    assert result == CheckResult(CheckStatus.FAILED, REASON_NO_ANSWER)


# --- ボタン ---


def test_ボタンはUSRと足したボタンの線を確かめる(tmp_path: Path) -> None:
    probes = FakeProbes()
    ctx = _ctx(tmp_path, core=CoreConfig(hardware=HardwareSettings(extra_button_pin="D0")))
    assert _one(probes, ctx, "core.buttons").status is CheckStatus.OK
    assert probes.gpio_queries == [25, 17]


def test_線を取れなければだめ(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, core=CoreConfig(hardware=HardwareSettings(extra_button_pin="D0")))
    result = _one(FakeProbes(busy_pins={17}), ctx, "core.buttons")
    assert result == CheckResult(CheckStatus.FAILED, REASON_PIN_BUSY)
    assert "17" not in result.reason
    assert "D0" not in result.reason


# --- サーボ ---


def test_サーボはHATの応答と調整値を見て動かさない(tmp_path: Path) -> None:
    probes = FakeProbes()
    assert _one(probes, _ctx(tmp_path), "core.servos").status is CheckStatus.OK
    assert probes.played == 0
    result = _one(FakeProbes(answering={0x27}), _ctx(tmp_path), "core.servos")
    assert result == CheckResult(CheckStatus.FAILED, REASON_NO_ANSWER)


@pytest.mark.parametrize(
    "calibration",
    [
        {"steering": 25.0},
        {"pan": -20.5},
        {"tilt": float("nan")},
        {"steering": float("inf")},
        {"motor_direction": (1, 0)},
    ],
)
def test_調整値が範囲の外ならだめ(tmp_path: Path, calibration: dict[str, object]) -> None:
    cal = Calibration().model_copy(update=calibration)  # 検証を通らない値を作る
    core = CoreConfig().model_copy(update={"motion": MotionSettings(calibration=cal)})
    result = _one(FakeProbes(), _ctx(tmp_path, core=core), "core.servos")
    assert result == CheckResult(CheckStatus.FAILED, REASON_CALIBRATION)
    assert "25" not in result.reason


@pytest.mark.parametrize(
    "values",
    [
        {"steering": 0.0, "pan": 30.0, "tilt": 0.0, "motor_direction": (1, 1)},
        {"steering": 0.0, "pan": 0.0, "tilt": float("nan"), "motor_direction": (1, 1)},
        {"steering": 0.0, "pan": 0.0, "tilt": 0.0, "motor_direction": (1, 0)},
    ],
)
def test_検証を通らない調整値はだめ(tmp_path: Path, values: dict[str, object]) -> None:
    cal = Calibration.model_construct(**values)  # type: ignore[arg-type]
    core = CoreConfig().model_copy(update={"motion": MotionSettings(calibration=cal)})
    result = _one(FakeProbes(), _ctx(tmp_path, core=core), "core.servos")
    assert result == CheckResult(CheckStatus.FAILED, REASON_CALIBRATION)


# --- ハードを触ってよくないとき ---


def test_ハードを触れないときはハードの確かめを呼ばない(tmp_path: Path) -> None:
    probes = FakeProbes()
    results = _run(probes, _ctx(tmp_path, hardware_allowed=False))
    for name in ("core.lcd", "core.hat", "core.buttons", "core.servos"):
        assert results[name] == CheckResult(CheckStatus.SKIPPED, REASON_SKIPPED_BUSY)
    assert probes.i2c_queries == []
    assert probes.gpio_queries == []


def test_サービスの報告でハードの項目を判定する(tmp_path: Path) -> None:
    service = ServiceSnapshot(
        pid=1, version="v0.2.0", heartbeat=1.0, driving=False, unavailable=("display",)
    )
    results = _run(FakeProbes(), _ctx(tmp_path, hardware_allowed=False, service=service))
    assert results["core.lcd"].status is CheckStatus.FAILED
    assert results["core.hat"].status is CheckStatus.OK


# --- 理由に値を出さない ---


def test_だめの理由に設定の値が出ない(tmp_path: Path) -> None:
    probes = FakeProbes(
        answering=set(),
        busy_pins={25, 17},
        sinks=set(),
        default_sink=False,
        synchronized=False,
        synth_ok=False,
    )
    core = CoreConfig(
        voice=VoiceSettings(sink=HIDDEN_SINK, open_jtalk="/opt/hidden/open_jtalk"),
        hardware=HardwareSettings(lcd_address=0x3F, extra_button_pin="D0"),
    )
    report = ConfigReport(core=core, features={}, fatal_errors=[f"core.voice.sink: {HIDDEN_SINK}"])
    ctx = _ctx(tmp_path, report=report)
    ctx.paths.cache_dir.parent.mkdir(parents=True, exist_ok=True)
    ctx.paths.cache_dir.write_text("x")
    results = _run(probes, ctx)
    assert all(r.status is CheckStatus.FAILED for r in results.values())
    fixed = {
        REASON_NOT_WRITABLE,
        REASON_NOT_SYNCED,
        REASON_NO_SINK,
        REASON_NO_ANSWER,
        REASON_PIN_BUSY,
        config_reason(1),
        "RuntimeError",
    }
    for result in results.values():
        assert result.reason in fixed
        for value in (HIDDEN_SINK, "hidden", "0x3f", "63", "D0", str(tmp_path)):
            assert value not in result.reason
