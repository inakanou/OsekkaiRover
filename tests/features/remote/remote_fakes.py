"""ラジコン（remote）のテストで共通に使う偽物（15.1）。

`from remote_fakes import ...` で読む（tests/features/remote が import の道にある）。

- 偽の時計は基盤の偽物（`FakeClock`）を使う。`advance()`・`sleep()` で時刻が進む
- 偽の機能の文脈（`FakeContext`）は、偽のリースを貸す。リースの口は基盤の口と同じ形で、
  呼ばれた操作を文脈の `calls` に順に覚える。借りられないこと（`busy`）と、
  中断の知らせ（`FakeLease.preempt`）を起こせる
- 偽の声は、話している途中で止めておける（`FakeContext.speech`）
- 台本のセンサー・偽の映像の元・偽の USB カメラは、口の型（ports）を満たす
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Final, TypeVar, cast

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import LeaseState
from osekkai.core.feature import FeatureContext
from osekkai.core.types import ButtonPress, Face, LeaseRevoked, Priority, Resource, ResourceBusy
from osekkai.features.remote.ports import SensorReading

__all__ = [
    "REMOTE_RESOURCES",
    "FakeClock",
    "FakeContext",
    "FakeFrameSource",
    "FakeLease",
    "FakeSpeech",
    "FakeUsbCamera",
    "ScriptedSensors",
]

E = TypeVar("E")

# ラジコンが申告する資源（機能の申告と同じ）
REMOTE_RESOURCES: Final = frozenset(
    {Resource.DRIVE, Resource.NECK, Resource.VOICE, Resource.DISPLAY}
)
CLEAR_READING: Final = SensorReading(distance_cm=100.0, floor=(1000, 1000, 1000))
WAIT_S: Final = 5.0  # 止めておいた発話を待つ上限（実際の秒）
MSG_SPEECH_STUCK = "偽の声が放されませんでした"

Call = tuple[object, ...]


class _Recorder:
    """呼ばれた操作を順に覚える。補助のスレッドからも足される。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: list[Call] = []

    def record(self, *call: object) -> None:
        with self._lock:
            self._calls.append(call)

    def snapshot(self) -> list[Call]:
        with self._lock:
            return list(self._calls)


class FakeSpeech:
    """偽の声の状態。`hold()` の後の発話は、`release()` まで話している途中のまま止まる。

    `error` に例外を入れると、話したところでその例外を投げる（1 回だけ）。
    """

    def __init__(self) -> None:
        self.speaking = threading.Event()  # 話している途中
        self.error: Exception | None = None
        self._open = threading.Event()
        self._open.set()

    def hold(self) -> None:
        self._open.clear()

    def release(self) -> None:
        self._open.set()

    def _speak(self) -> None:
        self.speaking.set()
        try:
            if not self._open.wait(WAIT_S):
                raise AssertionError(MSG_SPEECH_STUCK)
            error, self.error = self.error, None
            if error is not None:
                raise error
        finally:
            self.speaking.clear()


class FakeLease:
    """偽のリース。基盤の `Lease` と同じ形で、同じ決まりで断る。

    - 返した・取り上げた後は、どの口も `LeaseRevoked` を投げる
    - 中断の知らせの後は、走る・段のある首の動き（うなずき・見回し・ダンス）を断る。
      止める・首を向ける・話す・LCD は、終わるまで使える
    - `preempt()` は中断の知らせを 1 回だけ出す。受け手はこのスレッドで直ちに呼ぶ
    """

    def __init__(
        self,
        ctx: FakeContext,
        owner: str,
        priority: int,
        resources: frozenset[Resource],
        on_preempt: Callable[[], None] | None,
    ) -> None:
        self._ctx = ctx
        self.owner = owner
        self.priority = priority
        self.resources = resources
        self.on_preempt = on_preempt
        self.preempt_requested = threading.Event()
        self._state = LeaseState.ACTIVE

    @property
    def state(self) -> LeaseState:
        return self._state

    @property
    def active(self) -> bool:
        return self._state in (LeaseState.ACTIVE, LeaseState.PREEMPT_REQUESTED)

    @property
    def voice(self) -> FakeVoice:
        self._require(Resource.VOICE)
        return FakeVoice(self)

    @property
    def display(self) -> FakeDisplay:
        self._require(Resource.DISPLAY)
        return FakeDisplay(self)

    @property
    def neck(self) -> FakeNeck:
        self._require(Resource.NECK)
        return FakeNeck(self)

    @property
    def drive(self) -> FakeDrive:
        self._require(Resource.DRIVE)
        return FakeDrive(self)

    def release(self) -> None:
        """返す。何度呼んでもよい。"""
        self._finish(LeaseState.RELEASED, "release")

    def __enter__(self) -> FakeLease:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    # --- テストから起こす口 ---

    def revoke(self) -> None:
        """取り上げる（基盤が猶予の後に取り上げたのと同じ）。"""
        self._finish(LeaseState.REVOKED, "revoke")

    def preempt(self) -> None:
        """中断を知らせる。知らせるのは 1 回だけ。"""
        if self._state is not LeaseState.ACTIVE:
            return
        self._state = LeaseState.PREEMPT_REQUESTED
        self.preempt_requested.set()
        self.record("preempt")
        if self.on_preempt is not None:
            self.on_preempt()

    # --- 内側 ---

    def record(self, *call: object) -> None:
        self._ctx._recorder.record(*call)

    def check(self) -> None:
        if not self.active:
            raise LeaseRevoked(self.owner)

    def check_may_move(self) -> None:
        if not self.may_move():
            raise LeaseRevoked(self.owner)

    def may_move(self) -> bool:
        return self.active and not self.preempt_requested.is_set()

    def _require(self, resource: Resource) -> None:
        if resource not in self.resources:
            raise ValueError(f"resource not leased: {resource.value}")

    def _finish(self, state: LeaseState, name: str) -> None:
        if not self.active:
            return
        self._state = state
        self.record(name)


