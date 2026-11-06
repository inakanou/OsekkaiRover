"""偽の軽い確かめのテスト（device-deployment 6.1、6.5）。"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from osekkai.adapters.fake.probes import FakeProbes
from osekkai.checks import Probes
from osekkai.core.config import VoiceSettings


def test_確かめの口の形に合う() -> None:
    probes: Probes = FakeProbes()
    assert probes.time_synchronized() is True


def test_既定ではすべてよい() -> None:
    probes = FakeProbes()
    assert probes.i2c_responds(0x27)
    assert probes.i2c_responds(0x14)
    assert probes.gpio_input_free(25)
    assert probes.audio_sink_exists("")
    assert probes.time_synchronized()


def test_だめにできる() -> None:
    probes = FakeProbes(
        answering={0x14}, busy_pins={25}, sinks={"a"}, default_sink=False, synchronized=False
    )
    assert not probes.i2c_responds(0x27)
    assert not probes.gpio_input_free(25)
    assert probes.gpio_input_free(17)
    assert probes.audio_sink_exists("a")
    assert not probes.audio_sink_exists("b")
    assert not probes.audio_sink_exists("")
    assert not probes.time_synchronized()


def test_尋ねたことを記録する() -> None:
    probes = FakeProbes()
    probes.i2c_responds(0x27)
    probes.gpio_input_free(25)
    probes.audio_sink_exists("x")
    assert probes.i2c_queries == [0x27]
    assert probes.gpio_queries == [25]
    assert probes.sink_queries == ["x"]


def test_合成は音のあるWAVを書く(tmp_path: Path) -> None:
    probes = FakeProbes()
    out = tmp_path / "a.wav"
    settings = VoiceSettings()
    probes.synthesize(settings, out)
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() > 0
    assert probes.synthesized == [(settings, out)]
    assert probes.played == 0


def test_合成の失敗と書かないこと(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        FakeProbes(synth_ok=False).synthesize(VoiceSettings(), tmp_path / "a.wav")
    FakeProbes(synth_writes_wav=False).synthesize(VoiceSettings(), tmp_path / "b.wav")
    assert not (tmp_path / "a.wav").exists()
    assert not (tmp_path / "b.wav").exists()
