"""押下から振り分けまでの流れ（1.1、1.2、1.4、1.5、1.7、2.5、3.3、3.4、4.3、6.1、6.2、7.3〜7.5）。

ListenFlow は、借りた資源の入出力（GatewayIO）と聞き取り（Recognizer）と登録表だけを見る。
資源を借りること（と中断の旗）は呼ぶ側（機能の本体）が持ち、ここには `cancelled` と
`release` を渡す。

- 聞き取りのヒントには、登録表の語句（`registry.hints()`）だけを渡す。既定のヒント
  （texts.DEFAULT_HINT）は、聞き取りのアダプタが自分で前に足す
- 「わからない」と「きこえなかった」は資源を返す前に話す。機能に渡すときは、資源を返してから渡す
- ログには、決まった文言・機能の名前・型の名前・かかった時間だけを出す。
  聞き取った言葉・登録の語句・話す言葉・LCD の文字・言葉の長さは出さない
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from osekkai.core.arbiter import Lease
from osekkai.core.clock import Clock
from osekkai.core.lcd_text import center
from osekkai.core.types import DeviceUnavailable, Face, LeaseRevoked, SpeechFailed
from osekkai.voice.intents import Fallback, Handler, Heard, IntentRegistry, Words
from osekkai.voice.recognizer import ListenCancelled, RecognitionFailed, Recognizer
from osekkai.voice.texts import (
    LCD_LISTENING,
    LCD_SPEAK,
    LCD_THINKING,
    SAY_CALL,
    SAY_FOLLOW_UP,
    SAY_NOT_HEARD,
    SAY_UNKNOWN,
)


class GatewayIO(Protocol):
    """入口が使う入出力。借りた資源の口で動かす。

    資源を取り上げられたら LeaseRevoked、声を出せなければ SpeechFailed を出してよい。
    """

    def show(self, line1: str, line2: str) -> None:
        """LCD に 2 行を出す（2 行とも中央に置く）。"""
        ...

    def led(self, on: bool) -> None:
        """LED を点ける・消す。"""
        ...

    def say(self, text: str) -> None:
        """話す。話し終わるまで戻らない。"""
        ...

    def nod(self) -> None:
        """首で 1 回うなずく。"""
        ...


class LeaseGatewayIO:
    """借りた資源（Lease）で動かす入出力。

    リースには VOICE・DISPLAY・LED・NECK が要る。
    """

    def __init__(self, lease: Lease) -> None:
        self._lease = lease

    def show(self, line1: str, line2: str) -> None:
        self._lease.display.show(center(line1), center(line2))

    def led(self, on: bool) -> None:
        self._lease.led.set(on)

    def say(self, text: str) -> None:
        self._lease.voice.say(text)

    def nod(self) -> None:
        self._lease.neck.nod(1)


class OutcomeKind(Enum):
    """1 回の聞き取りの結果の種類。"""

    ROUTED = "routed"  # 機能に渡した
    NOT_HEARD = "not_heard"  # 空だった（6.1）
    NOT_UNDERSTOOD = "not_understood"  # 渡す先がなかった（3.4）
    DISCARDED = "discarded"  # 中断で捨てた（1.7）
    FAILED = "failed"  # 録音か文字にする処理に失敗した（6.2）


@dataclass(frozen=True)
class ListenOutcome:
    """1 回の聞き取りの結果。`owner` は渡した先の機能の名前（ROUTED のときだけ）。"""

    kind: OutcomeKind
    owner: str | None = None


_DISCARDED = ListenOutcome(OutcomeKind.DISCARDED)


class _Session:
    """1 回の聞き取りの間の状態（取り上げられたか・資源を返したか）。"""

    def __init__(
        self,
        io: GatewayIO,
        cancelled: Callable[[], bool],
        release: Callable[[], None],
        log: logging.Logger,
    ) -> None:
        self.io = io
        self._cancelled = cancelled
        self._release = release
        self._log = log
        self._revoked = False
        self._released = False

    def stopped(self) -> bool:
        """中断の旗が立ったか、資源を取り上げられたか。"""
        return self._revoked or self._cancelled()

    def do(self, operation: Callable[[], None]) -> None:
        """入出力を 1 つ行う。取り上げられたら覚えておく。声と画面の失敗は記録して続ける。"""
        if self._revoked:
            return
        try:
            operation()
        except LeaseRevoked:
            self._revoked = True
        except (SpeechFailed, DeviceUnavailable) as error:
            self._log.warning("入出力に失敗しました: %s", type(error).__name__)

    def release(self) -> None:
        """資源を返す。2 度目からは何もしない。"""
        if self._released:
            return
        self._released = True
        self._release()


class ListenFlow:
    """ボタンの押下から、言葉を機能に渡すまで。"""

    def __init__(
        self,
        recognizer: Recognizer,
        registry: IntentRegistry,
        clock: Clock,
        log: logging.Logger,
    ) -> None:
        self._recognizer = recognizer
        self._registry = registry
        self._clock = clock
        self._log = log

    def run(
        self,
        io: GatewayIO,
        *,
        follow_up_owner: str | None,
        seconds: float,
        cancelled: Callable[[], bool],
        release: Callable[[], None],
    ) -> ListenOutcome:
        """1 回聞いて振り分ける。どの終わり方でも `release()` を 1 回呼ぶ。

        `follow_up_owner` があれば続けての聞き取りにし、振り分けずにその機能へ渡す（4.3）。
        `cancelled()` が真になったら（日課の中断）、言葉を捨てて何も話さない（1.7）。
        """
        session = _Session(io, cancelled, release, self._log)
        try:
            return self._run(session, follow_up_owner, seconds)
        finally:
            session.release()

    def _run(self, session: _Session, follow_up_owner: str | None, seconds: float) -> ListenOutcome:
        io = session.io
        self._log.info("聞き取りを始めました")
        steps: list[Callable[[], None]] = []
        if follow_up_owner is None:
            steps.append(io.nod)
        steps.append(lambda: io.show(LCD_LISTENING, Face.CALL.text()))
        steps.append(lambda: io.say(SAY_CALL if follow_up_owner is None else SAY_FOLLOW_UP))
        for step in steps:
            session.do(step)
            if session.stopped():
                return self._discard(session)

        marks: dict[str, float] = {}

        def on_recording(recording: bool) -> None:
            marks["start" if recording else "end"] = self._clock.monotonic()
            session.do(lambda: io.led(recording))
            if session.stopped():
                return
            if recording:
                # 話してよい合図は LCD に出す（LED は小さくて子どもから見えない）
                session.do(lambda: io.show(LCD_SPEAK, Face.CALL.text()))
            else:
                session.do(lambda: io.show(LCD_THINKING, Face.NORMAL.text()))

        try:
            text = self._recognizer.listen(
                seconds,
                hints=self._registry.hints(),
                should_continue=lambda: not session.stopped(),
                on_recording=on_recording,
            )
        except ListenCancelled:
            return self._discard(session)
        except RecognitionFailed as error:
            return self._failed(session, error.reason)
        except Exception as error:  # 型に従わない失敗も、失敗として扱う
            return self._failed(session, type(error).__name__)
        done = self._clock.monotonic()
        if "start" in marks and "end" in marks:
            self._log.info(
                "かかった時間: 録音 %.1f 秒、文字にする %.1f 秒",
                marks["end"] - marks["start"],
                done - marks["end"],
            )

        if session.stopped():
            return self._discard(session)
        if not text.strip():
            self._log.info("聞き取れませんでした")
            return self._answer(session, SAY_NOT_HEARD, ListenOutcome(OutcomeKind.NOT_HEARD))

        if follow_up_owner is not None:
            handler = self._registry.handler_for(follow_up_owner)
            if handler is None:
                return self._not_understood(session)
            return self._deliver(session, follow_up_owner, handler, Heard(text, follow_up=True))

        route = self._registry.route(text)
        if isinstance(route, (Words, Fallback)):
            registration = route.registration
            return self._deliver(
                session, registration.owner, registration.handler, Heard(text, follow_up=False)
            )
        return self._not_understood(session)

    def _discard(self, session: _Session) -> ListenOutcome:
        self._log.info("中断が来たので、聞き取りを捨てました")
        session.release()
        return _DISCARDED

    def _failed(self, session: _Session, reason: str) -> ListenOutcome:
        if session.stopped():
            return self._discard(session)
        self._log.warning("聞き取りに失敗しました: %s", reason)
        return self._answer(session, SAY_NOT_HEARD, ListenOutcome(OutcomeKind.FAILED))

    def _not_understood(self, session: _Session) -> ListenOutcome:
        self._log.info("渡す先がありませんでした")
        return self._answer(session, SAY_UNKNOWN, ListenOutcome(OutcomeKind.NOT_UNDERSTOOD))

    def _answer(self, session: _Session, text: str, outcome: ListenOutcome) -> ListenOutcome:
        """資源を返す前に話す。中断が来ていたら話さずに捨てる（1.7）。"""
        if session.stopped():
            return self._discard(session)
        session.do(lambda: session.io.say(text))
        if session.stopped():
            return self._discard(session)
        session.release()
        return outcome

    def _deliver(
        self, session: _Session, owner: str, handler: Handler, heard: Heard
    ) -> ListenOutcome:
        """資源を返してから、機能の口へ渡す（2.5）。口の例外は記録して続ける。

        振り分けの間に中断が来ていたら、渡さずに捨てる（1.7）。
        """
        if session.stopped():
            return self._discard(session)
        session.release()
        self._log.info("振り分けました: %s", owner)
        try:
            handler(heard)
        except Exception as error:  # 機能の口の誤りで入口を止めない
            self._log.warning(
                "渡した先の口で例外がありました: %s（%s）", owner, type(error).__name__
            )
        return ListenOutcome(OutcomeKind.ROUTED, owner)
