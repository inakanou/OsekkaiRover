"""実機の軽い確かめのテスト（device-deployment 6.1、6.5）。

smbus2・lgpio・pactl・open_jtalk・timedatectl は、偽のオブジェクトを渡して使わない。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from osekkai.adapters.pi.gpio import Gpio
from osekkai.adapters.pi.i2c_bus import I2cBus
from osekkai.adapters.pi.probes import PROBE_PHRASE, PiProbes, ProbeGpio
from osekkai.checks import Probes
from osekkai.core.config import VoiceSettings

# ---- 偽の smbus2 と lgpio ----


class FakeSMBus:
    def __init__(self, answer: set[int]) -> None:
        self.answer = answer
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    def write_quick(self, addr: int) -> None:
        self.calls.append(("quick", addr))
        if addr not in self.answer:
            raise OSError(121, "Remote I/O error")

    def write_byte(self, addr: int, value: int) -> None:  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("書き込んではいけない")

    def write_i2c_block_data(
        self, addr: int, reg: int, data: list[int]
    ) -> None:  # pragma: no cover
        raise AssertionError("書き込んではいけない")

    def close(self) -> None:
        self.closed = True


class LgpioError(Exception):
    pass


class FakeLgpio:
    SET_PULL_UP = 32

    def __init__(self, busy: set[int] | None = None) -> None:
        self.busy = busy or set()
        self.calls: list[tuple[Any, ...]] = []

    def gpiochip_open(self, n: int) -> int:
        self.calls.append(("open", n))
        return 100 + n

    def gpiochip_close(self, handle: int) -> None:
        self.calls.append(("close", handle))

    def gpio_get_chip_info(self, handle: int) -> tuple[int, int, str, str]:
        return (0, 54, "gpiochip0", "pinctrl-rp1")

    def gpio_claim_input(self, handle: int, pin: int, flags: int = 0) -> int:
        if pin in self.busy:
            raise LgpioError("GPIO busy")
        self.calls.append(("in", pin, flags))
        return 0

    def gpio_free(self, handle: int, pin: int) -> int:
        self.calls.append(("free", pin))
        return 0

    def __getattr__(self, name: str) -> Any:  # 出力・書き込み・見張りは呼ばれてはいけない
        raise AssertionError(f"呼んではいけない: {name}")


def _probes(
    *,
    smbus: FakeSMBus | None = None,
    lg: FakeLgpio | None = None,
    run: Any = None,
    **kwargs: Any,
) -> PiProbes:
    fake_bus = smbus or FakeSMBus({0x14})
    fake_lg = lg or FakeLgpio()
    return PiProbes(
        i2c_bus=lambda: I2cBus(opener=lambda _n: fake_bus),
        gpio=lambda: ProbeGpio(lg=fake_lg, environ={}, chips=[0]),
        run=run or _no_run,
        **kwargs,
    )


def _no_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    raise AssertionError("コマンドを呼んではいけない")


def test_確かめの口の形に合う() -> None:
    probes: Probes = _probes()
    assert isinstance(probes, PiProbes)


# ---- I2C ----


def test_I2Cは素早い問い合わせだけで書き込まずバスを閉じる() -> None:
    bus = FakeSMBus({0x27})
    probes = _probes(smbus=bus)
    assert probes.i2c_responds(0x27) is True
    assert bus.closed
    assert probes.i2c_responds(0x14) is False
    assert bus.calls == [("quick", 0x27), ("quick", 0x14)]


def test_バスを開けなければ例外() -> None:
    def opener(_n: int) -> FakeSMBus:
        raise FileNotFoundError("/dev/i2c-1")

    probes = PiProbes(i2c_bus=lambda: I2cBus(opener=opener), run=_no_run)
    with pytest.raises(FileNotFoundError):
        probes.i2c_responds(0x27)


# ---- GPIO ----


def test_線を入力として取ってすぐ放しチップを閉じる() -> None:
    lg = FakeLgpio()
    probes = _probes(lg=lg)
    assert probes.gpio_input_free(25) is True
    assert lg.calls == [
        ("open", 0),
        ("in", 25, FakeLgpio.SET_PULL_UP),
        ("free", 25),
        ("close", 100),
    ]


def test_取られている線はだめでチップを閉じる() -> None:
    lg = FakeLgpio(busy={25})
    probes = _probes(lg=lg)
    assert probes.gpio_input_free(25) is False
    assert ("free", 25) not in lg.calls
    assert lg.calls[-1] == ("close", 100)


def test_ProbeGpioはGpioの仲間() -> None:
    assert issubclass(ProbeGpio, Gpio)


# ---- 再生先 ----


class FakeRun:
    def __init__(self, answers: dict[tuple[str, ...], tuple[int, str]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        self.kwargs.append(kwargs)
        code, out = self.answers[tuple(args)]
        return subprocess.CompletedProcess(args, code, stdout=out, stderr="")


SINKS = ("pactl", "list", "short", "sinks")
DEFAULT = ("pactl", "get-default-sink")
LISTING = "1\talsa_output.a\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED\n2\tbt.b\tPipeWire\t-\tIDLE\n"


def test_設定の再生先が一覧にあればよい() -> None:
    run = FakeRun({SINKS: (0, LISTING)})
    probes = _probes(run=run)
    assert probes.audio_sink_exists("bt.b") is True
    assert probes.audio_sink_exists("other") is False
    assert run.calls == [list(SINKS), list(SINKS)]
    assert all(k.get("timeout") for k in run.kwargs)


def test_空なら既定の再生先が一覧にあるかを見る() -> None:
    assert (
        _probes(
            run=FakeRun({SINKS: (0, LISTING), DEFAULT: (0, "alsa_output.a\n")})
        ).audio_sink_exists("")
        is True
    )
    assert (
        _probes(run=FakeRun({SINKS: (0, LISTING), DEFAULT: (0, "gone\n")})).audio_sink_exists("")
        is False
    )
    assert _probes(run=FakeRun({SINKS: (0, ""), DEFAULT: (1, "")})).audio_sink_exists("") is False


def test_pactlが失敗したら値を含まない例外() -> None:
    probes = _probes(run=FakeRun({SINKS: (1, "")}))
    with pytest.raises(RuntimeError) as info:
        probes.audio_sink_exists("secret-sink")
    assert "secret-sink" not in str(info.value)

    def missing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        raise FileNotFoundError("pactl")

    with pytest.raises(OSError):
        _probes(run=missing).audio_sink_exists("")

    def slow(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        raise subprocess.TimeoutExpired("pactl", 3)

    with pytest.raises(RuntimeError):
        _probes(run=slow).audio_sink_exists("")


# ---- 合成と時刻 ----


def test_合成は決まった短い文をOpenJTalkに渡し再生しない(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        seen.append({"args": list(args), **kwargs})
        assert args[0] != "paplay"
        Path(args[args.index("-ow") + 1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(args, 0)

    normalized: list[Path] = []
    probes = _probes(run=run, normalize=normalized.append)
    out = tmp_path / "a.wav"
    probes.synthesize(VoiceSettings(open_jtalk="open_jtalk"), out)
    assert len(seen) == 1
    assert seen[0]["args"][0] == "open_jtalk"
    assert seen[0]["input"] == (PROBE_PHRASE + "\n").encode("utf-8")
    assert normalized == [out]
    assert 0 < len(PROBE_PHRASE) <= 10


def test_時刻はtimedatectlで判定する(tmp_path: Path) -> None:
    run = FakeRun({("timedatectl", "show", "-p", "NTPSynchronized", "--value"): (0, "yes\n")})
    assert _probes(run=run, timesync_marker=tmp_path / "none").time_synchronized() is True
    run = FakeRun({("timedatectl", "show", "-p", "NTPSynchronized", "--value"): (0, "no\n")})
    assert _probes(run=run, timesync_marker=tmp_path / "none").time_synchronized() is False


def test_モーターとサーボの書き込みの口を持たない() -> None:
    names = {n for n in dir(PiProbes) if not n.startswith("_")}
    assert names == {
        "i2c_responds",
        "gpio_input_free",
        "audio_sink_exists",
        "synthesize",
        "time_synchronized",
    }
