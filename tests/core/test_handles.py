"""リースから得る操作の口（Handles）のテスト（1.6、4.4、4.6、4.9）。"""

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.adapters.fake.devices import FakePlayer, FakeServos, FakeSynth, _FakeDevice
from osekkai.core.arbiter import Arbiter, Lease
from osekkai.core.cache import CacheManager
from osekkai.core.config import MotionSettings, VoiceSettings
from osekkai.core.display import DisplayService, LedService
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.ports import Unavailable
from osekkai.core.types import (
    DeviceUnavailable,
    LeaseRevoked,
    Priority,
    Resource,
    SpeechFailed,
)
from osekkai.core.voice import VoiceService

V = Resource.VOICE
D = Resource.DISPLAY
L = Resource.LED
N = Resource.NECK
R = Resource.DRIVE
ALL = [V, D, L, N, R]


@pytest.fixture
def cache(xdg_dirs: dict[str, Path], clock: FakeClock) -> CacheManager:
    return CacheManager(Paths().cache_dir, max_bytes=10 * 1024 * 1024, clock=clock)


@pytest.fixture
def motion(fakes: FakeDeviceSet, clock: FakeClock) -> MotionService:
    return MotionService(fakes.servos, fakes.motors, MotionSettings(), clock)


@pytest.fixture
def voice(fakes: FakeDeviceSet, cache: CacheManager) -> VoiceService:
    return VoiceService(fakes.synth, fakes.player, cache, VoiceSettings(sink="speaker"))


@pytest.fixture
def arbiter(
    clock: FakeClock, motion: MotionService, voice: VoiceService, fakes: FakeDeviceSet
) -> Arbiter:
    return Arbiter(
        clock,
        motion,
        voice=voice,
        display=DisplayService(fakes.display),
        led=LedService(fakes.led),
    )


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "条件が満たされませんでした"
        time.sleep(0.005)


def _operations(lease: Lease) -> dict[str, Callable[[], None]]:
    return {
        "say": lambda: lease.voice.say("こんにちは。"),
        "show": lambda: lease.display.show("a"),
        "backlight": lambda: lease.display.set_backlight(True),
        "led": lambda: lease.led.set(True),
        "look": lambda: lease.neck.look(10, 10),
        "home": lambda: lease.neck.home(),
        "nod": lambda: lease.neck.nod(),
        "look_around": lambda: lease.neck.look_around(),
        "sleepy": lambda: lease.neck.sleepy(),
        "dance": lambda: lease.neck.dance(),
        "drive": lambda: lease.drive.drive(0, 10),
        "stop": lambda: lease.drive.stop(),
    }


# --- 有効なリースでの操作 ---


def test_有効なリースの口で各デバイスを操作できる(arbiter: Arbiter, fakes: FakeDeviceSet) -> None:
    lease = arbiter.acquire("a", Priority.INTERACTIVE, ALL)
    lease.voice.say("こんにちは。")
    lease.display.show("ABC", "DEF")
    lease.display.set_backlight(False)
    lease.led.set(True)
    lease.neck.look(20, 5)
    lease.drive.drive(10, 30)
    assert len(fakes.player.played) == 1
    assert fakes.display.rows[0] is not None
    assert "set_backlight" in fakes.display.ops()
    assert "set" in fakes.led.ops()
    assert fakes.servos.angles["pan"] == 20
    assert fakes.motors.speeds == (30, 30)
    lease.drive.stop()
    assert fakes.motors.speeds == (0, 0)
    lease.neck.nod()
    lease.neck.look_around()
    lease.neck.sleepy()
    lease.neck.dance()
    lease.neck.home()
    assert fakes.servos.angles["pan"] == 0
    assert fakes.servos.angles["tilt"] == 0


def test_首の口の正面はステアリングに触らない(arbiter: Arbiter, fakes: FakeDeviceSet) -> None:
    neck = arbiter.acquire("neck", Priority.INTERACTIVE, [N])
    drive = arbiter.acquire("drive", Priority.INTERACTIVE, [R])
    drive.drive.drive(20, 0)
    neck.neck.home()
    assert fakes.servos.angles["steering"] == 20


