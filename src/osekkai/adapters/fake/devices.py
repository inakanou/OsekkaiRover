"""テスト用の、呼ばれた操作を記録する偽のデバイス。

どの偽物も `calls` に `Call(時刻, 操作の名前, 引数)` を呼ばれた順に残す。時刻は
渡した時計の `monotonic()`（既定は偽物ごとの `FakeClock`）。`fail_on` に操作の名前を
入れると、その操作は記録した後に `FakeDeviceError` を投げる。
"""

from __future__ import annotations

import threading
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol

from osekkai.adapters.fake.clock import FakeClock

if TYPE_CHECKING:
    from osekkai.core.config import VoiceSettings


class MonotonicSource(Protocol):
    def monotonic(self) -> float: ...


class Call(NamedTuple):
    at: float
    op: str
    args: tuple[object, ...]


class FakeDeviceError(RuntimeError):
    """偽物に起こさせた失敗。"""


class _FakeDevice:
    def __init__(self, clock: MonotonicSource | None = None) -> None:
        self._clock: MonotonicSource = clock if clock is not None else FakeClock()
        # 同じスレッドから入り直しても止まらないように RLock（シグナルの受け手が割り込む形）
        self._lock = threading.RLock()
        self.calls: list[Call] = []
        self.fail_on: set[str] = set()

    def ops(self) -> list[str]:
        with self._lock:
            return [c.op for c in self.calls]

    def _record(self, op: str, *args: object) -> None:
        with self._lock:
            self.calls.append(Call(self._clock.monotonic(), op, args))
        if op in self.fail_on:
            raise FakeDeviceError(f"fake: {op} failed")


class FakeDisplay(_FakeDevice):
    def __init__(self, clock: MonotonicSource | None = None) -> None:
        super().__init__(clock)
        self.rows: list[tuple[int, ...]] = [(), ()]
        self.backlight = True

    def write_line(self, row: int, codes: Sequence[int]) -> None:
        values = tuple(codes)
        self._record("write_line", row, values)
        self.rows[row] = values

    def set_backlight(self, on: bool) -> None:
        self._record("set_backlight", on)
        self.backlight = on

    def clear(self) -> None:
        self._record("clear")
        self.rows = [(), ()]


class FakeLed(_FakeDevice):
    def __init__(self, clock: MonotonicSource | None = None) -> None:
        super().__init__(clock)
        self.on = False

    def set(self, on: bool) -> None:
        self._record("set", on)
        self.on = on


class FakeButtons(_FakeDevice):
    """`press()` で立ち下がりを模擬する。`start()` の前と `close()` の後は知らせない。"""

    def __init__(self, clock: MonotonicSource | None = None) -> None:
        super().__init__(clock)
        self._on_falling: Callable[[str, float], None] | None = None

    def start(self, on_falling: Callable[[str, float], None]) -> None:
        self._record("start")
        self._on_falling = on_falling

    def close(self) -> None:
        self._record("close")
        self._on_falling = None

    def press(self, source: Literal["usr", "extra"] = "usr") -> None:
        self._record("press", source)
        callback = self._on_falling
        if callback is not None:
            callback(source, self._clock.monotonic())


class FakeServos(_FakeDevice):
    def __init__(self, clock: MonotonicSource | None = None) -> None:
        super().__init__(clock)
        self.angles: dict[str, float] = {}

    def set_angle(self, channel: Literal["pan", "tilt", "steering"], degrees: float) -> None:
        self._record("set_angle", channel, degrees)
        self.angles[channel] = degrees


class FakeMotors(_FakeDevice):
    def __init__(self, clock: MonotonicSource | None = None) -> None:
        super().__init__(clock)
        self.speeds: tuple[int, int] = (0, 0)

    def set_speeds(self, left: int, right: int) -> None:
        self._record("set_speeds", left, right)
        self.speeds = (left, right)

    def stop_now(self) -> None:
        self._record("stop_now")
        self.speeds = (0, 0)


class FakeSynth(_FakeDevice):
    """`out` に短い無音の WAV を書く。`fail=True` なら合成に失敗する。"""

    def __init__(self, clock: MonotonicSource | None = None, *, fail: bool = False) -> None:
        super().__init__(clock)
        if fail:
            self.fail_on.add("synthesize")
        self.lines: list[str] = []

    def synthesize(self, line: str, settings: VoiceSettings, out: Path) -> None:
        self._record("synthesize", line, out)
        self.lines.append(line)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 160)  # 10 ms


class FakePlayer(_FakeDevice):
    """再生したファイルと再生先を `played` に残す。`fail=True` なら再生に失敗する。"""

    def __init__(self, clock: MonotonicSource | None = None, *, fail: bool = False) -> None:
        super().__init__(clock)
        if fail:
            self.fail_on.add("play")
        self.played: list[tuple[Path, str]] = []

    def play(self, wav: Path, sink: str) -> None:
        self._record("play", wav, sink)
        self.played.append((wav, sink))


class FakeTimeSync(_FakeDevice):
    def __init__(self, clock: MonotonicSource | None = None, *, synchronized: bool = True) -> None:
        super().__init__(clock)
        self.synchronized = synchronized

    def is_synchronized(self) -> bool:
        self._record("is_synchronized")
        return self.synchronized

    def set_synchronized(self, synchronized: bool) -> None:
        self.synchronized = synchronized


class FakeHat(_FakeDevice):
    def reset(self) -> None:
        self._record("reset")
