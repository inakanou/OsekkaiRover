import wave
from pathlib import Path

import pytest

from osekkai.adapters.fake import FakeDeviceSet, build_fake_devices
from osekkai.adapters.fake.clock import FakeClock
from osekkai.adapters.fake.devices import (
    Call,
    FakeButtons,
    FakeDeviceError,
    FakeDisplay,
    FakeHat,
    FakeLed,
    FakeMotors,
    FakePlayer,
    FakeServos,
    FakeSynth,
    FakeTimeSync,
)
from osekkai.core.config import VoiceSettings
from osekkai.core.ports import (
    AudioPlayerPort,
    ButtonPort,
    Devices,
    DisplayPort,
    HatPort,
    LedPort,
    MotorPort,
    ServoPort,
    SpeechSynthPort,
    TimeSyncPort,
    Unavailable,
)


def test_偽物はポートの形を満たす() -> None:
    # mypy が代入の型を確かめる
    display: DisplayPort = FakeDisplay()
    led: LedPort = FakeLed()
    buttons: ButtonPort = FakeButtons()
    servos: ServoPort = FakeServos()
    motors: MotorPort = FakeMotors()
    synth: SpeechSynthPort = FakeSynth()
    player: AudioPlayerPort = FakePlayer()
    timesync: TimeSyncPort = FakeTimeSync()
    hat: HatPort = FakeHat()
    assert all(
        d is not None for d in (display, led, buttons, servos, motors, synth, player, timesync, hat)
    )


def test_操作を時刻付きで順に記録する(clock: FakeClock) -> None:
    display = FakeDisplay(clock)
    start = clock.monotonic()
    display.clear()
    clock.advance(1.5)
    display.write_line(0, [0x41] * 16)
    display.set_backlight(False)
    assert display.calls == [
        Call(start, "clear", ()),
        Call(start + 1.5, "write_line", (0, (0x41,) * 16)),
        Call(start + 1.5, "set_backlight", (False,)),
    ]
    assert display.ops() == ["clear", "write_line", "set_backlight"]


def test_画面は今の表示を覚える(clock: FakeClock) -> None:
    display = FakeDisplay(clock)
    display.write_line(1, [0x20] * 16)
    assert display.rows[1] == (0x20,) * 16
    display.clear()
    assert display.rows == [(), ()]
    display.set_backlight(False)
    assert display.backlight is False


def test_LED_とサーボとモーターは今の状態を覚える(clock: FakeClock) -> None:
    led = FakeLed(clock)
    led.set(True)
    assert led.on is True

    servos = FakeServos(clock)
    servos.set_angle("pan", 15.0)
    assert servos.angles == {"pan": 15.0}

    motors = FakeMotors(clock)
    motors.set_speeds(40, -40)
    assert motors.speeds == (40, -40)
    motors.stop_now()
    assert motors.speeds == (0, 0)
    assert motors.ops() == ["set_speeds", "stop_now"]


def test_ボタンの押下を模擬すると登録した関数に知らせる(clock: FakeClock) -> None:
    buttons = FakeButtons(clock)
    got: list[tuple[str, float]] = []
    buttons.start(lambda source, at: got.append((source, at)))
    buttons.press()
    clock.advance(2)
    buttons.press("extra")
    start = clock.monotonic() - 2
    assert got == [("usr", start), ("extra", start + 2)]


def test_閉じた後と始める前のボタンの押下は知らせない(clock: FakeClock) -> None:
    buttons = FakeButtons(clock)
    got: list[str] = []
    buttons.press()
    buttons.start(lambda source, at: got.append(source))
    buttons.close()
    buttons.press()
    assert got == []
    assert buttons.ops() == ["press", "start", "close", "press"]


def test_合成は_WAV_として読めるファイルを書く(tmp_path: Path, clock: FakeClock) -> None:
    synth = FakeSynth(clock)
    out = tmp_path / "a.wav"
    synth.synthesize("こんにちは", VoiceSettings(), out)
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() > 0
    assert synth.calls[0].op == "synthesize"
    assert synth.calls[0].args[0] == "こんにちは"
    assert synth.lines == ["こんにちは"]


def test_合成の失敗を起こせる(tmp_path: Path) -> None:
    synth = FakeSynth(fail=True)
    out = tmp_path / "a.wav"
    with pytest.raises(FakeDeviceError):
        synth.synthesize("やあ", VoiceSettings(), out)
    assert not out.exists()
    assert synth.ops() == ["synthesize"]
    synth.fail_on.clear()
    synth.synthesize("やあ", VoiceSettings(), out)
    assert out.exists()


def test_再生は再生したファイルを記録し_失敗も起こせる(tmp_path: Path) -> None:
    player = FakePlayer()
    player.play(tmp_path / "a.wav", "speaker")
    assert player.played == [(tmp_path / "a.wav", "speaker")]
    with pytest.raises(FakeDeviceError):
        FakePlayer(fail=True).play(tmp_path / "a.wav", "")


def test_どの操作でも失敗を起こせる() -> None:
    hat = FakeHat()
    hat.fail_on.add("reset")
    with pytest.raises(FakeDeviceError):
        hat.reset()
    assert hat.ops() == ["reset"]


def test_時刻の同期の状態を切り替えられる() -> None:
    timesync = FakeTimeSync(synchronized=False)
    assert timesync.is_synchronized() is False
    timesync.set_synchronized(True)
    assert timesync.is_synchronized() is True


def test_偽のデバイス一式を組み立てる(clock: FakeClock) -> None:
    devices = build_fake_devices(clock)
    assert isinstance(devices, Devices)
    assert isinstance(devices.display, FakeDisplay)
    assert isinstance(devices.motors, FakeMotors)
    clock.advance(10)
    devices.motors.stop_now()
    assert devices.motors.calls == [Call(clock.monotonic(), "stop_now", ())]


def test_準備に失敗したデバイスを使えないものにできる() -> None:
    devices = build_fake_devices(fail={"display", "motors"})
    assert devices.display == Unavailable("display", "fake: preparation failed")
    assert isinstance(devices.motors, Unavailable)
    assert isinstance(devices.led, FakeLed)


def test_時刻の同期と知らない名前は使えないものにできない() -> None:
    with pytest.raises(ValueError):
        build_fake_devices(fail={"timesync"})
    with pytest.raises(ValueError):
        build_fake_devices(fail={"wheels"})


def test_型付きの偽物一式から同じ偽物を取り出せる(fakes: FakeDeviceSet) -> None:
    devices = fakes.to_devices()
    assert devices.display is fakes.display
    assert devices.timesync is fakes.timesync


def test_共通の準備の偽のデバイス一式は共通の時計を使う(
    fake_devices: Devices, clock: FakeClock
) -> None:
    clock.advance(3600)
    assert isinstance(devices_led := fake_devices.led, FakeLed)
    devices_led.set(True)
    assert devices_led.calls[-1].at == clock.monotonic()


def test_一時の_XDG_ディレクトリを使う(xdg_dirs: dict[str, Path], tmp_path: Path) -> None:
    import os

    for name, path in xdg_dirs.items():
        assert os.environ[name] == str(path)
        assert path.is_dir()
        assert tmp_path in path.parents
    assert set(xdg_dirs) == {
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