def test_首だけの口のダンスはほかの持ち主のステアリングに触らない(
    arbiter: Arbiter, fakes: FakeDeviceSet
) -> None:
    neck = arbiter.acquire("neck", Priority.INTERACTIVE, [N])
    drive = arbiter.acquire("drive", Priority.INTERACTIVE, [R])
    drive.drive.drive(20, 0)
    before = len(fakes.servos.calls)
    neck.neck.dance()
    written = [c.args[0] for c in fakes.servos.calls[before:] if c.op == "set_angle"]
    assert "steering" not in written
    assert {"pan", "tilt"} <= set(written)
    assert fakes.servos.angles["steering"] == 20


def test_走行の口を渡したダンスはステアリングも振る(arbiter: Arbiter, fakes: FakeDeviceSet) -> None:
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [N, R])
    lease.neck.dance(lease.drive)
    steerings = [c.args[1] for c in fakes.servos.calls if c.args[0] == "steering"]
    assert steerings == [-25.0, 25.0, -25.0, 25.0, -25.0, 25.0, 0.0]


# --- 借りていない資源（4.9） ---


@pytest.mark.parametrize("name", ["voice", "display", "led", "neck", "drive"])
def test_借りていない資源の口は求められない(arbiter: Arbiter, name: str) -> None:
    others = [r for r in ALL if r.value != name]
    lease = arbiter.acquire("a", Priority.INTERACTIVE, others)
    with pytest.raises(ValueError):
        getattr(lease, name)


# --- 取り上げ後・返却後の操作（4.4） ---


OPS = [
    "say",
    "show",
    "backlight",
    "led",
    "look",
    "home",
    "nod",
    "look_around",
    "sleepy",
    "dance",
    "drive",
    "stop",
]


@pytest.mark.parametrize("op", OPS)
def test_取り上げた後の操作は取り上げの例外になる(
    arbiter: Arbiter, fakes: FakeDeviceSet, op: str
) -> None:
    lease = arbiter.acquire("a", Priority.INTERACTIVE, ALL)
    operations = _operations(lease)
    arbiter.revoke_all("a")
    before = {name: len(device.calls) for name, device in _devices(fakes).items()}
    with pytest.raises(LeaseRevoked):
        operations[op]()
    after = {name: len(device.calls) for name, device in _devices(fakes).items()}
    assert after == before, "取り上げた後にデバイスを操作しました"


@pytest.mark.parametrize("op", OPS)
def test_返した後の操作は取り上げの例外になる(arbiter: Arbiter, op: str) -> None:
    lease = arbiter.acquire("a", Priority.INTERACTIVE, ALL)
    operations = _operations(lease)
    lease.release()
    with pytest.raises(LeaseRevoked):
        operations[op]()


def test_返した後に求めた口も操作で取り上げの例外になる(arbiter: Arbiter) -> None:
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [D])
    lease.release()
    with pytest.raises(LeaseRevoked):
        lease.display.show("a")


def _devices(fakes: FakeDeviceSet) -> dict[str, _FakeDevice]:
    return {
        "display": fakes.display,
        "led": fakes.led,
        "servos": fakes.servos,
        "motors": fakes.motors,
        "synth": fakes.synth,
        "player": fakes.player,
    }


# --- 中断を知らされた後の走行（4.5 の補い） ---


def _preempt(arbiter: Arbiter, lease: Lease, resources: list[Resource]) -> threading.Thread:
    """高い方の要求を別のスレッドで出し、中断が知らされるまで待つ。"""
    thread = threading.Thread(
        target=lambda: arbiter.acquire("high", Priority.INTERACTIVE, resources).release(),
        daemon=True,
    )
    thread.start()
    _wait_until(lease.preempt_requested.is_set)
    return thread


def test_中断を知らされた後の走行は断られる(arbiter: Arbiter, fakes: FakeDeviceSet) -> None:
    lease = arbiter.acquire("low", Priority.BACKGROUND, [R])
    lease.drive.drive(0, 40)
    thread = _preempt(arbiter, lease, [R])
    assert lease.active
    assert fakes.motors.speeds == (0, 0)
    with pytest.raises(LeaseRevoked):
        lease.drive.drive(0, 40)
    assert fakes.motors.speeds == (0, 0)
    # 止めるのは、中断を知らされた後でもよい
    lease.drive.stop()
    lease.release()
    thread.join(2)
    assert not thread.is_alive()


