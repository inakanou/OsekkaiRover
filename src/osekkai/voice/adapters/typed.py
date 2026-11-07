"""打った文字の聞き取り: ハードなしの実行で、端末で打った行を聞き取りの代わりにする（9.1）。

- 端末の偽物（`ConsoleButtons`）が押下を知らせる前に取っておいた行を取り出して返す
- 端末の偽物が動いているときだけ使える
- 打った文字はログに出さない
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from osekkai.adapters.fake.console import take_typed_line, typed_input_active
from osekkai.voice.recognizer import ListenCancelled, RecognizerState

__all__ = ["TypedRecognizer"]


class TypedRecognizer:
    """端末で打った行を返す聞き取り。録音はしないので、すぐに返す。"""

    @property
    def state(self) -> RecognizerState:
        """端末の偽物が動いていれば使える。そうでなければ使えない。"""
        return RecognizerState.READY if typed_input_active() else RecognizerState.UNAVAILABLE

    def prepare(self) -> None:
        """準備することはない（使えるかは `state` を読むたびに確かめる）。"""

    def listen(
        self,
        seconds: float,
        *,
        hints: Sequence[str],
        should_continue: Callable[[], bool],
        on_recording: Callable[[bool], None],
    ) -> str:
        """取っておいた行を取り出して、前後の空白を除いて返す。なければ空の文字を返す。"""
        on_recording(True)
        try:
            text = take_typed_line() or ""
        finally:
            on_recording(False)
        if not should_continue():
            raise ListenCancelled
        return text.strip()
