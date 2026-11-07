"""聞き取りの口（ポート）の型（6.3〜6.5、1.7、9.2）。

入口の流れはこの型だけを見る。実機の聞き取り（arecord と faster-whisper）と、
端末で打った文字の聞き取りと、テストの偽の聞き取りが、この型を満たす。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import Enum
from typing import Protocol


class RecognizerState(Enum):
    """聞き取りの状態。"""

    PREPARING = "preparing"  # 準備中（モデルを読んでいる、など）
    READY = "ready"  # 使える
    UNAVAILABLE = "unavailable"  # 使えない（マイクがない、モデルを読めない、など）


class ListenCancelled(Exception):
    """続けてよくなくなったので、聞き取りを取り消した。結果は捨てる（1.7）。"""


class RecognitionFailed(Exception):
    """録音か、文字にする処理に失敗した。

    決まった理由（例外の型の名前など）だけを持つ。聞き取った文字や音は持たない。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @classmethod
    def from_error(cls, error: BaseException) -> RecognitionFailed:
        """例外から、型の名前だけを理由にして作る（例外の文言は持たない）。"""
        return cls(type(error).__name__)


class Recognizer(Protocol):
    """聞き取りの口。

    - `state` はどのスレッドから読んでもよい
    - `prepare()` は起動のときに入口のスレッドで 1 回呼ぶ（6.5）。終わるまで `state` は
      PREPARING。失敗は例外にせず UNAVAILABLE にする
    - `listen()` は READY のときにだけ呼ぶ
    """

    @property
    def state(self) -> RecognizerState: ...

    def prepare(self) -> None:
        """聞き取りの準備をする。終われば READY、失敗なら UNAVAILABLE にする。"""
        ...

    def listen(
        self,
        seconds: float,
        *,
        hints: Sequence[str],
        should_continue: Callable[[], bool],
        on_recording: Callable[[bool], None],
    ) -> str:
        """`seconds` 秒だけ録音して、文字にして返す。

        - 録音を始めるときに `on_recording(True)`、終わるときに `on_recording(False)` を呼ぶ。
          失敗や取り消しで終わるときも、始めたなら必ず `on_recording(False)` を呼ぶ
          （LED を消すため）
        - 録音の間と文字にする間に `should_continue()` を何度も見る。偽になったら、
          そこで止めて `ListenCancelled` を出す
        - 何も聞き取れなかったら（静か）、空の文字 "" を返す
        - 録音や文字にする処理の失敗は `RecognitionFailed` にする
        - `hints` は聞き取りのヒントにする語句（登録の語句）
        - 声はメモリの中だけに置き、ファイルに書かない（7.2）
        """
        ...
