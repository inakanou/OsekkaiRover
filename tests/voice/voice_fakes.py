"""音声の入口のテストで共通に使う偽物。

`from voice_fakes import ...` で読む（tests/voice が import の道にある）。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.types import LeaseRevoked, SpeechFailed
from osekkai.voice.recognizer import ListenCancelled, RecognitionFailed, RecognizerState


@dataclass(frozen=True)
class Fail:
    """台本: 失敗する。`while_recording` なら録音の途中、そうでなければ文字にする間。"""

    reason: str = "RuntimeError"
    while_recording: bool = False


@dataclass(frozen=True)
class Cancel:
    """台本: 続けてよいかを `after_polls` 回見た後に、取り消す（中断が来たのと同じ）。"""

    after_polls: int = 0


Outcome = str | Fail | Cancel  # str は聞き取った文字（"" は静か）


@dataclass(frozen=True)
class ListenCall:
    seconds: float
    hints: tuple[str, ...]


class ScriptedRecognizer:
    """台本どおりに返す、偽の聞き取り（`Recognizer` を満たす）。

    - 準備の前は PREPARING。`prepare()` で READY（`prepare_fails` なら UNAVAILABLE）
    - `prepare_gate` を渡すと、その合図が来るまで `prepare()` が待つ（準備の遅れ）。
      `prepare_timeout` 秒が過ぎても来なければ UNAVAILABLE にして戻る
    - `listen()` は台本を 1 つずつ取り出す。録音の間に 1 回、文字にする間に 1 回
      `should_continue()` を見て、偽なら取り消す
    - `clock` を渡すと、録音で `record_seconds`、文字にするので `transcribe_seconds` 進める
    """

    def __init__(
        self,
        script: Iterable[Outcome],
        *,
        clock: FakeClock | None = None,
        record_seconds: float = 0.0,
        transcribe_seconds: float = 0.0,
        prepare_fails: bool = False,
        prepare_gate: threading.Event | None = None,
        prepare_timeout: float = 5.0,
    ) -> None:
        self._lock = threading.Lock()
        self._script: deque[Outcome] = deque(script)
        self._state = RecognizerState.PREPARING
        self._clock = clock
        self._record_seconds = record_seconds
        self._transcribe_seconds = transcribe_seconds
        self._prepare_fails = prepare_fails
        self._prepare_gate = prepare_gate
        self._prepare_timeout = prepare_timeout
        self.prepare_started = threading.Event()
        self.prepared = threading.Event()
        self.prepare_calls = 0
        self.calls: list[ListenCall] = []

    # --- Recognizer の口 ---

    @property
    def state(self) -> RecognizerState:
        with self._lock:
            return self._state

    def prepare(self) -> None:
        with self._lock:
            self.prepare_calls += 1
        self.prepare_started.set()
        ok = not self._prepare_fails
        if self._prepare_gate is not None and not self._prepare_gate.wait(self._prepare_timeout):
            ok = False
        self.set_state(RecognizerState.READY if ok else RecognizerState.UNAVAILABLE)
        self.prepared.set()

    def listen(
        self,
        seconds: float,
        *,
        hints: Sequence[str],
        should_continue: Callable[[], bool],
        on_recording: Callable[[bool], None],
    ) -> str:
        with self._lock:
            self.calls.append(ListenCall(seconds, tuple(hints)))
            if not self._script:
                raise AssertionError("偽の聞き取りの台本がもうありません")
            outcome = self._script.popleft()

        on_recording(True)
        try:
            if isinstance(outcome, Cancel):
                for _ in range(outcome.after_polls):
                    if not should_continue():
                        break
                raise ListenCancelled
            if isinstance(outcome, Fail) and outcome.while_recording:
                raise RecognitionFailed(outcome.reason)
            if not should_continue():
                raise ListenCancelled
            self._advance(self._record_seconds)
        finally:
            on_recording(False)

        self._advance(self._transcribe_seconds)
        if isinstance(outcome, Fail):
            raise RecognitionFailed(outcome.reason)
        if not should_continue():
            raise ListenCancelled
        return outcome

    # --- テストから操作する口 ---

    def set_state(self, state: RecognizerState) -> None:
        with self._lock:
            self._state = state

    @property
    def remaining(self) -> int:
        """まだ使っていない台本の数。"""
        with self._lock:
            return len(self._script)

    def _advance(self, seconds: float) -> None:
        if self._clock is not None and seconds > 0:
            self._clock.advance(seconds)


Event = tuple[object, ...]


class ScriptedGatewayIO:
    """入出力（`GatewayIO`）の偽物。呼ばれた順に `events` に残す。

    - `events` を渡すと、その並びに足す（資源を返すことや口を呼ぶことと順を比べるため）
    - `say_fails` なら話すたびに SpeechFailed を出す（残すのは出す前）
    - `revoke_on` に操作の名前（"show"・"led"・"say"・"nod"）と何回目か（1 から）を渡すと、
      その呼び出しで LeaseRevoked を出す（日課に取り上げられたのと同じ）
    """

    def __init__(
        self,
        events: list[Event] | None = None,
        *,
        say_fails: bool = False,
        revoke_on: tuple[str, int] | None = None,
    ) -> None:
        self.events: list[Event] = events if events is not None else []
        self._say_fails = say_fails
        self._revoke_on = revoke_on
        self._counts: dict[str, int] = {}

    def show(self, line1: str, line2: str) -> None:
        self._enter("show", line1, line2)

    def led(self, on: bool) -> None:
        self._enter("led", on)

    def say(self, text: str) -> None:
        self._enter("say", text)
        if self._say_fails:
            raise SpeechFailed("voice")

    def nod(self) -> None:
        self._enter("nod")

    # --- テストから見る口 ---

    @property
    def sayings(self) -> list[str]:
        return [str(e[1]) for e in self.events if e[0] == "say"]

    @property
    def displays(self) -> list[tuple[str, str]]:
        return [(str(e[1]), str(e[2])) for e in self.events if e[0] == "show"]

    @property
    def leds(self) -> list[bool]:
        return [bool(e[1]) for e in self.events if e[0] == "led"]

    def _enter(self, name: str, *args: object) -> None:
        count = self._counts.get(name, 0) + 1
        self._counts[name] = count
        if self._revoke_on == (name, count):
            raise LeaseRevoked("voice")
        self.events.append((name, *args))
