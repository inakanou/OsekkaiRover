"""日課の進み方のテストで使う、台本の入出力と台本の聞き先（TaskHooks）。

- `ScriptedIO` は呼ばれた操作を順に `calls` に残す。押下の待ちは偽の時計を進める
- 押下は、作ったときの単調な時刻からの秒数で決める（`press_at`）。話している間の押下も作れる
- `ScriptedHooks` は、割り込みと予告を偽の時計の時刻で返す
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.types import LeaseRevoked, SpeechFailed
from osekkai.features.routine.schedule import DueNotice, DueTask, Phase

Call = tuple[object, ...]


class ScriptedIO:
    """`RoutineIO` を形で満たす、台本どおりに動く入出力。"""

    def __init__(self, clock: FakeClock, *, speech_s: float = 0.0) -> None:
        self._clock = clock
        self._origin = clock.monotonic()
        self._presses: list[float] = []  # 押下の単調な時刻
        self.speech_s = speech_s  # 1 回話すのにかかる秒数
        self.calls: list[Call] = []
        self.said_at: list[tuple[float, str]] = []  # 話し始めた秒数と言葉
        self.press_while_saying: set[str] = set()  # この言葉を話している間に押す
        self.fail_speech: set[str] = set()  # この言葉は SpeechFailed にする
        self.revoke_after: float | None = None  # この秒数の後の操作は LeaseRevoked にする

    # --- テストから操作する口 ---

    def press_at(self, seconds: float) -> None:
        """作ったときから `seconds` 秒の時刻に、ボタンを押す。"""
        self._presses.append(self._origin + seconds)
        self._presses.sort()

    def elapsed(self) -> float:
        """作ったときからの秒数。"""
        return self._clock.monotonic() - self._origin

    @property
    def pending_presses(self) -> int:
        """まだ使われていない押下の数（これからの押下も含む）。"""
        return len(self._presses)

    def past_presses(self) -> int:
        """今までに押されて、まだ使われていない押下の数。"""
        now = self._clock.monotonic()
        return sum(1 for p in self._presses if p <= now)

    def spoken(self) -> list[str]:
        """話した言葉（ダンスしながら話したものも含む）を順に。"""
        return [str(c[1]) for c in self.calls if c[0] in ("say", "dance_and_say")]

    def names(self) -> list[str]:
        """呼ばれた操作の名前を順に。"""
        return [str(c[0]) for c in self.calls]

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)

    # --- RoutineIO ---

    def show(self, line1: str, line2: str) -> None:
        self._record("show", line1, line2)

    def backlight(self, on: bool) -> None:
        self._record("backlight", on)

    def led(self, on: bool) -> None:
        self._record("led", on)

    def say(self, text: str) -> None:
        self._record("say", text)
        self._speak(text)

    def clear_presses(self) -> None:
        self._record("clear_presses")
        now = self._clock.monotonic()
        self._presses = [p for p in self._presses if p > now]

    def wait_press(self, timeout: float) -> bool:
        self._record("wait_press", timeout)
        now = self._clock.monotonic()
        if self._presses and self._presses[0] <= now + timeout:
            press = self._presses.pop(0)
            self._clock.advance(max(0.0, press - now))
            return True
        self._clock.advance(timeout)
        return False

    def look_around(self) -> None:
        self._record("look_around")

    def nod(self, times: int) -> None:
        self._record("nod", times)

    def sleepy(self) -> None:
        self._record("sleepy")

    def dance_and_say(self, text: str) -> None:
        self._record("dance_and_say", text)
        self._speak(text)

    def _record(self, *call: object) -> None:
        if self.revoke_after is not None and self.elapsed() >= self.revoke_after:
            raise LeaseRevoked()
        self.calls.append(call)

    def _speak(self, text: str) -> None:
        self.said_at.append((self.elapsed(), text))
        if text in self.press_while_saying:
            self.press_at(self.elapsed() + self.speech_s / 2)
        self._clock.advance(self.speech_s)
        if text in self.fail_speech:
            raise SpeechFailed()


@dataclass(frozen=True)
class ScriptedCut:
    """`after_s` 秒の後に、`phases` の段階で割り込む。"""

    after_s: float
    due: DueTask
    phases: frozenset[Phase]


class ScriptedHooks:
    """`TaskHooks` を形で満たす、偽の時計の時刻で答える聞き先。"""

    def __init__(
        self,
        clock: FakeClock,
        *,
        stars: int = 0,
        cuts: Iterable[ScriptedCut] = (),
        notices: Iterable[tuple[float, DueNotice]] = (),
    ) -> None:
        self._clock = clock
        self._origin = clock.monotonic()
        self._cuts = list(cuts)
        self._notices = list(notices)
        self.stars = stars
        self.phases: list[Phase] = []  # cut_in で聞かれた段階
        self.said: list[DueNotice] = []
        self.stop_after: float | None = None  # この秒数の後の区切りで止める知らせを返す

    def should_stop(self) -> bool:
        elapsed = self._clock.monotonic() - self._origin
        return self.stop_after is not None and elapsed >= self.stop_after

    def cut_in(self, phase: Phase) -> DueTask | None:
        self.phases.append(phase)
        elapsed = self._clock.monotonic() - self._origin
        for cut in self._cuts:
            if elapsed >= cut.after_s and phase in cut.phases:
                return cut.due
        return None

    def due_notice(self) -> DueNotice | None:
        elapsed = self._clock.monotonic() - self._origin
        for at, notice in self._notices:
            if elapsed >= at and notice not in self.said:
                return notice
        return None

    def notice_said(self, notice: DueNotice) -> None:
        self.said.append(notice)

    def star_count_after_done(self) -> int:
        return self.stars
