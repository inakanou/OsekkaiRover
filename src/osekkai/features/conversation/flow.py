"""1 回の返事の流れ（ReplyFlow）。

要件 1.1、1.2、1.5、2.1、2.3、3.2、3.3、5.4、6.1〜6.4、6.6、7.1〜7.4、8.2、8.4、8.5。

言葉を 1 つ受けて、門の判定 → 問い合わせ → 話す → 数える → やりとりに残す、までを行う。
資源を借りるのはこの部品だけ。話すのは決まった言葉か、整えた返事だけ（子どもの言葉は話さない）。
外部の会話の AI に渡すのは、決まりの文と、呼び名を目印にしたやりとりだけ（8.5）。
ログには、決まった文言・その日の回数・かかった時間・種類の名前だけを出す（8.4）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol

from osekkai.core.arbiter import Lease
from osekkai.core.feature import FeatureContext
from osekkai.core.lcd_text import center
from osekkai.core.types import (
    DeviceUnavailable,
    Face,
    LeaseRevoked,
    Resource,
    ResourceBusy,
    SpeechFailed,
)
from osekkai.features.conversation import texts
from osekkai.features.conversation.config import ConversationConfig
from osekkai.features.conversation.gate import GateReason, check_gate
from osekkai.features.conversation.ports import (
    ChatModelPort,
    ChatResult,
    Failed,
    FailureKind,
    Refused,
    Replied,
)
from osekkai.features.conversation.privacy import NameGuard, shape_reply
from osekkai.features.conversation.session import Session
from osekkai.features.conversation.usage import DailyUsage
from osekkai.voice.api import Heard

logger = logging.getLogger(__name__)

RESOURCES: Final = frozenset({Resource.VOICE, Resource.DISPLAY, Resource.BUTTON})
ACQUIRE_WAIT_S: Final = 2.0  # 資源を待つ長さ（7.4）
POLL_S: Final = 0.05  # 返事を待つ間に、中断・静かな状態・止める旗を見る間（実際の秒）

_FAILURE_SAYS: Final = {
    FailureKind.CONNECTION: texts.SAY_CONNECTION,  # 6.1
    FailureKind.BUSY: texts.SAY_BUSY,  # 6.2
    FailureKind.OTHER: texts.SAY_OTHER,  # 6.3
    FailureKind.TIMEOUT: texts.SAY_OTHER,  # 6.4
}
# 話して困る誤り。記録だけして、数えない・続けて聞かない
_SPEAK_ERRORS: Final = (LeaseRevoked, SpeechFailed, DeviceUnavailable)


class Outcome(Enum):
    """1 回の言葉の結果。"""

    REPLIED = "replied"  # 返事・空の言葉・拒否の言葉を話した（数えた）
    FAILED = "failed"  # 失敗の言葉を話した（数えない）
    GATED = "gated"  # 門の言葉を話して終えた
    DROPPED = "dropped"  # 静かな状態・中断・借りられない・話せない。数えない


class ConversationIO(Protocol):
    """流れが使う声と画面の口。実体は借りた資源で動かす（LeaseConversationIO）。"""

    def show(self, line1: str, line2: str) -> None: ...

    def say(self, text: str, *, keep: bool) -> None:
        """話し終わるまで戻らない。LeaseRevoked・SpeechFailed を投げうる。"""
        ...


class LeaseConversationIO:
    """`ConversationIO` を、借りた `Lease` の声と画面で動かす。2 行とも LCD の中央に置く。"""

    def __init__(self, lease: Lease) -> None:
        self._lease = lease

    def show(self, line1: str, line2: str) -> None:
        self._lease.display.show(center(line1), center(line2))

    def say(self, text: str, *, keep: bool) -> None:
        self._lease.voice.say(text, keep=keep)


IoFactory = Callable[[Lease], ConversationIO]


def _has_letters(text: str) -> bool:
    """かな・漢字・英数字が 1 つでもあるか（「。」だけの返事は空とみなす。1.5）。"""
    return any(ch.isalnum() for ch in text)


@dataclass(frozen=True)
class _Speech:
    """話す言葉と、そのときの顔・声を残すか。"""

    text: str
    face: Face
    keep: bool


class ReplyFlow:
    """1 回の言葉に答える流れ。機能のループのスレッドから、1 つずつ呼ぶ。"""

    def __init__(
        self,
        ctx: FeatureContext,
        config: ConversationConfig,
        model: Callable[[], ChatModelPort | None],
        usage: DailyUsage,
        names: NameGuard,
        stopped: Callable[[], bool],
        *,
        io_factory: IoFactory = LeaseConversationIO,
    ) -> None:
        """`model` は会話の AI を返す口（初めて使うときに選ぶ。None は鍵なし）。"""
        self._ctx = ctx
        self._config = config
        self._model = model
        self._usage = usage
        self._names = names
        self._stopped = stopped
        self._io_factory = io_factory

    def handle(self, heard: Heard, session: Session | None) -> tuple[Outcome, Session | None]:
        """言葉に答え、結果と、この後のやりとり（捨てたら None）を返す。"""
        clock = self._ctx.clock
        # 続けての言葉でないか、やりとりが切れていれば、新しい会話にする（2.3）
        if not heard.follow_up or (session is not None and session.expired(clock.monotonic())):
            session = None
        if self._ctx.is_quiet:  # 7.2
            logger.info("静かな状態なので答えませんでした")
            return Outcome.DROPPED, None
        preempted = threading.Event()
        try:
            lease = self._ctx.acquire(RESOURCES, wait=ACQUIRE_WAIT_S, on_preempt=preempted.set)
        except ResourceBusy:  # 7.4
            logger.info("声と画面を借りられなかったので答えませんでした")
            return Outcome.DROPPED, None
        try:
            return self._with_lease(lease, preempted, heard, session)
        finally:
            lease.release()

    def _with_lease(
        self, lease: Lease, preempted: threading.Event, heard: Heard, session: Session | None
    ) -> tuple[Outcome, Session | None]:
        clock = self._ctx.clock

        def interrupted() -> bool:
            return (
                self._stopped()
                or preempted.is_set()
                or lease.preempt_requested.is_set()
                or not lease.active
                or self._ctx.is_quiet
            )

        if interrupted():  # 借りた後にもう一度見る（7.2）
            logger.info("借りた後に止まったので答えませんでした")
            return Outcome.DROPPED, None
        io = self._io_factory(lease)

        model = self._model()
        synchronized = clock.is_synchronized()
        now = clock.now()
        reason = check_gate(
            ready=model is not None,
            synchronized=synchronized,
            now=now,
            hours=self._config.hours,
            # 時刻が確かなときだけ記録を読む（古い日付を読めなかった日にしない）
            usage_reached=synchronized
            and self._usage.reached(now.date(), self._config.daily_limit),
        )
        if reason is not None or model is None:
            gated = reason if reason is not None else GateReason.NOT_SET_UP
            try:
                io.show(texts.LCD_ROBOT, Face.NORMAL.text())
                io.say(texts.GATE_REPLIES[gated], keep=True)
            except _SPEAK_ERRORS as error:
                logger.warning("門の言葉を話せませんでした: %s", type(error).__name__)
            logger.info("門で止めました: %s", gated.name)
            return Outcome.GATED, None

        # 1.1。問いは呼び名を目印にして送る（8.5）
        try:
            io.show(texts.LCD_THINKING, Face.NORMAL.text())
        except _SPEAK_ERRORS as error:
            logger.warning("考えている表示を出せませんでした: %s", type(error).__name__)
            return Outcome.DROPPED, None
        asked = self._names.mask(heard.text)
        current = (
            session if session is not None else Session(self._config.max_turns, clock.monotonic())
        )
        started = clock.monotonic()
        result = self._ask(model, current, asked, interrupted)
        if result is None:  # 7.1、7.3
            logger.info("返事を待つ間に止まったので話しませんでした")
            return Outcome.DROPPED, None

        speech, kept_reply = self._speech_for(result)
        try:
            io.show(texts.LCD_ROBOT, speech.face.text())  # 1.2
            io.say(speech.text, keep=speech.keep)  # AI の返事は残さない（8.2）
        except _SPEAK_ERRORS as error:
            logger.warning("話せませんでした: %s", type(error).__name__)
            return Outcome.DROPPED, None
        finished = clock.monotonic()
        elapsed_ms = round((finished - started) * 1000)
        if isinstance(result, Failed):
            if interrupted():
                logger.info("話している途中で止まりました")
                return Outcome.DROPPED, None
            current.touch(finished)  # 失敗した問いは残さない（6.6）
            logger.info("失敗の言葉を話しました: %s（%d ms）", result.kind.name, elapsed_ms)
            return Outcome.FAILED, current
        # 話し終えた返事と拒否は、この後に止まっても数える（5.4）
        count = self._usage.add_one(clock.now().date())
        if interrupted():
            # 話している途中の中断は、言い終えてから返す。やりとりには残さない（7.1）
            logger.info("話している途中で止まりました（今日 %d 回目）", count)
            return Outcome.DROPPED, None
        if kept_reply is not None:
            current.add(asked, kept_reply, finished)  # 2.1
            logger.info("返事を話しました（今日 %d 回目、%d ms）", count, elapsed_ms)
        else:
            current.touch(finished)
            kind = "拒否" if isinstance(result, Refused) else "空"
            logger.info("%sの言葉を話しました（今日 %d 回目、%d ms）", kind, count, elapsed_ms)
        return Outcome.REPLIED, current

    def _ask(
        self,
        model: ChatModelPort,
        session: Session,
        asked: str,
        interrupted: Callable[[], bool],
    ) -> ChatResult | None:
        """別のスレッドで問い合わせ、締め切りまで待つ（6.4）。止まったら None。

        締め切りは時計の単調な時刻で決め、実際の待ちも同じ秒数で区切る（偽の時計が進まなくても終わる）。
        締め切りを過ぎて届いた結果は捨てる。
        """
        timeout_s = self._config.reply_timeout_seconds
        turns = session.turns_with(asked)
        done = threading.Event()
        box: list[ChatResult] = []

        def call() -> None:
            try:
                box.append(model.reply(texts.SYSTEM_PROMPT, turns, timeout_s))
            except Exception as error:  # 口は例外を出さない決まりだが、念のため
                logger.warning("会話の AI の呼び出しで誤りが起きました: %s", type(error).__name__)
                box.append(Failed(FailureKind.OTHER))
            finally:
                done.set()

        clock = self._ctx.clock
        deadline = clock.monotonic() + timeout_s  # 問い合わせを始める前に決める
        threading.Thread(target=call, name="osekkai-conversation-reply", daemon=True).start()
        real_left = timeout_s
        while not done.wait(POLL_S):
            real_left -= POLL_S
            if interrupted():
                return None
            if clock.monotonic() >= deadline or real_left <= 0:
                logger.info("返事を待つ時間を過ぎました")
                return Failed(FailureKind.TIMEOUT)
        if interrupted():
            return None
        return box[0]

    def _speech_for(self, result: ChatResult) -> tuple[_Speech, str | None]:
        """話す言葉と、やりとりに残す返事（目印のまま。残さないなら None）。"""
        if isinstance(result, Replied):
            shaped = shape_reply(result.text)
            if not _has_letters(shaped):  # 1.5
                return _Speech(texts.SAY_EMPTY, Face.HAPPY, keep=True), None
            # 呼び名は目印より長いことがあるので、戻した後にもう一度 120 文字までにする（1.4）
            spoken = shape_reply(self._names.fill(shaped))
            return _Speech(spoken, Face.HAPPY, keep=False), shaped
        if isinstance(result, Refused):  # 3.2
            return _Speech(texts.SAY_REFUSED, Face.HAPPY, keep=True), None
        return _Speech(_FAILURE_SAYS[result.kind], Face.NORMAL, keep=True), None