# --- 段階のある動きの中断 ---


class _HookServos(FakeServos):
    """チルトに初めて書いたときに、合図を呼ぶ。"""

    def __init__(self) -> None:
        super().__init__()
        self.hook: Callable[[], None] | None = None

    def set_angle(self, channel: Literal["pan", "tilt", "steering"], degrees: float) -> None:
        super().set_angle(channel, degrees)
        hook, self.hook = self.hook, None
        if hook is not None:
            hook()


def test_うなずきの途中で中断されると残りを行わない(clock: FakeClock, fakes: FakeDeviceSet) -> None:
    servos = _HookServos()
    motion = MotionService(servos, fakes.motors, MotionSettings(), clock)
    arbiter = Arbiter(clock, motion)
    lease = arbiter.acquire("low", Priority.BACKGROUND, [N])
    threads: list[threading.Thread] = []
    servos.hook = lambda: threads.append(_preempt(arbiter, lease, [N]))

    with pytest.raises(LeaseRevoked):
        lease.neck.nod(times=3)

    # 最初の 1 段（チルト −20）だけで止まり、首を戻していない
    assert [c.args for c in servos.calls] == [("tilt", -20.0)]
    assert lease.active
    lease.release()
    threads[0].join(2)
    assert not threads[0].is_alive()


class _HookClock(FakeClock):
    """初めて眠るときに、合図を呼ぶ（動きの段階の間、ロックの外）。"""

    def __init__(self) -> None:
        super().__init__()
        self.hook: Callable[[], None] | None = None

    def sleep(self, seconds: float) -> None:
        hook, self.hook = self.hook, None
        if hook is not None:
            hook()
        super().sleep(seconds)


def test_見回しの途中で取り上げられると残りを行わない(fakes: FakeDeviceSet) -> None:
    clock = _HookClock()
    motion = MotionService(fakes.servos, fakes.motors, MotionSettings(), clock)
    arbiter = Arbiter(clock, motion)
    lease = arbiter.acquire("low", Priority.BACKGROUND, [N])
    clock.hook = lambda: arbiter.revoke_all("low")

    with pytest.raises(LeaseRevoked):
        lease.neck.look_around()
    pans = [c.args[1] for c in fakes.servos.calls if c.args[0] == "pan"]
    # 1 段目の −30 と、取り上げによる首の正面（4.8）だけ
    assert pans == [-30.0, 0.0]


def test_ダンスは走行の口があるときだけタイヤを回す(clock: FakeClock, fakes: FakeDeviceSet) -> None:
    motion = MotionService(
        fakes.servos, fakes.motors, MotionSettings(allow_drive_in_dance=True), clock
    )
    arbiter = Arbiter(clock, motion)
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [N, R])
    lease.neck.dance()
    assert "set_speeds" not in fakes.motors.ops()
    lease.neck.dance(lease.drive)
    assert "set_speeds" in fakes.motors.ops()


def test_ダンスは無効な走行の口ではタイヤを回さない(clock: FakeClock, fakes: FakeDeviceSet) -> None:
    motion = MotionService(
        fakes.servos, fakes.motors, MotionSettings(allow_drive_in_dance=True), clock
    )
    arbiter = Arbiter(clock, motion)
    neck = arbiter.acquire("a", Priority.INTERACTIVE, [N])
    drive = arbiter.acquire("a", Priority.INTERACTIVE, [R])
    handle = drive.drive
    drive.release()  # 返したときの停止（2.5）で 1 回だけ書く
    before = fakes.motors.ops().count("set_speeds")
    neck.neck.dance(handle)
    assert fakes.motors.ops().count("set_speeds") == before


# --- 使えないデバイス（1.6） ---


def test_使えないデバイスの操作はデバイスが使えないの例外になる(
    clock: FakeClock, cache: CacheManager
) -> None:
    motion = MotionService(
        Unavailable("servos", "missing"), Unavailable("motors", "missing"), MotionSettings(), clock
    )
    voice = VoiceService(
        Unavailable("synth", "missing"), FakePlayer(), cache, VoiceSettings(sink="speaker")
    )
    arbiter = Arbiter(
        clock,
        motion,
        voice=voice,
        display=DisplayService(Unavailable("display", "i2c")),
        led=LedService(Unavailable("led", "gpio")),
    )
    lease = arbiter.acquire("a", Priority.INTERACTIVE, ALL)
    for name, op in _operations(lease).items():
        with pytest.raises(DeviceUnavailable):
            op()
        assert lease.active, name


