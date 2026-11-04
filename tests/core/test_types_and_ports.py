import dataclasses
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

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
from osekkai.core.types import (
    ButtonPress,
    DeviceUnavailable,
    Face,
    Glyph,
    LeaseRevoked,
    MotionAborted,
    Priority,
    Resource,
    ResourceBusy,
    SpeechFailed,
)


def test_資源の種類は6つ() -> None:
    assert {r.value for r in Resource} == {"voice", "display", "led", "button", "neck", "drive"}


def test_優先度の帯の値() -> None:
    assert [(p.name, int(p)) for p in Priority] == [
        ("IDLE", 0),
        ("BACKGROUND", 10),
        ("REMOTE", 30),
        ("INTERACTIVE", 50),
        ("SCHEDULED", 80),
    ]
    assert Priority.SCHEDULED + 1 == 81


def test_絵の文字のコードは0から7() -> None:
    assert [(g.name, int(g)) for g in Glyph] == [
        ("EYE", 0),
        ("EYE_HAPPY", 1),
        ("EYE_CLOSED", 2),
        ("SMILE", 3),
        ("MOUTH_O", 4),
        ("STAR", 5),
        ("HEART", 6),
        ("ZZZ", 7),
    ]


def test_顔の種類() -> None:
    assert [f.value for f in Face] == ["normal", "call", "happy", "sleep"]


def test_ボタンの押下は変えられない値() -> None:
    press = ButtonPress(at=1.5, source="usr")
    assert press == ButtonPress(1.5, "usr")
    with pytest.raises(dataclasses.FrozenInstanceError):
        press.at = 2.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "exc", [ResourceBusy, LeaseRevoked, DeviceUnavailable, MotionAborted, SpeechFailed]
)
def test_例外は_Exception_の子(exc: type[Exception]) -> None:
    assert issubclass(exc, Exception)
    assert not issubclass(exc, (KeyboardInterrupt, SystemExit))


def test_使えないデバイスは名前と理由を持つ() -> None:
    u = Unavailable(device="display", reason="I2C の応答がない")
    assert (u.device, u.reason) == ("display", "I2C の応答がない")
    with pytest.raises(dataclasses.FrozenInstanceError):
        u.reason = "x"  # type: ignore[misc]


def test_声の設定の既定値() -> None:
    s = VoiceSettings()
    assert s.open_jtalk == "open_jtalk"
    assert s.dictionary == Path("/var/lib/mecab/dic/open-jtalk/naist-jdic")
    assert s.htsvoice == Path("~/.local/share/osekkai/voices/tohoku-f01-happy.htsvoice")
    assert s.pitch == 3.0
    assert s.speed == 1.05
    assert s.sink == ""


@pytest.mark.parametrize(
    ("field", "value", "ok"),
    [
        ("pitch", -24, True),
        ("pitch", 24, True),
        ("pitch", -24.1, False),
        ("pitch", 24.1, False),
        ("speed", 0.5, True),
        ("speed", 2.0, True),
        ("speed", 0.49, False),
        ("speed", 2.01, False),
    ],
)
def test_声の設定の範囲(field: str, value: float, ok: bool) -> None:
    if ok:
        assert getattr(VoiceSettings.model_validate({field: value}), field) == value
    else:
        with pytest.raises(ValidationError):
            VoiceSettings.model_validate({field: value})


# --- ポートを満たす最小の実装（mypy で Protocol に合うことを確かめる） ---


class _Display:
    def write_line(self, row: int, codes: Sequence[int]) -> None: ...
    def set_backlight(self, on: bool) -> None: ...
    def clear(self) -> None: ...


class _Led:
    def set(self, on: bool) -> None: ...


class _Buttons:
    def start(self, on_falling: Callable[[str, float], None]) -> None: ...
    def close(self) -> None: ...


class _Servos:
    def set_angle(self, channel: Literal["pan", "tilt", "steering"], degrees: float) -> None: ...


class _Motors:
    def set_speeds(self, left: int, right: int) -> None: ...
    def stop_now(self) -> None: ...


class _Synth:
    def synthesize(self, line: str, settings: VoiceSettings, out: Path) -> None: ...


class _Player:
    def play(self, wav: Path, sink: str) -> None: ...


class _TimeSync:
    def is_synchronized(self) -> bool:
        return True


class _Hat:
    def reset(self) -> None: ...


def test_デバイス一式はポートか使えないデバイスを持つ() -> None:
    display: DisplayPort = _Display()
    led: LedPort = _Led()
    buttons: ButtonPort = _Buttons()
    servos: ServoPort = _Servos()
    motors: MotorPort = _Motors()
    synth: SpeechSynthPort = _Synth()
    player: AudioPlayerPort = _Player()
    timesync: TimeSyncPort = _TimeSync()
    hat: HatPort = _Hat()
    devices = Devices(
        display=Unavailable("display", "LCD がない"),
        led=led,
        buttons=buttons,
        servos=servos,
        motors=motors,
        synth=synth,
        player=player,
        timesync=timesync,
        hat=hat,
    )
    assert isinstance(devices.display, Unavailable)
    devices.display = display
    assert not isinstance(devices.display, Unavailable)
    assert devices.timesync.is_synchronized()
