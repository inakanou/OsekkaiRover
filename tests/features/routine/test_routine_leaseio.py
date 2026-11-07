"""借りた資源で入出力を動かす口（LeaseIO）のテスト（4.1、4.9、4.10）。"""

import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.adapters.fake.devices import FakePlayer, FakeServos
from osekkai.core.arbiter import Arbiter, Lease
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.config import MotionSettings, VoiceSettings
from osekkai.core.display import DisplayService, LedService
from osekkai.core.lcd_text import center, to_lcd_codes
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.ports import ServoPort, Unavailable
from osekkai.core.types import Face, LeaseRevoked, Priority, Resource
from osekkai.core.voice import VoiceService
from osekkai.features.routine.runner import LeaseIO, RoutineIO
from osekkai.features.routine.texts import lcd_done

OWNER = "routine"
ALL = list(Resource)
WAIT_S = 2.0  # 実際の時間で待つ上限（テストが止まらないように）


@dataclass
class Rig:
    arbiter: Arbiter
    router: ButtonRouter


def _rig(
    fakes: FakeDeviceSet,
    clock: FakeClock,
    *,
    allow_drive: bool = False,
    player: FakePlayer | None = None,
    servos: ServoPort | Unavailable | None = None,
) -> Rig:
    cache = CacheManager(Paths().cache_dir, max_bytes=10 * 1024 * 1024, clock=clock)
    voice = VoiceService(fakes.synth, player or fakes.player, cache, VoiceSettings(sink="speaker"))
    motion = MotionService(
        servos if servos is not None else fakes.servos,
        fakes.motors,
        MotionSettings(allow_drive_in_dance=allow_drive),
        clock,
    )
    router = ButtonRouter(fakes.buttons, clock)
    router.start()
    arbiter = Arbiter(
        clock,
        motion,
        voice=voice,
        display=DisplayService(fakes.display),
        led=LedService(fakes.led),
        buttons=router,
    )
    return Rig(arbiter, router)


def _lease(rig: Rig, resources: list[Resource] = ALL) -> Lease:
    return rig.arbiter.acquire(OWNER, Priority.SCHEDULED, resources)


@pytest.fixture
def rig(xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock) -> Iterator[Rig]:
    r = _rig(fakes, clock)
    yield r
    r.router.close()


def _row(text: str) -> tuple[int, ...]:
    codes = to_lcd_codes(text)[:16]
    return tuple(codes) + (0x20,) * (16 - len(codes))


def _pans(servos: FakeServos) -> list[object]:
    return [c.args[1] for c in servos.calls if c.op == "set_angle" and c.args[0] == "pan"]


def test_LeaseIOは入出力の型を満たす(rig: Rig) -> None:
    io: RoutineIO = LeaseIO(_lease(rig))
    assert io is not None


# --- 4.1 表示・LED・声・首が偽のデバイスに届く ---


def test_話した言葉が声の偽物に届く(rig: Rig, fakes: FakeDeviceSet) -> None:
    LeaseIO(_lease(rig)).say("はみがき しようね")
    assert fakes.synth.lines == ["はみがき しようね"]
    assert len(fakes.player.played) == 1


def test_LCDの2行とバックライトとLEDが届く(rig: Rig, fakes: FakeDeviceSet) -> None:
    io = LeaseIO(_lease(rig))
    io.show(lcd_done(3), Face.HAPPY.text())
    # 2 行とも LCD の中央に置く
    assert fakes.display.rows[0] == _row(center(lcd_done(3)))
    assert fakes.display.rows[1] == _row(center(Face.HAPPY.text()))
    assert fakes.display.rows[0][0] == 0x20  # 左に寄せない
    io.backlight(False)
    io.backlight(True)
    lights = [c.args for c in fakes.display.calls if c.op == "set_backlight"]
    assert lights == [(False,), (True,)]
    io.led(True)
    io.led(False)
    assert [c.args for c in fakes.led.calls if c.op == "set"] == [(True,), (False,)]


def test_見回しとうなずきと下を向くが首に届く(rig: Rig, fakes: FakeDeviceSet) -> None:
    io = LeaseIO(_lease(rig))
    io.look_around()
    assert _pans(fakes.servos) == [-30.0, 30.0, 0.0]
    before = len(fakes.servos.calls)
    io.nod(2)
    tilts = [c.args[1] for c in fakes.servos.calls[before:] if c.args[0] == "tilt"]
    assert tilts == [-20.0, 10.0, -20.0, 10.0, 0.0]
    io.sleepy()
    assert fakes.servos.angles["tilt"] == -30.0
    assert fakes.servos.angles["pan"] == 0.0


# --- ボタン ---


