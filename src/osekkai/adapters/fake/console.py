"""ハードなしの実行（`--fake-hardware`）のデバイス。

1.4 の偽物を受け継ぎ、状態が変わるたびに端末へ 1 行ずつ出す。話す文章は `[声] ...` と
して出す（開発者の画面への出力で、ログではない）。標準入力の Enter キーは USR ボタンの
押下として知らせる。
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TextIO

from osekkai.adapters.fake import FakeDeviceSet
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
from osekkai.core.ports import Devices
from osekkai.core.types import Glyph

if TYPE_CHECKING:
    from osekkai.core.config import VoiceSettings

__all__ = [
    "ConsoleButtons",
    "ConsoleDisplay",
    "ConsoleLed",
    "ConsoleMotors",
    "ConsoleOutput",
    "ConsoleServos",
    "ConsoleSynth",
    "build_console_devices",
    "build_console_set",
    "codes_to_text",
]

logger = logging.getLogger(__name__)

_WIDTH = 16
_GLYPHS: dict[int, str] = {
    Glyph.EYE: "●",
    Glyph.EYE_HAPPY: "^",
    Glyph.EYE_CLOSED: "-",
    Glyph.SMILE: "◡",
    Glyph.MOUTH_O: "○",
    Glyph.STAR: "★",
    Glyph.HEART: "♥",
    Glyph.ZZZ: "z",
}


def codes_to_text(codes: Sequence[int]) -> str:
    """LCD のコードを、端末で読める文字に戻す。"""
    chars: list[str] = []
    for code in codes:
        if code in _GLYPHS:
            chars.append(_GLYPHS[code])
        elif code == 0x5C:  # A00 の ROM では「¥」
            chars.append("¥")
        elif 0x20 <= code <= 0x7D:
            chars.append(chr(code))
        elif 0xA1 <= code <= 0xDF:  # 半角カタカナ
            chars.append(chr(0xFF61 + code - 0xA1))
        else:
            chars.append("?")
    return "".join(chars)


class ConsoleOutput:
    """端末への書き出し。スレッドとシグナルの受け手から呼んでよい。"""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._lock = threading.RLock()

    def line(self, text: str) -> None:
        with self._lock:
            try:
                self._stream.write(text + "\n")
                self._stream.flush()
            except (OSError, ValueError):
                pass  # 端末が閉じていても、ロボットの処理は止めない


def _fmt_angle(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


class ConsoleDisplay(FakeDisplay):
    def __init__(self, clock: MonotonicSource | None, out: ConsoleOutput) -> None:
        super().__init__(clock)
        self._out = out
        self._shown = self._render()

    def _render(self) -> str:
        rows = [codes_to_text(r).ljust(_WIDTH) for r in self.rows]
        return f"[LCD] |{rows[0]}|{rows[1]}|"

    def _show_if_changed(self) -> None:
        text = self._render()
        if text != self._shown:
            self._shown = text
            self._out.line(text)

    def write_line(self, row: int, codes: Sequence[int]) -> None:
        super().write_line(row, codes)
        self._show_if_changed()

    def clear(self) -> None:
        super().clear()
        self._show_if_changed()

    def set_backlight(self, on: bool) -> None:
        before = self.backlight
        super().set_backlight(on)
        if on != before:
            self._out.line(f"[LCD] バックライト: {'点灯' if on else '消灯'}")


class ConsoleLed(FakeLed):
    def __init__(self, clock: MonotonicSource | None, out: ConsoleOutput) -> None:
        super().__init__(clock)
        self._out = out

    def set(self, on: bool) -> None:
        before = self.on
        super().set(on)
        if on != before:
            self._out.line(f"[LED] {'点灯' if on else '消灯'}")


class ConsoleServos(FakeServos):
    def __init__(self, clock: MonotonicSource | None, out: ConsoleOutput) -> None:
        super().__init__(clock)
        self._out = out
        self._shown: dict[str, str] = {}

    def set_angle(self, channel: Literal["pan", "tilt", "steering"], degrees: float) -> None:
        super().set_angle(channel, degrees)
        if channel == "steering":
            text = f"[ステアリング] {_fmt_angle(degrees)}"
            key = "steering"
        else:
            pan = _fmt_angle(self.angles.get("pan"))
            tilt = _fmt_angle(self.angles.get("tilt"))
            text = f"[首] pan={pan} tilt={tilt}"
            key = "neck"
        if self._shown.get(key) != text:
            self._shown[key] = text
            self._out.line(text)


class ConsoleMotors(FakeMotors):
    def __init__(self, clock: MonotonicSource | None, out: ConsoleOutput) -> None:
        super().__init__(clock)
        self._out = out

    def _show_if_changed(self, before: tuple[int, int]) -> None:
        if self.speeds != before:
            left, right = self.speeds
            self._out.line(f"[モーター] 左={left} 右={right}")

    def set_speeds(self, left: int, right: int) -> None:
        before = self.speeds
        super().set_speeds(left, right)
        self._show_if_changed(before)

    def stop_now(self) -> None:
        before = self.speeds
        super().stop_now()
        self._show_if_changed(before)


class ConsoleSynth(FakeSynth):
    """合成に成功した文章を `[声]` として出す（WAV も書くので声の流れはそのまま動く）。"""

    def __init__(self, clock: MonotonicSource | None, out: ConsoleOutput) -> None:
        super().__init__(clock)
        self._out = out

    def synthesize(self, line: str, settings: VoiceSettings, out: Path) -> None:
        super().synthesize(line, settings, out)
        self._out.line(f"[声] {line}")


class ConsoleButtons(FakeButtons):
    """入力の 1 行（Enter キー）を USR ボタンの押下として知らせる。

    読み手は daemon のスレッドで、`start()` で始まる。入力の終わり（EOF）で静かに終わる。
    `close()` の後に読んだ行は知らせない。
    """

    def __init__(self, clock: MonotonicSource | None = None, *, inp: TextIO | None = None) -> None:
        super().__init__(clock)
        self._inp = inp if inp is not None else sys.stdin
        self._reader: threading.Thread | None = None

    def start(self, on_falling: Callable[[str, float], None]) -> None:
        super().start(on_falling)
        if self._reader is None:
            self._reader = threading.Thread(
                target=self._read_loop, name="console-buttons", daemon=True
            )
            self._reader.start()

    def join_reader(self, timeout: float | None = None) -> bool:
        """読み手が終わるのを待つ。終わっていれば True（テスト用）。"""
        reader = self._reader
        if reader is None:
            return True
        reader.join(timeout)
        return not reader.is_alive()

    def _read_loop(self) -> None:
        while True:
            try:
                line = self._inp.readline()
            except (OSError, ValueError):
                return  # 入力が閉じられた
            if not line:
                return  # EOF
            try:
                self.press("usr")
            except Exception as error:
                logger.warning("ボタンの押下を知らせられませんでした: %s", type(error).__name__)
                logger.debug("ボタンの押下の通知の失敗の詳細", exc_info=error)


def build_console_set(
    clock: MonotonicSource | None = None,
    *,
    out: TextIO | None = None,
    inp: TextIO | None = None,
) -> FakeDeviceSet:
    """端末に出す版の、型の付いた偽物の一式を作る。"""
    shared: MonotonicSource = clock if clock is not None else FakeClock()
    console = ConsoleOutput(out)
    return FakeDeviceSet(
        display=ConsoleDisplay(shared, console),
        led=ConsoleLed(shared, console),
        buttons=ConsoleButtons(shared, inp=inp),
        servos=ConsoleServos(shared, console),
        motors=ConsoleMotors(shared, console),
        synth=ConsoleSynth(shared, console),
        player=FakePlayer(shared),
        timesync=FakeTimeSync(shared),
        hat=FakeHat(shared),
    )


def build_console_devices(
    clock: MonotonicSource | None = None,
    *,
    out: TextIO | None = None,
    inp: TextIO | None = None,
    fail: Iterable[str] = (),
) -> Devices:
    """ハードなしの実行のデバイス一式。`out` は既定で標準出力、`inp` は標準入力。"""
    return build_console_set(clock, out=out, inp=inp).to_devices(fail)
