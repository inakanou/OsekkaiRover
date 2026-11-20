"""資源を借りて、朝と夜のお話を文ごとに話す流れ（TalkFlow）。

要件 4.4、6.8、7.1〜7.3、7.5〜7.8、8.1〜8.3。

始まり → 天気の文 → 豆知識 → 終わりの順に文を並べ（7.1）、声と画面だけを待たずに借りて話す。
借りたら最初の文の前に記録を付ける（4.4）。文ごとに中断の知らせ・取り上げ・静かな状態・
機能を止める旗を確かめ、文の切れ目でやめる（8.1、8.3）。声は残さない（6.8）。最後に必ず資源を返す（7.8）。
ログには決まった文言と種類の名前・文の数だけを出す（呼び名・天気・豆知識は出さない）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Final

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
from osekkai.features.talk import texts
from osekkai.features.talk.config import TalkConfig
from osekkai.features.talk.model import Prepared
from osekkai.features.talk.weather_talk import lcd_line, weather_sentences

logger = logging.getLogger(__name__)

RESOURCES: Final = frozenset({Resource.VOICE, Resource.DISPLAY})  # モーターは借りない（8.6）
ACQUIRE_WAIT_S: Final = 0.0  # 待たずに借りる。借りられなければ次の秒に判定し直す（8.4）
# 話して困る誤り。その場でお話をやめる
_SPEAK_ERRORS: Final = (LeaseRevoked, SpeechFailed, DeviceUnavailable)


@dataclass(frozen=True)
class Line:
    """話す 1 文と、その間の LCD の 2 行（中央に置く前の文字）。"""

    text: str
    lcd1: str
    lcd2: str


class FlowOutcome(Enum):
    """1 回のお話の結果。"""

    SPOKEN = "spoken"  # 終わりまで話した
    QUIET = "quiet"  # 静かな状態なので借りなかった（記録していない）
    # 借りられなかった・借りた直後に取られた・止める旗が立っていた（記録していない。
    # 次の秒（次に動いたとき）にやり直す）
    BUSY = "busy"
    RECORD_FAILED = "record_failed"  # 記録を書けなかったので話さなかった（4.4）
    # 途中で中断・静かな状態・止める旗が立ち、文の切れ目でやめた（記録済み）
    INTERRUPTED = "interrupted"
    FAILED = "failed"  # 話せなかった・画面を使えなかった（記録済み）


def compose_lines(prepared: Prepared, config: TalkConfig) -> list[Line]:
    """話す文と LCD を決まった順に並べる（7.1、7.5、7.6、7.7）。

    天気の文の間は天気の表示とふつうの顔、そのほかは「ｵﾊﾅｼ」と喜んだ顔。天気も豆知識も
    なければ、始まりの言葉を言わずに決まった短い言葉だけにする（7.6）。
    呼び名を入れた後の文は `format` にかけない（呼び名の波かっこで壊れるため）。
    """
    kind = prepared.kind
    happy = Face.HAPPY.text()
    if prepared.forecast is None and prepared.fact is None:
        return [Line(texts.FALLBACKS[kind], texts.LCD_TALK, happy)]
    lines = [Line(texts.fill_name(texts.OPENERS[kind], config.call_name), texts.LCD_TALK, happy)]
    if prepared.forecast is not None:
        weather_lcd = lcd_line(kind, prepared.forecast)
        normal = Face.NORMAL.text()
        lines.extend(
            Line(sentence, weather_lcd, normal)
            for sentence in weather_sentences(kind, prepared.forecast, prepared.alerts, config)
        )
    if prepared.fact is not None:
        lines.append(Line(prepared.fact, texts.LCD_TALK, happy))
    lines.append(
        Line(texts.fill_name(texts.CLOSERS[kind], config.call_name), texts.LCD_TALK, happy)
    )
    return lines


class TalkFlow:
    """資源を借りてお話をする流れ。機能のループのスレッドから、1 つずつ呼ぶ。

    `stopped` は機能を止める旗が立ったか（基盤が止めるときに、文の切れ目ですぐやめるため）。
    """

    def __init__(
        self, ctx: FeatureContext, config: TalkConfig, *, stopped: Callable[[], bool]
    ) -> None:
        self._ctx = ctx
        self._config = config
        self._stopped = stopped

    def speak(self, prepared: Prepared, on_started: Callable[[], None]) -> FlowOutcome:
        """用意したものを話す。`on_started` は借りた直後、最初の文の前に呼ぶ（記録。4.4）。

        `on_started` が `OSError` を上げたら話さずに `RECORD_FAILED` を返す。
        """
        kind_name = prepared.kind.name
        if self._ctx.is_quiet:  # 8.2
            logger.info("静かな状態なのでお話をしませんでした: %s", kind_name)
            return FlowOutcome.QUIET
        if self._stopped():
            logger.info("止めるのでお話をしませんでした: %s", kind_name)
            return FlowOutcome.BUSY
        lines = compose_lines(prepared, self._config)
        preempted = threading.Event()
        try:
            lease = self._ctx.acquire(RESOURCES, wait=ACQUIRE_WAIT_S, on_preempt=preempted.set)
        except ResourceBusy:  # 8.4
            logger.info("声と画面を借りられなかったのでお話を待ちます: %s", kind_name)
            return FlowOutcome.BUSY
        try:
            return self._with_lease(lease, preempted, lines, on_started, kind_name)
        finally:
            lease.release()  # 7.8。待機画面に戻すのは基盤

    def _with_lease(
        self,
        lease: Lease,
        preempted: threading.Event,
        lines: list[Line],
        on_started: Callable[[], None],
        kind_name: str,
    ) -> FlowOutcome:
        def interrupted() -> bool:
            return (
                preempted.is_set()
                or lease.preempt_requested.is_set()
                or not lease.active
                or self._ctx.is_quiet
                or self._stopped()
            )

        # 借りた直後に止まったら、記録せずに返す（静かな状態か、次の秒にやり直す）
        if self._ctx.is_quiet:
            logger.info("借りた後に静かな状態になったのでお話をしませんでした: %s", kind_name)
            return FlowOutcome.QUIET
        if interrupted():
            logger.info("借りた直後に中断されたのでお話を待ちます: %s", kind_name)
            return FlowOutcome.BUSY
        try:
            on_started()  # 4.4
        except OSError as error:
            logger.warning(
                "記録を書けなかったのでお話をしませんでした: %s（%s）",
                kind_name,
                type(error).__name__,
            )
            return FlowOutcome.RECORD_FAILED
        try:
            lease.display.set_backlight(True)  # 表示の前に点ける（7.7）
            for index, line in enumerate(lines):
                if interrupted():  # 8.1、8.3。文の切れ目でやめる
                    logger.info(
                        "お話を途中でやめました: %s（%d / %d 文）", kind_name, index, len(lines)
                    )
                    return FlowOutcome.INTERRUPTED
                lease.display.show(center(line.lcd1), center(line.lcd2))  # 7.7
                lease.voice.say(line.text, keep=False)  # 6.8
        except LeaseRevoked:
            logger.info("資源を取り上げられたのでお話をやめました: %s", kind_name)
            return FlowOutcome.INTERRUPTED
        except _SPEAK_ERRORS as error:
            logger.warning("お話を話せませんでした: %s（%s）", kind_name, type(error).__name__)
            return FlowOutcome.FAILED
        logger.info("お話を話しました: %s（%d 文）", kind_name, len(lines))
        return FlowOutcome.SPOKEN
