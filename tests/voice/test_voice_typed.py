"""打った文字の聞き取り（9.1）。"""

from __future__ import annotations

import io
import os
import threading
from collections.abc import Iterator

import pytest

from osekkai.adapters.fake.clock import FakeClock
from osekkai.adapters.fake.console import ConsoleButtons, _reset_typed_input_for_tests
from osekkai.voice.adapters.typed import TypedRecognizer
from osekkai.voice.recognizer import ListenCancelled, Recognizer, RecognizerState


@pytest.fixture(autouse=True)
def _reset_typed_input() -> Iterator[None]:
    _reset_typed_input_for_tests()
    yield
    _reset_typed_input_for_tests()


class _Calls:
    def __init__(self, *, keep_going: bool = True) -> None:
        self.events: list[str] = []
        self._keep_going = keep_going

    def on_recording(self, recording: bool) -> None:
        self.events.append("録音の始まり" if recording else "録音の終わり")

    def should_continue(self) -> bool:
        return self._keep_going


def _listen(recognizer: Recognizer, calls: _Calls) -> str:
    return recognizer.listen(
        5.0,
        hints=("だるまさん",),
        should_continue=calls.should_continue,
        on_recording=calls.on_recording,
    )


def _type_line(clock: FakeClock, line: str) -> tuple[ConsoleButtons, TypedRecognizer, str]:
    """端末の偽物に 1 行を入れ、押下の知らせの中で聞き取る（実行のときと同じ順番）。"""
    recognizer = TypedRecognizer()
    calls = _Calls()
    heard: list[str] = []
    done = threading.Event()

    def on_falling(source: str, at: float) -> None:
        heard.append(_listen(recognizer, calls))
        done.set()

    buttons = ConsoleButtons(clock, inp=io.StringIO(line))
    buttons.start(on_falling)
    assert done.wait(2.0)
    assert buttons.join_reader(2.0)
    assert calls.events == ["録音の始まり", "録音の終わり"]
    return buttons, recognizer, heard[0]


def test_型を満たす() -> None:
    recognizer: Recognizer = TypedRecognizer()  # 型を満たすかは mypy が確かめる
    recognizer.prepare()
    assert recognizer.state is RecognizerState.UNAVAILABLE


def test_打った文字を返す(clock: FakeClock) -> None:
    buttons, _, heard = _type_line(clock, "だるまさん しよう\n")
    buttons.close()
    assert heard == "だるまさん しよう"


def test_Enter_だけなら空を返す(clock: FakeClock) -> None:
    buttons, _, heard = _type_line(clock, "\n")
    buttons.close()
    assert heard == ""


def test_前後の空白は除く(clock: FakeClock) -> None:
    buttons, _, heard = _type_line(clock, "  あそぼ  \n")
    buttons.close()
    assert heard == "あそぼ"


def test_取り出す行がなければ空を返す() -> None:
    calls = _Calls()
    assert _listen(TypedRecognizer(), calls) == ""
    assert calls.events == ["録音の始まり", "録音の終わり"]


def test_端末の偽物が動いていれば使え_動いていなければ使えない(clock: FakeClock) -> None:
    recognizer = TypedRecognizer()
    states: list[RecognizerState] = []
    recognizer.prepare()
    states.append(recognizer.state)
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "r") as inp, os.fdopen(write_fd, "w") as feed:
        buttons = ConsoleButtons(clock, inp=inp)
        buttons.start(lambda source, at: None)
        recognizer.prepare()
        states.append(recognizer.state)
        buttons.close()
        states.append(recognizer.state)
        feed.close()
        assert buttons.join_reader(2.0)
    assert states == [
        RecognizerState.UNAVAILABLE,
        RecognizerState.READY,
        RecognizerState.UNAVAILABLE,
    ]


def test_EOF_の後は使えない(clock: FakeClock) -> None:
    buttons, recognizer, _ = _type_line(clock, "あ\n")
    assert recognizer.state is RecognizerState.UNAVAILABLE
    buttons.close()


def test_続けてよくなければ取り消し_録音の終わりも知らせる(clock: FakeClock) -> None:
    calls = _Calls(keep_going=False)
    with pytest.raises(ListenCancelled):
        _listen(TypedRecognizer(), calls)
    assert calls.events == ["録音の始まり", "録音の終わり"]
