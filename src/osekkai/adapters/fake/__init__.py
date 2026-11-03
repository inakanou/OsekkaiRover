"""ハードのない環境で使う偽のデバイス一式。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, fields
from typing import TypeVar

from osekkai.adapters.fake.clock import FakeClock
from osekkai.adapters.fake.devices import (
    FakeButtons,
    FakeDisplay,
    FakeHat,
    FakeLed,
    FakeMotors,
    FakePlayer,
    FakeServos,
    FakeSynth,
    FakeTimeSync,
    MonotonicSource,
)
from osekkai.core.ports import Devices, Unavailable

__all__ = ["FakeClock", "FakeDeviceSet", "build_fake_devices"]

FAIL_REASON = "fake: preparation failed"

_P = TypeVar("_P")


@dataclass
class FakeDeviceSet:
    """型の付いた偽物の一式。テストは記録をここから読む。"""

    display: FakeDisplay
    led: FakeLed
    buttons: FakeButtons
    servos: FakeServos
    motors: FakeMotors
    synth: FakeSynth
    player: FakePlayer
    timesync: FakeTimeSync
    hat: FakeHat

    @classmethod
    def create(cls, clock: MonotonicSource | None = None) -> FakeDeviceSet:
        """すべての偽物を、同じ時計で作る。"""
        shared = clock if clock is not None else FakeClock()
        return cls(
            display=FakeDisplay(shared),
            led=FakeLed(shared),
            buttons=FakeButtons(shared),
            servos=FakeServos(shared),
            motors=FakeMotors(shared),
            synth=FakeSynth(shared),
            player=FakePlayer(shared),
            timesync=FakeTimeSync(shared),
            hat=FakeHat(shared),
        )

    def to_devices(self, fail: Iterable[str] = ()) -> Devices:
        """`Devices` にする。`fail` に挙げた名前のデバイスは準備に失敗したものとする。"""
        failed = set(fail)
        names = {f.name for f in fields(Devices)}
        unknown = failed - names
        if unknown:
            raise ValueError(f"unknown devices: {sorted(unknown)}")
        if "timesync" in failed:
            raise ValueError("timesync cannot be unavailable")

        def pick(name: str, device: _P) -> _P | Unavailable:
            return Unavailable(name, FAIL_REASON) if name in failed else device

        return Devices(
            display=pick("display", self.display),
            led=pick("led", self.led),
            buttons=pick("buttons", self.buttons),
            servos=pick("servos", self.servos),
            motors=pick("motors", self.motors),
            synth=pick("synth", self.synth),
            player=pick("player", self.player),
            timesync=self.timesync,
            hat=pick("hat", self.hat),
        )


def build_fake_devices(
    clock: MonotonicSource | None = None, *, fail: Iterable[str] = ()
) -> Devices:
    """偽物のデバイス一式を組み立てる。`fail` で準備の失敗を模擬する（1.6）。"""
    return FakeDeviceSet.create(clock).to_devices(fail)