def test_押されたら押下の待ちは真を返す(rig: Rig, fakes: FakeDeviceSet, clock: FakeClock) -> None:
    io = LeaseIO(_lease(rig))
    clock.advance(0.2)
    fakes.buttons.press()
    assert io.wait_press(0.05) is True


def test_押されなければ押下の待ちは時間切れで偽を返す(rig: Rig) -> None:
    io = LeaseIO(_lease(rig))
    assert io.wait_press(0.02) is False


def test_押下を捨てるとそれまでの押下は数えない(
    rig: Rig, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    io = LeaseIO(_lease(rig))
    clock.advance(0.2)
    fakes.buttons.press()
    io.clear_presses()
    assert io.wait_press(0.02) is False


# --- 取り上げ ---


def test_取り上げられたリースで話すとLeaseRevoked(rig: Rig, fakes: FakeDeviceSet) -> None:
    io = LeaseIO(_lease(rig))
    rig.arbiter.revoke_all(OWNER)
    with pytest.raises(LeaseRevoked):
        io.say("こんにちは")
    with pytest.raises(LeaseRevoked):
        io.dance_and_say("できたね")
    assert fakes.synth.lines == []


# --- 4.9 ダンスしながら話す ---


class _SpeakingPlayer(FakePlayer):
    """話し始めたら知らせ、ダンスが始まるまで話し終えない。"""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.speaking = threading.Event()
        self.dancing = threading.Event()
        self.saw_dance = False

    def play(self, wav: Path, sink: str) -> None:
        self.speaking.set()
        self.saw_dance = self.dancing.wait(WAIT_S)
        super().play(wav, sink)


class _DancingServos(FakeServos):
    """ダンスの最初の段階で知らせ、話し始めるまで次へ進まない。"""

    def __init__(self, clock: FakeClock, player: _SpeakingPlayer) -> None:
        super().__init__(clock)
        self._player = player
        self.saw_speech = False

    def set_angle(self, channel: Literal["pan", "tilt", "steering"], degrees: float) -> None:
        super().set_angle(channel, degrees)
        if channel == "pan" and degrees == 30.0 and not self._player.dancing.is_set():
            self._player.dancing.set()
            self.saw_speech = self._player.speaking.wait(WAIT_S)


def test_ダンスの間に話す(
    xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    player = _SpeakingPlayer(clock)
    servos = _DancingServos(clock, player)
    rig = _rig(fakes, clock, player=player, servos=servos)
    try:
        LeaseIO(_lease(rig)).dance_and_say("できたね")
    finally:
        rig.router.close()
    assert player.saw_dance  # 話している間にダンスが始まった
    assert servos.saw_speech  # 踊っている間に話し始めた
    assert fakes.synth.lines == ["できたね"]
    # 話し終えたらダンスの終わりを待つ（首は正面に戻っている）
    assert servos.angles["pan"] == 0.0
    assert servos.angles["tilt"] == 0.0


# --- 4.10 タイヤは基盤の許可しだい ---


@pytest.mark.parametrize("allow", [False, True])
def test_タイヤは許可があるときだけ回る(
    xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock, allow: bool
) -> None:
    rig = _rig(fakes, clock, allow_drive=allow)
    try:
        LeaseIO(_lease(rig)).dance_and_say("できたね")
    finally:
        rig.router.close()
    moved = [c.args for c in fakes.motors.calls if c.op == "set_speeds" and c.args != (0, 0)]
    assert bool(moved) is allow
    assert fakes.motors.speeds == (0, 0)
    # 走行を借りているので、ステアリングも踊る
    steerings = [c.args[1] for c in fakes.servos.calls if c.args[0] == "steering"]
    assert steerings


def test_走行を借りていなければ首だけで踊る(
    xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    rig = _rig(fakes, clock, allow_drive=True)
    try:
        lease = _lease(rig, [r for r in ALL if r is not Resource.DRIVE])
        LeaseIO(lease).dance_and_say("できたね")
    finally:
        rig.router.close()
    assert "set_speeds" not in fakes.motors.ops()
    assert not [c for c in fakes.servos.calls if c.args[0] == "steering"]
    assert _pans(fakes.servos)


def test_ダンスの失敗は型の名前だけ記録して話し続ける(
    xdg_dirs: dict[str, Path],
    fakes: FakeDeviceSet,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _rig(fakes, clock, servos=Unavailable("servos", "missing"))
    caplog.set_level(logging.DEBUG)
    try:
        LeaseIO(_lease(rig)).dance_and_say("できたね")
    finally:
        rig.router.close()
    assert fakes.synth.lines == ["できたね"]
    assert "DeviceUnavailable" in caplog.text
    assert "できたね" not in caplog.text
