"""聞き取りの口の型と、テスト用の偽の聞き取り（9.2）。"""

from __future__ import annotations

import threading

import pytest
from voice_fakes import Cancel, Fail, ScriptedRecognizer

from osekkai.adapters.fake.clock import FakeClock
from osekkai.voice.recognizer import (
    ListenCancelled,
    RecognitionFailed,
    Recognizer,
    RecognizerState,
)


class _Calls:
    """録音中の知らせと、続けてよいかの問い合わせを順に残す。"""

    def __init__(self, *, allow: int | None = None) -> None:
        self.events: list[str] = []
        self.polls = 0
        self._allow = allow  # None なら何回でも続けてよい

    def on_recording(self, recording: bool) -> None:
        self.events.append("録音の始まり" if recording else "録音の終わり")

    def should_continue(self) -> bool:
        self.polls += 1
        return self._allow is None or self.polls <= self._allow


def _listen(
    recognizer: Recognizer, calls: _Calls, *, seconds: float = 5.0, hints: tuple[str, ...] = ()
) -> str:
    return recognizer.listen(
        seconds,
        hints=hints,
        should_continue=calls.should_continue,
        on_recording=calls.on_recording,
    )


def _ready(*script: str | Fail | Cancel, clock: FakeClock | None = None) -> ScriptedRecognizer:
    recognizer = ScriptedRecognizer(script, clock=clock)
    recognizer.prepare()
    return recognizer


# --- 口の型 ---


def test_状態は準備中と使えると使えないの3つ() -> None:
    assert {state.value for state in RecognizerState} == {"preparing", "ready", "unavailable"}


def test_失敗は決まった理由だけを持ち文字を持たない() -> None:
    error = RecognitionFailed("record_timeout")
    assert error.reason == "record_timeout"
    assert str(error) == "record_timeout"


def test_失敗は例外から型の名前だけを取る() -> None:
    error = RecognitionFailed.from_error(ValueError("聞き取った言葉"))
    assert error.reason == "ValueError"
    assert "聞き取った言葉" not in str(error)
    assert "聞き取った言葉" not in repr(error)


def test_取り消しと失敗は別の例外() -> None:
    assert not issubclass(ListenCancelled, RecognitionFailed)
    assert not issubclass(RecognitionFailed, ListenCancelled)


def test_偽の聞き取りは口の型を満たす() -> None:
    recognizer: Recognizer = ScriptedRecognizer(["だるまさん しよう"])
    recognizer.prepare()
    assert recognizer.state is RecognizerState.READY
    assert _listen(recognizer, _Calls()) == "だるまさん しよう"


# --- 準備 ---


def test_準備する前は準備中() -> None:
    assert ScriptedRecognizer([]).state is RecognizerState.PREPARING


def test_準備に失敗すると使えない() -> None:
    recognizer = ScriptedRecognizer([], prepare_fails=True)
    recognizer.prepare()
    assert recognizer.state is RecognizerState.UNAVAILABLE
    assert recognizer.prepare_calls == 1


def test_準備はテストが合図するまで準備中のまま() -> None:
    gate = threading.Event()
    recognizer = ScriptedRecognizer([], prepare_gate=gate)
    thread = threading.Thread(target=recognizer.prepare)
    thread.start()
    try:
        assert recognizer.prepare_started.wait(timeout=2.0)
        before = recognizer.state
        gate.set()
        assert recognizer.prepared.wait(timeout=2.0)
        after = recognizer.state
        assert (before, after) == (RecognizerState.PREPARING, RecognizerState.READY)
    finally:
        gate.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_合図が来ないまま時間が過ぎると使えない() -> None:
    recognizer = ScriptedRecognizer([], prepare_gate=threading.Event(), prepare_timeout=0.01)
    recognizer.prepare()
    assert recognizer.state is RecognizerState.UNAVAILABLE


def test_状態はテストから変えられる() -> None:
    recognizer = _ready()
    recognizer.set_state(RecognizerState.UNAVAILABLE)
    assert recognizer.state is RecognizerState.UNAVAILABLE


# --- 台本どおりに返す ---


def test_台本の文字を順に返し秒とヒントを残す() -> None:
    recognizer = _ready("だるまさん しよう", "おやすみ")
    first = _listen(recognizer, _Calls(), seconds=5.0, hints=("だるまさん",))
    second = _listen(recognizer, _Calls(), seconds=3.0, hints=())
    assert (first, second) == ("だるまさん しよう", "おやすみ")
    assert [(call.seconds, call.hints) for call in recognizer.calls] == [
        (5.0, ("だるまさん",)),
        (3.0, ()),
    ]
    assert recognizer.remaining == 0


def test_静かなときは空の文字を返す() -> None:
    calls = _Calls()
    assert _listen(_ready(""), calls) == ""
    assert calls.events == ["録音の始まり", "録音の終わり"]


def test_台本の失敗は理由つきの失敗になる() -> None:
    calls = _Calls()
    with pytest.raises(RecognitionFailed) as caught:
        _listen(_ready(Fail("TimeoutExpired")), calls)
    assert caught.value.reason == "TimeoutExpired"
    assert calls.events == ["録音の始まり", "録音の終わり"]


def test_録音の途中の失敗でも録音の終わりを知らせる() -> None:
    calls = _Calls()
    with pytest.raises(RecognitionFailed):
        _listen(_ready(Fail("CalledProcessError", while_recording=True)), calls)
    assert calls.events == ["録音の始まり", "録音の終わり"]


def test_台本の取り消しは決めた回数を問い合わせた後に取り消す() -> None:
    calls = _Calls()
    with pytest.raises(ListenCancelled):
        _listen(_ready(Cancel(after_polls=3)), calls)
    assert calls.polls == 3
    assert calls.events == ["録音の始まり", "録音の終わり"]


def test_続けてよくなくなったら取り消す() -> None:
    calls = _Calls(allow=0)
    with pytest.raises(ListenCancelled):
        _listen(_ready("だるまさん しよう"), calls)
    assert calls.events == ["録音の始まり", "録音の終わり"]


def test_文字にする間に続けてよくなくなっても取り消す() -> None:
    # 録音の間に 1 回、文字にする間に 1 回問い合わせる。2 回目で偽になる
    calls = _Calls(allow=1)
    with pytest.raises(ListenCancelled):
        _listen(_ready("だるまさん しよう"), calls)
    assert calls.polls == 2
    assert calls.events == ["録音の始まり", "録音の終わり"]


def test_台本の取り消しでも続けてよくなくなればそこで取り消す() -> None:
    calls = _Calls(allow=1)
    with pytest.raises(ListenCancelled):
        _listen(_ready(Cancel(after_polls=5)), calls)
    assert calls.polls == 2


def test_台本がなくなると分かりやすく止まる() -> None:
    with pytest.raises(AssertionError):
        _listen(_ready(), _Calls())


def test_録音と文字にするのにかかった時間だけ時計を進める() -> None:
    clock = FakeClock()
    seen: list[tuple[bool, float]] = []
    recognizer = ScriptedRecognizer(
        ["だるまさん しよう"], clock=clock, record_seconds=5.0, transcribe_seconds=1.5
    )
    recognizer.prepare()
    start = clock.monotonic()
    recognizer.listen(
        5.0,
        hints=(),
        should_continue=lambda: True,
        on_recording=lambda on: seen.append((on, clock.monotonic() - start)),
    )
    assert seen == [(True, 0.0), (False, 5.0)]
    assert clock.monotonic() - start == pytest.approx(6.5)