class FakeVoice:
    """偽の声の口（`VoiceHandle` と同じ形）。"""

    def __init__(self, lease: FakeLease) -> None:
        self._lease = lease

    def say(self, text: str, *, keep: bool = True) -> None:
        lease = self._lease
        lease.check()
        lease.record("say", text, keep)
        lease._ctx.speech._speak()
        lease.record("said", text)


class FakeDisplay:
    """偽の LCD の口（`DisplayHandle` と同じ形）。"""

    def __init__(self, lease: FakeLease) -> None:
        self._lease = lease

    def show(self, line1: str, line2: str = "") -> None:
        self._lease.check()
        self._lease.record("show", line1, line2)

    def set_backlight(self, on: bool) -> None:
        self._lease.check()
        self._lease.record("backlight", on)


class FakeDrive:
    """偽の走行の口（`DriveHandle` と同じ形）。中断の知らせの後は走るを断る。"""

    def __init__(self, lease: FakeLease) -> None:
        self._lease = lease

    def drive(self, steering: float, speed: int) -> None:
        self._lease.check_may_move()
        self._lease.record("drive", steering, speed)

    def stop(self) -> None:
        self._lease.check()
        self._lease.record("stop")


class FakeNeck:
    """偽の首の口（`NeckHandle` と同じ形）。段のある動きは中断の知らせの後に断る。"""

    def __init__(self, lease: FakeLease) -> None:
        self._lease = lease

    def look(self, pan: float, tilt: float) -> None:
        self._lease.check()
        self._lease.record("look", pan, tilt)

    def home(self) -> None:
        self._lease.check()
        self._lease.record("home")

    def sleepy(self) -> None:
        self._lease.check()
        self._lease.record("sleepy")

    def nod(self, times: int = 2) -> None:
        self._lease.check_may_move()
        self._lease.record("nod", times)

    def look_around(self) -> None:
        self._lease.check_may_move()
        self._lease.record("look_around")

    def dance(self, drive: FakeDrive | None = None) -> None:
        self._lease.check_may_move()
        self._lease.record("dance", drive is not None)


