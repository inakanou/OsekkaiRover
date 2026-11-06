"""PC とテストで使う、偽の軽い確かめ（device-deployment 6.1、6.5）。

よい・だめを作るときに決める。尋ねたことは記録して、テストが読む。
ハードには触らず、音も鳴らさない。
"""

from __future__ import annotations

import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from osekkai.core.config import VoiceSettings

FAKE_RATE: Final = 16_000
FAKE_FRAMES: Final = 160  # 0.01 秒


def _default_answering() -> set[int]:
    return {0x27, 0x14}  # LCD の既定のアドレスと、HAT のマイコン


@dataclass
class FakeProbes:
    """`osekkai.checks.Probes` の偽物。"""

    answering: set[int] = field(default_factory=_default_answering)  # I2C で応えるアドレス
    busy_pins: set[int] = field(default_factory=set)  # 取られている GPIO の線
    sinks: set[str] = field(default_factory=set)  # 名前で見つかる再生先
    default_sink: bool = True  # 既定の再生先があるか
    synchronized: bool = True
    synth_ok: bool = True  # False なら合成で例外
    synth_writes_wav: bool = True  # False なら何も書かずに終わる
    i2c_queries: list[int] = field(default_factory=list)
    gpio_queries: list[int] = field(default_factory=list)
    sink_queries: list[str] = field(default_factory=list)
    synthesized: list[tuple[VoiceSettings, Path]] = field(default_factory=list)
    played: int = 0  # 鳴らした回数（いつも 0。鳴らす口はない）

    def i2c_responds(self, address: int) -> bool:
        self.i2c_queries.append(address)
        return address in self.answering

    def gpio_input_free(self, pin: int) -> bool:
        self.gpio_queries.append(pin)
        return pin not in self.busy_pins

    def audio_sink_exists(self, sink: str) -> bool:
        self.sink_queries.append(sink)
        if not sink:
            return self.default_sink
        return sink in self.sinks

    def synthesize(self, settings: VoiceSettings, out: Path) -> None:
        self.synthesized.append((settings, out))
        if not self.synth_ok:
            raise RuntimeError("fake: synthesis failed")
        if not self.synth_writes_wav:
            return
        # 小さな音のある短い WAV（16 ビット、1 チャンネル）
        with wave.open(str(out), "wb") as dst:
            dst.setnchannels(1)
            dst.setsampwidth(2)
            dst.setframerate(FAKE_RATE)
            dst.writeframes(b"\x10\x00" * FAKE_FRAMES)

    def time_synchronized(self) -> bool:
        return self.synchronized
