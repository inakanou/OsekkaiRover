"""だるまさん（daruma）のテストで共通に使う偽物（9.1）。

`from daruma_fakes import ...` で読む（tests/features/daruma が import の道にある）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from osekkai.features.daruma.rules import Rules
from osekkai.features.daruma.texts import CHANTS
from osekkai.features.routine.api import RoutineStatus

MSG_SCRIPT_EMPTY = "偽の遊びの口の回の台本が尽きました"

# 偽の日課の時刻（時差つき）
NOW = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)

_CHANT_SPEECHES = frozenset(chant.speech for chant in CHANTS)


def make_rules(
    *,
    threshold: float = 0.01,
    watch_seconds: float = 2.5,
    max_rounds: int = 10,
    end_before_task_min: int = 10,
) -> Rules:
    """テスト用の決まり（既定は設定の既定と同じ）。"""
    return Rules(
        threshold=threshold,
        watch_seconds=watch_seconds,
        max_rounds=max_rounds,
        end_before_task=timedelta(minutes=end_before_task_min),
    )


def make_status(*, next_task_in: timedelta | None = None, in_task: bool = False) -> RoutineStatus:
    """偽の日課の状況。`next_task_in` は NOW から次のタスクまでの長さ（None はタスクなし）。"""
    return RoutineStatus(
        next_task_at=None if next_task_in is None else NOW + next_task_in,
        in_task=in_task,
        asleep=False,
        updated_at=NOW,
    )


@dataclass(frozen=True)
class RoundScript:
    """1 回分の台本。どの手順の間に押されるかと、見たときの動きの量。

    `motion` に例外を入れると、見るときにその例外を投げる。
    """

    press_while_chanting: bool = False  # 唱え言葉を話している間に押す
    press_in_pause: bool = False  # 唱えた後の間に押す
    press_while_watching: bool = False  # 見ている間に押す
    press_while_resting: bool = False  # 動いたの後の待ちの間に押す
    motion: float | Exception = 0.0


@dataclass(frozen=True)
class Call:
    """偽の遊びの口が受けた 1 回の呼び出し。`result` は返した値。"""

    name: str
    args: tuple[object, ...] = ()
    result: object = None


class FakeGameIO:
    """偽の遊びの口（GameIO）。回ごとの台本で押下と動きの量を返し、呼び出しを順に覚える。

    唱え言葉を話すと次の回の台本に進む。押下は数で持ち、`wait_press` は 1 つ消費し、
    `pressed` は消費しない（基盤のボタンの口と同じ）。
    """

    def __init__(
        self,
        rounds: Sequence[RoundScript] = (),
        *,
        camera: bool = True,
        press_during_intro: bool = False,
        after_say: Callable[[str], None] | None = None,
        say_errors: Mapping[str, Exception] | None = None,
        errors: Mapping[str, Exception] | None = None,
    ) -> None:
        """`after_say` は話し終えた言葉を受ける（中断の旗を立てるのに使う）。

        `say_errors` はその言葉を話すときに投げる例外、`errors` は口の名前ごとに
        最初の呼び出しで投げる例外（呼び出しは覚えてから投げる）。
        """
        self.calls: list[Call] = []
        self._after_say = after_say
        self._say_errors = dict(say_errors or {})
        self._errors = dict(errors or {})
        self._rounds = list(rounds)
        self._camera = camera
        self._press_during_intro = press_during_intro
        self._index = -1  # 今の回（-1 は始めの間）
        self._presses = 0

    # 口

    def show(self, line1: str, line2: str) -> None:
        self.calls.append(Call("show", (line1, line2)))

    def say(self, text: str) -> None:
        if text in _CHANT_SPEECHES:
            self._index += 1
            if self._index >= len(self._rounds):
                raise AssertionError(MSG_SCRIPT_EMPTY)
            if self._script.press_while_chanting:
                self._presses += 1
        self.calls.append(Call("say", (text,)))
        if text in self._say_errors:
            raise self._say_errors.pop(text)
        if self._after_say is not None:
            self._after_say(text)

    def look(self, pan: float, tilt: float) -> None:
        self.calls.append(Call("look", (pan, tilt)))
        self._maybe_fail("look")

    def home(self) -> None:
        self.calls.append(Call("home"))

    def nod(self) -> None:
        self.calls.append(Call("nod"))
        self._maybe_fail("nod")

    def celebrate(self, text: str) -> None:
        self.calls.append(Call("celebrate", (text,)))

    def clear_presses(self) -> None:
        self._presses = 0
        self.calls.append(Call("clear_presses"))

    def pressed(self) -> bool:
        result = self._presses > 0
        self.calls.append(Call("pressed", (), result))
        return result

    def wait_press(self, seconds: float) -> bool:
        if self._presses == 0 and self._index >= 0 and self._script.press_in_pause:
            self._presses += 1
        result = self._presses > 0
        if result:
            self._presses -= 1
        self.calls.append(Call("wait_press", (seconds,), result))
        return result

    def open_camera(self) -> bool:
        self.calls.append(Call("open_camera", (), self._camera))
        self._maybe_fail("open_camera")
        return self._camera

    def motion(self, seconds: float) -> float:
        self.calls.append(Call("motion", (seconds,)))
        script = self._script
        if script.press_while_watching:
            self._presses += 1
        if isinstance(script.motion, Exception):
            raise script.motion
        return script.motion

    def rest(self, seconds: float) -> None:
        self.calls.append(Call("rest", (seconds,)))
        if self._index < 0:
            if self._press_during_intro:
                self._presses += 1
        elif self._script.press_while_resting:
            self._presses += 1

    def _maybe_fail(self, name: str) -> None:
        if name in self._errors:
            raise self._errors.pop(name)

    # 確かめ

    @property
    def names(self) -> list[str]:
        """呼ばれた口の名前の並び。"""
        return [call.name for call in self.calls]

    @property
    def spoken(self) -> list[str]:
        """話した言葉（踊りながらの言葉を含む）の並び。"""
        return [str(call.args[0]) for call in self.calls if call.name in ("say", "celebrate")]

    @property
    def shown(self) -> list[tuple[object, ...]]:
        """LCD に出した 2 行の並び。"""
        return [call.args for call in self.calls if call.name == "show"]

    @property
    def rounds_started(self) -> int:
        """唱え始めた回の数。"""
        return self._index + 1

    @property
    def _script(self) -> RoundScript:
        if not 0 <= self._index < len(self._rounds):
            raise AssertionError(MSG_SCRIPT_EMPTY)
        return self._rounds[self._index]


class FakeLease:
    """偽のリースの口（声・LCD・首・ボタン）。呼ばれた操作を `events` に順に覚える。

    `events` は複数のスレッド（踊りのスレッド）から足される。
    `speak_hook`・`dance_hook` は、話す・踊るの最中に呼ばれる（並びを確かめるのに使う）。
    """

    def __init__(self) -> None:
        self.active = True
        self.events: list[tuple[object, ...]] = []
        self.presses = 0
        self.say_error: Exception | None = None
        self.dance_error: Exception | None = None
        self.speak_hook: Callable[[], None] | None = None
        self.dance_hook: Callable[[], None] | None = None
        self._lock = threading.Lock()
        self.voice = _FakeVoice(self)
        self.display = _FakeDisplay(self)
        self.neck = _FakeNeck(self)
        self.button = _FakeButton(self)

    def record(self, *event: object) -> None:
        with self._lock:
            self.events.append(event)

    @property
    def names(self) -> list[object]:
        """覚えた操作の名前の並び。"""
        with self._lock:
            return [event[0] for event in self.events]


class _FakeVoice:
    def __init__(self, lease: FakeLease) -> None:
        self._lease = lease

    def say(self, text: str) -> None:
        self._lease.record("say", text)
        if self._lease.speak_hook is not None:
            self._lease.speak_hook()
        if self._lease.say_error is not None:
            raise self._lease.say_error
        self._lease.record("said", text)


class _FakeDisplay:
    def __init__(self, lease: FakeLease) -> None:
        self._lease = lease

    def show(self, line1: str, line2: str) -> None:
        self._lease.record("show", line1, line2)


class _FakeNeck:
    def __init__(self, lease: FakeLease) -> None:
        self._lease = lease

    def look(self, pan: float, tilt: float) -> None:
        self._lease.record("look", pan, tilt)

    def home(self) -> None:
        self._lease.record("home")

    def nod(self, times: int = 2) -> None:
        self._lease.record("nod", times)

    def dance(self, drive: object = None) -> None:
        """`drive` は走行の口（遊びは渡さない）。渡されたかを覚える。"""
        self._lease.record("dance", drive)
        if self._lease.dance_hook is not None:
            self._lease.dance_hook()
        if self._lease.dance_error is not None:
            raise self._lease.dance_error
        self._lease.record("danced")


class _FakeButton:
    def __init__(self, lease: FakeLease) -> None:
        self._lease = lease

    def clear(self) -> None:
        self._lease.presses = 0
        self._lease.record("clear")

    def has_press(self) -> bool:
        self._lease.record("has_press")
        return self._lease.presses > 0

    def wait_press(self, timeout: float | None) -> bool:
        self._lease.record("wait_press", timeout)
        if self._lease.presses > 0:
            self._lease.presses -= 1
            return True
        return False
