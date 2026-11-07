"""入口の流れを通して確かめる（1.7、2.2、3.3、3.4、4.3、5.2、5.3、6.1、7.3〜7.5、9.2）。

本物の調停役・静かな状態・ボタンの振り分けを偽のデバイスで組み、偽の聞き取りと偽の時計で、
1 日の流れを 1 つのテストで通す。ほかの機能は登録の口（`osekkai.voice.api`）から登録する。
通しのあいだのログを集め、聞き取った言葉と登録の語句が出ていないことを確かめる。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from voice_bench import WAIT_S, Bench, wait_for
from voice_bench import lcd_row as row
from voice_fakes import ListenCall, ScriptedRecognizer

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import JST, FakeClock
from osekkai.core.types import Face, Priority, Resource
from osekkai.voice import api
from osekkai.voice.intents import Heard, normalize
from osekkai.voice.recognizer import ListenCancelled, RecognizerState
from osekkai.voice.texts import (
    LCD_LISTENING,
    LCD_SLEEP_TIME,
    SAY_CALL,
    SAY_FOLLOW_UP,
    SAY_NOT_HEARD,
    SAY_SLEEP_TIME,
    SAY_UNKNOWN,
)

DARUMA = "daruma"
TALK = "talk"
DARUMA_PHRASES = ("だるまさん",)

HEARD_DARUMA = "ダルマさん しよう！"  # かな・空白・記号が違っても「だるまさん」に一致する
HEARD_TALK = "きょう たのしかった"  # どの語句にも当てはまらない → 受け手
HEARD_FOLLOW_UP = "ぶらんこに のったよ"  # 続けて聞いた言葉 → 振り分けずに talk
HEARD_UNKNOWN = "きょうりゅうの はなし"  # 受け手がいない → わからない
HEARD_NOTHING = ""  # 静か → きこえなかった
HEARD_TEXTS = (HEARD_DARUMA, HEARD_TALK, HEARD_FOLLOW_UP, HEARD_UNKNOWN)


class JourneyRecognizer(ScriptedRecognizer):
    """台本どおりに返す偽の聞き取り。`hold` が立つと、取り消されるまで録音を続ける。"""

    def __init__(self, script: Sequence[str]) -> None:
        super().__init__(script)
        self.hold = threading.Event()
        self.listening = threading.Event()
        self.cancelled = threading.Event()

    def listen(
        self,
        seconds: float,
        *,
        hints: Sequence[str],
        should_continue: Callable[[], bool],
        on_recording: Callable[[bool], None],
    ) -> str:
        if not self.hold.is_set():
            return super().listen(
                seconds, hints=hints, should_continue=should_continue, on_recording=on_recording
            )
        self.calls.append(ListenCall(seconds, tuple(hints)))
        on_recording(True)
        self.listening.set()
        try:
            deadline = time.monotonic() + WAIT_S
            while time.monotonic() < deadline:
                if not should_continue():
                    self.cancelled.set()
                    raise ListenCancelled
                time.sleep(0.01)
        finally:
            on_recording(False)
        return DARUMA_PHRASES[0]  # 取り消されなかった（このときは daruma に届いて失敗になる）


class Spoken:
    """本物の声の口（VoiceService.speak）を包み、話した文章を順に残す。"""

    def __init__(self, bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
        self.texts: list[str] = []
        original = bench.voice.speak

        def speak(text: str, **kwargs: Any) -> bool:
            self.texts.append(text)
            return original(text, **kwargs)

        monkeypatch.setattr(bench.voice, "speak", speak)


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    api._reset_for_tests()
    yield
    api._reset_for_tests()


@pytest.fixture
def bench(xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock) -> Iterator[Bench]:
    kit = Bench(fakes, clock)
    kit.registry = api.registry()  # ほかの機能と同じ、プロセスの中の登録表を使う
    yield kit
    kit.close()


def _advance_to(bench: Bench, when: datetime) -> None:
    """偽の時計を `when` まで進める（単調な時刻も一緒に進む）。"""
    seconds = (when - bench.clock.now()).total_seconds()
    assert seconds > 0
    bench.clock.advance(seconds)


def test_1日の流れを通しても_言葉は届く先にだけ届き_ログには出ない(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="osekkai")
    spoken = Spoken(bench, monkeypatch)
    stop = threading.Event()

    # 登録の口から、語句の機能と受け手を登録する
    daruma: list[Heard] = []
    api.register_words(DARUMA, 0, DARUMA_PHRASES, daruma.append, alive=lambda: not stop.is_set())
    talk: list[Heard] = []

    def on_talk(heard: Heard) -> None:
        talk.append(heard)
        if not heard.follow_up:
            api.listen_again(TALK)  # 言葉を受けて、続けて聞くを求める

    talk_registration = api.register_fallback(TALK, on_talk, alive=lambda: not stop.is_set())

    recognizer = JourneyRecognizer(
        [HEARD_DARUMA, HEARD_TALK, HEARD_FOLLOW_UP, HEARD_UNKNOWN, HEARD_NOTHING]
    )
    bench.start(recognizer)
    feature = bench.feature
    assert feature is not None
    assert wait_for(lambda: recognizer.state is RecognizerState.READY)

    # 1) 語句に一致する言葉は、登録した機能へ（2.2）
    bench.press_and_wait()

    assert [(h.text, h.follow_up) for h in daruma] == [(HEARD_DARUMA, False)]
    assert talk == []
    assert spoken.texts == [SAY_CALL]
    assert (0, row(LCD_LISTENING)) in bench.rows()
    assert recognizer.calls[0].hints == DARUMA_PHRASES  # 登録の語句をヒントに渡す
    assert bench.arbiter.holder(Resource.VOICE) is None

    # 2) 当てはまらない言葉は受け手へ（3.3）。受け手が続けて聞くを求め、
    #    締め切りの前の押下は「うん、なあに？」と聞いて、振り分けずに受け手へ（4.3）
    bench.press_and_wait()

    assert [(h.text, h.follow_up) for h in talk] == [(HEARD_TALK, False)]
    assert wait_for(lambda: feature.follow_up_owner == TALK), "続けて聞くの待ちに入っていません"
    bench.clock.advance(20.0)  # 締め切り（30 秒）の前
    bench.press_and_wait()

    assert [(h.text, h.follow_up) for h in talk] == [(HEARD_TALK, False), (HEARD_FOLLOW_UP, True)]
    assert len(daruma) == 1
    assert spoken.texts[-2:] == [SAY_CALL, SAY_FOLLOW_UP]
    assert feature.follow_up_owner is None

    # 3) 受け手が外れた後の、当てはまらない言葉は「わからない」（3.4）
    talk_registration.remove()
    bench.press_and_wait()

    assert spoken.texts[-2:] == [SAY_CALL, SAY_UNKNOWN]
    assert len(daruma) == 1
    assert len(talk) == 2

    # 4) 空の言葉は「きこえなかった」で、どこにも渡さない（6.1）
    bench.press_and_wait()

    assert spoken.texts[-2:] == [SAY_CALL, SAY_NOT_HEARD]
    assert len(daruma) == 1
    assert len(talk) == 2
    assert recognizer.remaining == 0
    assert len(recognizer.calls) == 5

    # 5) 時間帯（7:00〜21:30）の外は、聞かずに「おやすみ」（5.2）
    _advance_to(bench, datetime(2026, 1, 1, 21, 30, tzinfo=JST))
    before_rows = len(bench.rows())
    bench.press_and_wait()

    assert spoken.texts[-1] == SAY_SLEEP_TIME
    rows = bench.rows()[before_rows:]
    assert (0, row(LCD_SLEEP_TIME)) in rows
    assert (1, row(Face.SLEEP.text())) in rows
    assert len(recognizer.calls) == 5

    # 6) 次の朝。おやすみモードの間の押下では、声も画面も LED も出さず、聞かない（5.3）
    _advance_to(bench, datetime(2026, 1, 2, 8, 0, tzinfo=JST))
    bench.quiet.request_quiet()
    said = len(spoken.texts)
    played = len(bench.fakes.player.played)
    displays = len(bench.fakes.display.calls)
    leds = len(bench.fakes.led.calls)
    bench.press_and_wait()

    assert len(spoken.texts) == said
    assert len(bench.fakes.player.played) == played
    assert len(bench.fakes.display.calls) == displays
    assert len(bench.fakes.led.calls) == leds
    assert len(recognizer.calls) == 5

    # 7) おやすみモードを終えて押し、聞いている間に日課（優先度の高い機能）が資源を求めると、
    #    聞き取りを取り消して資源を返し、どこにも渡さず、何も話さない（1.7）
    bench.quiet.end_quiet()
    recognizer.hold.set()
    bench.press()
    assert recognizer.listening.wait(WAIT_S)
    assert len(spoken.texts) == said + 1  # 「なあに？」まで
    assert spoken.texts[-1] == SAY_CALL

    lease = bench.arbiter.acquire(
        "routine", Priority.SCHEDULED + 5, {Resource.VOICE, Resource.DISPLAY}
    )
    lease.release()

    assert recognizer.cancelled.is_set()
    assert bench.wait_idle()
    assert len(daruma) == 1
    assert len(talk) == 2
    assert len(spoken.texts) == said + 1  # 「きこえなかった」なども話さない
    assert not bench.fakes.led.on
    assert bench.arbiter.holder(Resource.VOICE) is None

    # 通しのあいだのログに、聞き取った言葉と登録の語句が出ていない（7.3〜7.5）
    records = [r for r in caplog.records if r.name.startswith("osekkai")]
    messages = [r.getMessage() for r in records]
    assert any("振り分けました" in m and DARUMA in m for m in messages)  # 届いた先の名前は出す
    assert any("振り分けました" in m and TALK in m for m in messages)
    hidden_values = {
        *HEARD_TEXTS,
        *(normalize(text) for text in HEARD_TEXTS),
        *DARUMA_PHRASES,
    }
    for record, message in zip(records, messages, strict=True):
        shown = message + " " + repr(record.args)
        for hidden in hidden_values:
            assert hidden not in shown, f"ログに言葉が出ています（{record.name}）"