def test_サービスを渡されていない資源の操作はデバイスが使えないの例外になる(
    arbiter: Arbiter, clock: FakeClock, motion: MotionService
) -> None:
    bare = Arbiter(clock, motion)
    lease = bare.acquire("a", Priority.INTERACTIVE, [V, D, L])
    with pytest.raises(DeviceUnavailable):
        lease.voice.say("あ。")
    with pytest.raises(DeviceUnavailable):
        lease.display.show("a")
    with pytest.raises(DeviceUnavailable):
        lease.led.set(True)


def test_話せなければ声の失敗の例外がそのまま届く(
    clock: FakeClock, motion: MotionService, cache: CacheManager
) -> None:
    voice = VoiceService(FakeSynth(fail=True), FakePlayer(), cache, VoiceSettings(sink="speaker"))
    arbiter = Arbiter(clock, motion, voice=voice)
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [V])
    with pytest.raises(SpeechFailed):
        lease.voice.say("あ。")
    assert lease.active


# --- 声は発話の途中で止めない（4.6） ---


class _BlockingPlayer:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.played = 0

    def play(self, wav: Path, sink: str) -> None:
        self.entered.set()
        self.release.wait(5)
        self.played += 1


def test_取り上げられても話している途中では止めない(
    clock: FakeClock, motion: MotionService, cache: CacheManager
) -> None:
    player = _BlockingPlayer()
    voice = VoiceService(FakeSynth(), player, cache, VoiceSettings(sink="speaker"))
    arbiter = Arbiter(clock, motion, voice=voice)
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [V])
    errors: list[BaseException] = []

    def speak() -> None:
        try:
            lease.voice.say("ながいおはなし。")
        except BaseException as error:  # pragma: no cover - 失敗のときだけ
            errors.append(error)

    thread = threading.Thread(target=speak)
    thread.start()
    assert player.entered.wait(2)
    arbiter.revoke_all("a")
    player.release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert errors == []
    assert player.played == 1
    with pytest.raises(LeaseRevoked):
        lease.voice.say("つぎ。")
    assert player.played == 1


class _WatchedLock:
    """発話のロックを待ち始めたことを知らせる。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.waiters = 0

    def __enter__(self) -> None:
        self.waiters += 1
        self._lock.acquire()

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


def test_ほかの発話を待つ間に取り上げられたら話さない(
    clock: FakeClock, motion: MotionService, cache: CacheManager
) -> None:
    player = _BlockingPlayer()
    synth = FakeSynth()
    voice = VoiceService(synth, player, cache, VoiceSettings(sink="speaker"))
    lock = _WatchedLock()
    voice._lock = lock  # type: ignore[assignment]
    arbiter = Arbiter(clock, motion, voice=voice)

    # C が話している途中で取り上げられ、D が声を借りる
    first = arbiter.acquire("c", Priority.INTERACTIVE, [V])
    speaking = threading.Thread(target=first.voice.say, args=("しーのおはなし。",))
    speaking.start()
    assert player.entered.wait(2)
    arbiter.revoke_all("c")
    second = arbiter.acquire("d", Priority.INTERACTIVE, [V])
    errors: list[BaseException] = []

    def say_d() -> None:
        try:
            second.voice.say("でぃーのおはなし。")
        except BaseException as error:
            errors.append(error)

    waiting = threading.Thread(target=say_d)
    waiting.start()
    _wait_until(lambda: lock.waiters == 2)
    # D がロックを待っている間に取り上げる
    arbiter.revoke_all("d")
    player.release.set()
    speaking.join(2)
    waiting.join(2)
    assert not speaking.is_alive()
    assert not waiting.is_alive()
    assert player.played == 1
    assert "でぃーのおはなし。" not in synth.lines
    assert len(errors) == 1
    assert isinstance(errors[0], LeaseRevoked)