class FakeContext:
    """偽の機能の文脈。偽のリースを貸し、呼ばれた操作を `calls` に順に覚える。

    - `busy` に入れた資源を 1 つでも求めると `ResourceBusy` を投げる
    - 申告にない資源と空の資源は、基盤と同じく `ValueError` で断る
    - 型が `FeatureContext` の引数に渡すときは `as_context()` を使う
    """

    def __init__(
        self,
        clock: FakeClock | None = None,
        *,
        name: str = "remote",
        resources: frozenset[Resource] = REMOTE_RESOURCES,
        priority: int = Priority.REMOTE,
        cache_dir: Path | None = None,
    ) -> None:
        self.name = name
        self.clock = clock if clock is not None else FakeClock()
        self.log = logging.getLogger(f"osekkai.features.{name}")
        self.cache_dir = cache_dir if cache_dir is not None else Path("cache")
        self.declared = resources
        self.priority = priority
        self.busy: set[Resource] = set()
        self.leases: list[FakeLease] = []
        self.speech = FakeSpeech()
        self.quiet = False
        self.idle_info: str | None = None
        self.idle_face: tuple[Face, float] | None = None
        self._recorder = _Recorder()
        self._subscribers: list[tuple[type, Callable[[object], None]]] = []
        self._button_callbacks: list[Callable[[ButtonPress], None]] = []

    @property
    def calls(self) -> list[Call]:
        """呼ばれた操作の並び（借りる・返す・リースの口）。"""
        return self._recorder.snapshot()

    def as_context(self) -> FeatureContext:
        return cast(FeatureContext, self)

    # --- FeatureContext の口 ---

    def acquire(
        self,
        resources: Iterable[Resource],
        *,
        wait: float = 0.0,
        on_preempt: Callable[[], None] | None = None,
    ) -> FakeLease:
        wanted = frozenset(resources)
        if not wanted:
            raise ValueError("resources must not be empty")
        if not wanted <= self.declared:
            raise ValueError("申告していない資源です")
        self._recorder.record("acquire", wanted, wait)
        if wanted & self.busy:
            raise ResourceBusy(", ".join(sorted(r.value for r in wanted)))
        lease = FakeLease(self, self.name, self.priority, wanted, on_preempt)
        self.leases.append(lease)
        return lease

    def want_button_presses(self, callback: Callable[[ButtonPress], None]) -> Callable[[], None]:
        self._button_callbacks.append(callback)
        return lambda: self._button_callbacks.remove(callback)

    def set_idle_info(self, text: str | None) -> None:
        self.idle_info = text

    def set_idle_face(self, face: Face, gaze: float = 0.0) -> None:
        self.idle_face = (face, gaze)

    def request_quiet(self) -> None:
        self.quiet = True

    def end_quiet(self) -> None:
        self.quiet = False

    @property
    def is_quiet(self) -> bool:
        return self.quiet

    @property
    def was_quiet_before_restart(self) -> bool:
        return False

    def subscribe(self, event_type: type[E], callback: Callable[[E], None]) -> None:
        self._subscribers.append((event_type, cast(Callable[[object], None], callback)))

    # --- テストから起こす口 ---

    def publish(self, event: object) -> None:
        """知らせを、その型を受ける受け手に届ける。"""
        for event_type, callback in list(self._subscribers):
            if isinstance(event, event_type):
                callback(event)

    def preempt_latest(self) -> None:
        """最後に貸したリースに、中断を知らせる。"""
        self.leases[-1].preempt()


class ScriptedSensors:
    """台本のセンサー（SensorPort）。台本を順に返し、尽きたら最後の読みを返し続ける。

    台本に例外を入れると、その読みで投げる。台本が空なら、いつも安全な読みを返す。
    """

    def __init__(self, script: Sequence[SensorReading | Exception] = ()) -> None:
        self._lock = threading.Lock()
        self._script: list[SensorReading | Exception] = list(script) or [CLEAR_READING]
        self.reads = 0
        self.closed = False

    def set(self, reading: SensorReading | Exception) -> None:
        """これからの読みを 1 つに変える。"""
        with self._lock:
            self._script = [reading]

    def read(self) -> SensorReading:
        with self._lock:
            self.reads += 1
            item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class _Frames:
    """台本の 1 枚ずつ。尽きたら最後の 1 枚を返し続ける。"""

    def __init__(self, frames: Sequence[bytes | None]) -> None:
        self._lock = threading.Lock()
        self._frames: list[bytes | None] = list(frames) or [None]

    def next(self) -> bytes | None:
        with self._lock:
            return self._frames.pop(0) if len(self._frames) > 1 else self._frames[0]


class FakeFrameSource:
    """偽の映像の元（FrameSource）。開いている間だけ、台本の 1 枚ずつを返す。"""

    def __init__(
        self,
        frames: Sequence[bytes | None] = (),
        *,
        still: bytes | None = None,
        can_open: bool = True,
    ) -> None:
        self._frames = _Frames(frames)
        self._still = still
        self.can_open = can_open
        self.is_open = False
        self.opens = 0
        self.closes = 0

    def open(self) -> bool:
        self.opens += 1
        self.is_open = self.can_open
        return self.is_open

    def read(self) -> bytes | None:
        return self._frames.next() if self.is_open else None

    def still(self) -> bytes | None:
        return self._still if self.is_open else None

    def close(self) -> None:
        self.closes += 1
        self.is_open = False


class FakeUsbCamera:
    """偽の USB カメラ（UsbCameraPort）。開いている間だけ、台本の JPEG を順に返す。"""

    def __init__(self, jpegs: Sequence[bytes | None] = (), *, can_open: bool = True) -> None:
        self._frames = _Frames(jpegs)
        self.can_open = can_open
        self.is_open = False
        self.closed = False

    def open(self) -> bool:
        self.is_open = self.can_open
        self.closed = False
        return self.is_open

    def read_jpeg(self) -> bytes | None:
        return self._frames.next() if self.is_open else None

    def close(self) -> None:
        self.is_open = False
        self.closed = True
