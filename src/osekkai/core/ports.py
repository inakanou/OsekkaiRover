"""デバイスのポート（Protocol）と、デバイス一式の入れ物。

アダプタ（pi、fake）はこのモジュールと `core.types` だけを読み込む。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from osekkai.core.config import VoiceSettings


class DisplayPort(Protocol):
    def write_line(self, row: int, codes: Sequence[int]) -> None: ...  # 16 コード
    def set_backlight(self, on: bool) -> None: ...
    def clear(self) -> None: ...


class LedPort(Protocol):
    def set(self, on: bool) -> None: ...


class ButtonPort(Protocol):
    def start(self, on_falling: Callable[[str, float], None]) -> None: ...  # (source, monotonic)
    def close(self) -> None: ...


class ServoPort(Protocol):
    def set_angle(self, channel: Literal["pan", "tilt", "steering"], degrees: float) -> None: ...


class MotorPort(Protocol):
    def set_speeds(
        self, left: int, right: int
    ) -> None: ...  # -100..100（向きの補正は MotionService）
    def stop_now(self) -> None: ...  # ロックを待たない最速の停止


class SpeechSynthPort(Protocol):
    def synthesize(self, line: str, settings: VoiceSettings, out: Path) -> None: ...


class AudioPlayerPort(Protocol):
    def play(self, wav: Path, sink: str) -> None: ...


class TimeSyncPort(Protocol):
    def is_synchronized(self) -> bool: ...


class HatPort(Protocol):
    def reset(self) -> None: ...


@dataclass(frozen=True)
class Unavailable:
    """準備に失敗して使えないデバイス。"""

    device: str
    reason: str


@dataclass
class Devices:
    """組み立てたデバイス一式。使えないものは `Unavailable` になる。"""

    display: DisplayPort | Unavailable
    led: LedPort | Unavailable
    buttons: ButtonPort | Unavailable
    servos: ServoPort | Unavailable
    motors: MotorPort | Unavailable
    synth: SpeechSynthPort | Unavailable
    player: AudioPlayerPort | Unavailable
    timesync: TimeSyncPort
    hat: HatPort | Unavailable
