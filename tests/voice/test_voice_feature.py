"""入口の機能の本体（1.6、1.7、4.2、4.4、4.5、5.2〜5.5、6.3〜6.5、8.5）。

本物の調停役・ボタンの振り分け・静かな状態を偽のデバイスで組み、偽の聞き取りで動かす。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

import pytest
from voice_bench import JOB_POLL_S, PHRASES, TEXT, WAIT_S, Bench
from voice_bench import lcd_row as _row
from voice_bench import wait_for as _wait_for
from voice_fakes import ScriptedRecognizer

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import JST, FakeClock
from osekkai.core.config import ConfigReport, CoreConfig
from osekkai.core.feature import validate_spec
from osekkai.core.paths import Paths
from osekkai.core.registry import FeatureRegistry
from osekkai.core.types import ButtonPress, Face, Priority, Resource
from osekkai.core.voice import split_lines
from osekkai.voice import VoiceGatewayFeature
from osekkai.voice import feature as feature_module
from osekkai.voice.adapters import typed as typed_module
from osekkai.voice.adapters.typed import TypedRecognizer
from osekkai.voice.adapters.whisper import WhisperRecognizer
from osekkai.voice.config import VoiceConfig
from osekkai.voice.intents import Heard
from osekkai.voice.recognizer import ListenCancelled, Recognizer, RecognizerState
from osekkai.voice.texts import (
    LCD_LISTENING,
    LCD_SLEEP_TIME,
    SAY_CALL,
    SAY_FOLLOW_UP,
    SAY_NOT_HEARD,
    SAY_PREPARING,
    SAY_SLEEP_TIME,
    SAY_UNAVAILABLE,
)

RESOURCES = frozenset(
    {Resource.VOICE, Resource.DISPLAY, Resource.LED, Resource.BUTTON, Resource.NECK}
)


class BlockingRecognizer:
    """使える聞き取り。`finish` が立つか、続けてよくなくなるまで録音を続ける。"""

    def __init__(self, text: str = TEXT) -> None:
        self.text = text
        self.started = threading.Event()
        self.finish = threading.Event()
        self.cancelled = threading.Event()
        self.calls = 0

    @property
    def state(self) -> RecognizerState:
        return RecognizerState.READY

    def prepare(self) -> None:
        pass

    def listen(
        self,
        seconds: float,
        *,
        hints: Sequence[str],
        should_continue: Callable[[], bool],
        on_recording: Callable[[bool], None],
    ) -> str:
        self.calls += 1
        on_recording(True)
        self.started.set()
        try:
            while not self.finish.wait(0.01):
                if not should_continue():
                    self.cancelled.set()
                    raise ListenCancelled
        finally:
            on_recording(False)
        return self.text


class RaisingOnceRecognizer(ScriptedRecognizer):
    """初めて状態を読まれたときだけ例外を出す（1 回の仕事の例外）。"""

    def __init__(self) -> None:
        super().__init__([], prepare_gate=threading.Event(), prepare_timeout=WAIT_S)
        self.raised = False

    @property
    def state(self) -> RecognizerState:
        if not self.raised:
            self.raised = True
            raise RuntimeError("壊れた")
        return super().state


@pytest.fixture
def bench(xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock) -> Iterator[Bench]:
    kit = Bench(fakes, clock)
    yield kit
    kit.close()


def _said(text: str) -> list[str]:
    return split_lines(text)


def _ready() -> ScriptedRecognizer:
    return ScriptedRecognizer([TEXT, TEXT, TEXT])


def _contains(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


# --- 申告 ---


def test_申告は名前_優先度_資源_設定のモデルを持ち_基盤の確かめを通る() -> None:
    spec = VoiceGatewayFeature.spec
    assert spec.name == "voice"
    assert spec.priority == Priority.INTERACTIVE + 5
    assert spec.resources == RESOURCES
    assert spec.config_model is VoiceConfig
    assert validate_spec(spec) == []


def test_無効の設定なら_基盤は入口を開始しない() -> None:
    registry = FeatureRegistry([VoiceGatewayFeature])
    disabled = ConfigReport(core=CoreConfig(), features={"voice": VoiceConfig(enabled=False)})
    enabled = ConfigReport(core=CoreConfig(), features={"voice": VoiceConfig()})

    assert list(registry.plan(disabled)) == []
    assert ("voice", "無効") in registry.skipped()
    assert [p.feature_cls for p in registry.plan(enabled)] == [VoiceGatewayFeature]


# --- 押下の判定 ---


def test_使えるときの押下で_聞いて登録した機能へ渡す(bench: Bench) -> None:
    heard = bench.words("daruma")
    recognizer = _ready()
    bench.start(recognizer)
    assert _wait_for(lambda: recognizer.state is RecognizerState.READY)

    bench.press_and_wait()

    assert [h.text for h in heard] == [TEXT]
    assert len(recognizer.calls) == 1
    assert bench.sayings() == _said(SAY_CALL)
    assert (0, _row(LCD_LISTENING)) in bench.rows()
    assert bench.arbiter.holder(Resource.VOICE) is None  # 資源を返した


def test_静かな状態の押下では_何もしない(bench: Bench) -> None:
    recognizer = _ready()
    bench.start(recognizer)
    assert _wait_for(lambda: recognizer.state is RecognizerState.READY)
    bench.quiet.request_quiet()
    before_display = len(bench.fakes.display.calls)
    before_led = len(bench.fakes.led.calls)
    before_neck = len(bench.fakes.servos.calls)

    bench.press_and_wait()

    assert bench.sayings() == []
    assert recognizer.calls == []
    assert len(bench.fakes.display.calls) == before_display
    assert len(bench.fakes.led.calls) == before_led
    assert len(bench.fakes.servos.calls) == before_neck


def _assert_sleep_reply(bench: Bench, recognizer: ScriptedRecognizer) -> None:
    assert bench.sayings() == _said(SAY_SLEEP_TIME)
    rows = bench.rows()
    assert (0, _row(LCD_SLEEP_TIME)) in rows
    assert (1, _row(Face.SLEEP.text())) in rows
    assert recognizer.calls == []
    assert bench.arbiter.holder(Resource.VOICE) is None


def test_時間帯の外の押下では_おやすみの表示と言葉にする(bench: Bench) -> None:
    recognizer = _ready()
    # 偽の時計は 9:00。帯は 10:00〜21:30
    bench.start(recognizer, VoiceConfig(hours=(dtime(10, 0), dtime(21, 30))))
    assert _wait_for(lambda: recognizer.state is RecognizerState.READY)

    bench.press_and_wait()

    _assert_sleep_reply(bench, recognizer)


def test_時刻が確かでない間の押下は_時間帯の外として扱う(bench: Bench) -> None:
    bench.clock.set_synchronized(False)
    recognizer = _ready()
    bench.start(recognizer)
    assert _wait_for(lambda: recognizer.state is RecognizerState.READY)

    bench.press_and_wait()

    _assert_sleep_reply(bench, recognizer)


def test_時間帯の終わりちょうどは外で_始めちょうどは中(bench: Bench) -> None:
    recognizer = _ready()
    # 偽の時計は 9:00 から（押すたびに 1 秒進む）
    bench.make(recognizer, VoiceConfig(hours=(dtime(7, 0), dtime(9, 0))))
    feature = bench.feature
    assert feature is not None
    assert not feature.within_listening_hours()

    bench.make(recognizer, VoiceConfig(hours=(dtime(9, 0), dtime(10, 0))))
    feature = bench.feature
    assert feature is not None
    assert feature.within_listening_hours()


@pytest.mark.parametrize(
    ("hour", "inside"),
    [(23, True), (2, True), (7, False), (12, False), (22, True), (21, False)],
)
def test_日をまたぐ時間帯も扱う(bench: Bench, hour: int, inside: bool) -> None:
    bench.clock.set_now(datetime(2026, 1, 1, hour, 0, tzinfo=JST))
    bench.make(_ready(), VoiceConfig(hours=(dtime(22, 0), dtime(7, 0))))
    feature = bench.feature
    assert feature is not None
    assert feature.within_listening_hours() is inside


def test_準備の間の押下には_じゅんびと答え_準備が終われば聞く(bench: Bench) -> None:
    gate = threading.Event()
    recognizer = ScriptedRecognizer([""], prepare_gate=gate, prepare_timeout=WAIT_S)
    bench.start(recognizer)
    assert recognizer.prepare_started.wait(WAIT_S)
    # 準備は別の短命のスレッドで動き、入口のスレッドは押下を受ける（6.5）
    assert any(t.name == "voice-prepare" for t in threading.enumerate())

    bench.press_and_wait()

    assert bench.sayings() == _said(SAY_PREPARING)
    assert recognizer.calls == []

    gate.set()
    assert recognizer.prepared.wait(WAIT_S)
    bench.press_and_wait()

    assert len(recognizer.calls) == 1
    assert _contains(bench.sayings(), _said(SAY_CALL) + _said(SAY_NOT_HEARD))
    assert recognizer.prepare_calls == 1


def test_使えない聞き取りでの押下には_おはなしできないと答える(bench: Bench) -> None:
    recognizer = ScriptedRecognizer([], prepare_fails=True)
    bench.start(recognizer)
    assert recognizer.prepared.wait(WAIT_S)

    bench.press_and_wait()

    assert bench.sayings() == _said(SAY_UNAVAILABLE)
    assert recognizer.calls == []
    assert bench.arbiter.holder(Resource.VOICE) is None


# --- 聞いている間 ---


def test_聞いている間の押下は捨てる(bench: Bench) -> None:
    heard = bench.words("daruma")
    recognizer = BlockingRecognizer()
    bench.start(recognizer)
    bench.press()
    assert recognizer.started.wait(WAIT_S)

    bench.press()  # 聞いている間
    bench.press()
    recognizer.finish.set()
    assert bench.wait_idle()
    time.sleep(0.3)  # 捨てていなければ、次の仕事が始まる

    assert recognizer.calls == 1
    assert [h.text for h in heard] == [TEXT]
    feature = bench.feature
    assert feature is not None
    assert not feature.busy


def test_日課が資源を求めると_聞き取りを取り消してすぐに返し_どこにも渡さない(
    bench: Bench,
) -> None:
    heard = bench.words("daruma")
    recognizer = BlockingRecognizer()
    bench.start(recognizer)
    bench.press()
    assert recognizer.started.wait(WAIT_S)
    before = list(bench.sayings())

    started = time.monotonic()
    lease = bench.arbiter.acquire(
        "routine", Priority.SCHEDULED + 5, {Resource.VOICE, Resource.DISPLAY}
    )
    elapsed = time.monotonic() - started
    lease.release()

    assert recognizer.cancelled.is_set()
    assert elapsed < 1.5  # 取り上げの猶予（2 秒）を待たずに返した
    assert bench.wait_idle()
    assert heard == []
    assert bench.sayings() == before  # 「きこえなかった」なども話さない
    assert not bench.fakes.led.on


def test_ほかの機能が資源を使っていれば_押下を捨てる(bench: Bench) -> None:
    recognizer = _ready()
    bench.start(recognizer)
    assert _wait_for(lambda: recognizer.state is RecognizerState.READY)
    lease = bench.arbiter.acquire("routine", Priority.SCHEDULED + 5, {Resource.VOICE})
    try:
        bench.press()
        assert bench.wait_idle()
    finally:
        lease.release()

    assert recognizer.calls == []
    assert bench.sayings() == []


def test_資源を待つ間に静かな状態になれば_借りた後も何もしない(bench: Bench) -> None:
    recognizer = _ready()
    bench.start(recognizer, acquire_wait=WAIT_S)
    assert _wait_for(lambda: recognizer.state is RecognizerState.READY)
    # ボタンは持たない（持ち主がいると押下はその持ち主に届く）。
    # 首も持たない（返すときに首を正面に戻す操作が入らないように）
    holder = bench.arbiter.acquire(
        "routine", Priority.SCHEDULED + 5, {Resource.VOICE, Resource.DISPLAY, Resource.LED}
    )
    bench.press()
    time.sleep(0.2)  # 入口は資源を待っている
    feature = bench.feature
    assert feature is not None
    assert feature.busy
    bench.quiet.request_quiet()
    before_led = len(bench.fakes.led.calls)
    before_neck = len(bench.fakes.servos.calls)
    holder.release()
    assert bench.wait_idle()

    assert recognizer.calls == []
    assert bench.sayings() == []
    rows = bench.rows()
    assert (0, _row(LCD_LISTENING)) not in rows
    assert (0, _row(LCD_SLEEP_TIME)) not in rows
    assert len(bench.fakes.led.calls) == before_led
    # 首はうなずかない（基盤がリースを返すときに正面に戻すだけ）
    neck = bench.fakes.servos.calls[before_neck:]
    assert all(c.op == "set_angle" and c.args[1] == 0.0 for c in neck)


def test_止める旗で_聞いている途中でもすぐに戻る(bench: Bench) -> None:
    recognizer = BlockingRecognizer()
    bench.start(recognizer)
    bench.press()
    assert recognizer.started.wait(WAIT_S)

    bench.stop.set()
    thread = bench.thread
    assert thread is not None
    thread.join(2.5)

    assert not thread.is_alive()
    assert recognizer.cancelled.is_set()
    assert bench.arbiter.holder(Resource.VOICE) is None


def test_止める旗ですぐに戻り_待ち手を外す(bench: Bench) -> None:
    bench.start(_ready())
    bench.stop.set()
    thread = bench.thread
    assert thread is not None
    thread.join(2.5)

    assert not thread.is_alive()
    assert bench.router._waiters == []


def test_1回の仕事の例外は型の名前だけを記録して続ける(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    recognizer = RaisingOnceRecognizer()
    bench.start(recognizer)
    assert recognizer.prepare_started.wait(WAIT_S)

    bench.press_and_wait()
    bench.press_and_wait()

    thread = bench.thread
    assert thread is not None
    assert thread.is_alive()
    assert bench.sayings() == _said(SAY_PREPARING)
    assert "RuntimeError" in caplog.text
    assert "壊れた" not in caplog.text


def test_ログに言葉と表示の文字を出さない(bench: Bench, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    bench.words("daruma")
    recognizer = _ready()
    bench.start(recognizer, VoiceConfig(hours=(dtime(7, 0), dtime(9, 1))))
    assert _wait_for(lambda: recognizer.state is RecognizerState.READY)

    bench.press_and_wait()  # 9:00:01 は中
    bench.clock.advance(120)
    bench.press_and_wait()  # 時間帯の外

    for text in (TEXT, *PHRASES, SAY_CALL, SAY_SLEEP_TIME, LCD_SLEEP_TIME, LCD_LISTENING):
        assert text not in caplog.text


# --- 続けて聞く ---

TALK = "talk"
TALK_PHRASES = ("おはなし",)


class Talk:
    """続けて聞くを求める機能の代わり。生きているかを切り替えられる。"""

    def __init__(self, bench: Bench, *, again: bool = False) -> None:
        self.heard: list[Heard] = []
        self.alive = True
        self._bench = bench
        self._again = again
        bench.registry.add_words(TALK, 0, TALK_PHRASES, self._on_heard, alive=lambda: self.alive)

    def _on_heard(self, heard: Heard) -> None:
        self.heard.append(heard)
        if self._again:
            self._bench.registry.request_follow_up(TALK)


def _start_follow_up(bench: Bench, recognizer: Recognizer | None = None) -> Talk:
    """入口を動かし、TALK が続けて聞くを求め、入口が待ちに入るまで進める。"""
    talk = Talk(bench)
    recognizer = recognizer or _ready()
    bench.start(recognizer)
    assert _wait_for(lambda: recognizer.state is RecognizerState.READY)
    bench.registry.request_follow_up(TALK)
    feature = bench.feature
    assert feature is not None
    assert _wait_for(lambda: feature.follow_up_owner == TALK), "待ちに入っていません"
    return talk


def _follow_up_ended(bench: Bench) -> bool:
    feature = bench.feature
    assert feature is not None
    return _wait_for(lambda: feature.follow_up_owner is None)


def test_続けて聞くの待ちの間の押下で_うんなあにと聞き_振り分けずに同じ機能へ渡す(
    bench: Bench,
) -> None:
    daruma = bench.words("daruma")  # TEXT は daruma の語句に一致するが、使わない
    recognizer = _ready()
    talk = _start_follow_up(bench, recognizer)
    before_neck = len(bench.fakes.servos.calls)

    bench.clock.advance(28.0)  # 押すときに 1 秒進むので、締め切りの 1 秒前
    bench.press_and_wait()

    assert [(h.text, h.follow_up) for h in talk.heard] == [(TEXT, True)]
    assert daruma == []
    assert bench.sayings() == _said(SAY_FOLLOW_UP)
    assert (0, _row(LCD_LISTENING)) in bench.rows()
    # うなずかない（基盤がリースを返すときに首を正面に戻すだけ）
    neck = bench.fakes.servos.calls[before_neck:]
    assert all(c.op == "set_angle" and c.args[1] == 0.0 for c in neck)
    feature = bench.feature
    assert feature is not None
    assert feature.follow_up_owner is None  # 1 回で使い切る

    bench.press_and_wait()  # 次は初めての聞き取りに戻る

    assert [h.text for h in daruma] == [TEXT]
    assert len(talk.heard) == 1


def test_締め切りを過ぎた押下は_初めての聞き取りとして振り分ける(bench: Bench) -> None:
    daruma = bench.words("daruma")
    talk = _start_follow_up(bench)

    bench.clock.advance(30.0)
    assert _follow_up_ended(bench)
    assert bench.sayings() == []  # 待ちの終わりには何も話さない
    bench.press_and_wait()

    assert talk.heard == []
    assert [(h.text, h.follow_up) for h in daruma] == [(TEXT, False)]
    assert bench.sayings() == _said(SAY_CALL)


def test_締め切りの確かめの前でも_締め切りを過ぎた押下は続けて聞かない(bench: Bench) -> None:
    daruma = bench.words("daruma")
    recognizer = _ready()
    talk = Talk(bench)
    bench.make(recognizer)
    feature = bench.feature
    assert feature is not None
    recognizer.prepare()
    bench.registry.request_follow_up(TALK)
    stop = threading.Event()
    feature._between_jobs(stop)  # 待ちに入る
    assert feature.follow_up_owner == TALK

    bench.clock.advance(31.0)
    # 入口のスレッドが締め切りを確かめる前に押下が届く（押下を受けるのはボタンのスレッド）
    feature._on_press(ButtonPress(at=bench.clock.monotonic(), source="usr"))
    bench.thread = threading.Thread(target=feature.run, args=(bench.stop,), daemon=True)
    bench.thread.start()
    assert bench.wait_idle()

    assert talk.heard == []
    assert [h.text for h in daruma] == [TEXT]
    assert bench.sayings() == _said(SAY_CALL)


def test_求めた直後の押下も_確かめを待たずに続けて聞く(bench: Bench) -> None:
    daruma = bench.words("daruma")
    recognizer = _ready()
    talk = Talk(bench)
    bench.make(recognizer)
    feature = bench.feature
    assert feature is not None
    recognizer.prepare()

    bench.registry.request_follow_up(TALK)
    # 入口のスレッドが要求を取り出す前に押下が届く
    feature._on_press(ButtonPress(at=bench.clock.monotonic(), source="usr"))
    bench.thread = threading.Thread(target=feature.run, args=(bench.stop,), daemon=True)
    bench.thread.start()
    assert bench.wait_idle()

    assert [(h.text, h.follow_up) for h in talk.heard] == [(TEXT, True)]
    assert daruma == []
    assert bench.sayings() == _said(SAY_FOLLOW_UP)
    assert feature.follow_up_owner is None  # この押下で使い切った


def test_機能が言葉を受けてまた続けて聞くを求めると_続けて話せる(bench: Bench) -> None:
    daruma = bench.words("daruma")
    talk = Talk(bench, again=True)
    recognizer = _ready()
    bench.start(recognizer)
    assert _wait_for(lambda: recognizer.state is RecognizerState.READY)
    bench.registry.request_follow_up(TALK)
    feature = bench.feature
    assert feature is not None
    assert _wait_for(lambda: feature.follow_up_owner == TALK)

    bench.press_and_wait()
    assert _wait_for(lambda: feature.follow_up_owner == TALK)  # 新しい締め切りで待つ
    bench.press_and_wait()

    assert [(h.text, h.follow_up) for h in talk.heard] == [(TEXT, True), (TEXT, True)]
    assert daruma == []
    assert bench.sayings() == _said(SAY_FOLLOW_UP)  # 2 回目は声の控えから流す
    assert len(bench.fakes.player.played) == 2


def test_待ちの間にもう一度求めると_締め切りを置き直す(bench: Bench) -> None:
    talk = _start_follow_up(bench)

    bench.clock.advance(20.0)
    feature = bench.feature
    assert feature is not None
    bench.registry.request_follow_up(TALK)
    # 入口が要求を取り出して、締め切りを置き直す
    assert _wait_for(lambda: bench.registry._follow_up is None)
    bench.clock.advance(20.0)  # 初めの締め切り（30 秒）は過ぎた
    time.sleep(0.2)
    assert feature.follow_up_owner == TALK
    bench.press_and_wait()

    assert [(h.text, h.follow_up) for h in talk.heard] == [(TEXT, True)]
    assert bench.sayings() == _said(SAY_FOLLOW_UP)


def test_おやすみモードになると_黙って待ちを終える(bench: Bench) -> None:
    daruma = bench.words("daruma")
    talk = _start_follow_up(bench)

    bench.quiet.request_quiet()
    assert _follow_up_ended(bench)
    bench.quiet.end_quiet()
    assert bench.sayings() == []
    bench.press_and_wait()

    assert talk.heard == []
    assert [h.text for h in daruma] == [TEXT]
    assert bench.sayings() == _said(SAY_CALL)


def test_時間帯が終わると_黙って待ちを終える(bench: Bench) -> None:
    daruma = bench.words("daruma")
    talk = _start_follow_up(bench)

    bench.clock.set_now(datetime(2026, 1, 1, 22, 0, tzinfo=JST))  # 帯は 7:00〜21:30
    assert _follow_up_ended(bench)
    assert bench.sayings() == []
    bench.clock.set_now(datetime(2026, 1, 1, 9, 0, 5, tzinfo=JST))
    bench.press_and_wait()

    assert talk.heard == []
    assert [h.text for h in daruma] == [TEXT]
    assert bench.sayings() == _said(SAY_CALL)


def test_時刻が確かでなくなると_黙って待ちを終える(bench: Bench) -> None:
    talk = _start_follow_up(bench)

    bench.clock.set_synchronized(False)
    assert _follow_up_ended(bench)
    assert bench.sayings() == []
    assert talk.heard == []


def test_求めた機能が止まると_黙って待ちを終える(bench: Bench) -> None:
    daruma = bench.words("daruma")
    talk = _start_follow_up(bench)

    talk.alive = False
    assert _follow_up_ended(bench)
    assert bench.sayings() == []
    talk.alive = True
    bench.press_and_wait()

    assert talk.heard == []
    assert [h.text for h in daruma] == [TEXT]
    assert bench.sayings() == _said(SAY_CALL)


def test_続けて聞くのログに言葉と機能の語句を出さない(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _start_follow_up(bench)
    bench.press_and_wait()
    bench.registry.request_follow_up(TALK)
    bench.clock.advance(31.0)
    assert _follow_up_ended(bench)

    for text in (TEXT, *TALK_PHRASES, *PHRASES, SAY_FOLLOW_UP, LCD_LISTENING):
        assert text not in caplog.text


# --- 聞き取りの選び方 ---


def _choose(
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    *,
    whisper: bool,
    typed: bool,
) -> Recognizer:
    monkeypatch.setattr(feature_module, "whisper_available", lambda: whisper)
    monkeypatch.setattr(typed_module, "typed_input_active", lambda: typed)
    feature = VoiceGatewayFeature(
        bench.context(),
        VoiceConfig.model_validate({"recognizer": kind}),
        registry=bench.registry,
        job_poll=JOB_POLL_S,
    )
    return feature.recognizer


def test_whisper_の設定では実機の聞き取りを_データの置き場所のモデルで使う(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    recognizer = _choose(bench, monkeypatch, "whisper", whisper=False, typed=True)
    assert isinstance(recognizer, WhisperRecognizer)
    assert feature_module.model_dir(Paths(), VoiceConfig(model="small")) == (
        Paths().data_dir / "models" / "faster-whisper-small"
    )


def test_typed_の設定では打った文字の聞き取りを使う(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    recognizer = _choose(bench, monkeypatch, "typed", whisper=True, typed=True)
    assert isinstance(recognizer, TypedRecognizer)


def test_auto_では_faster_whisper_を読み込めれば実機の聞き取り(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    recognizer = _choose(bench, monkeypatch, "auto", whisper=True, typed=True)
    assert isinstance(recognizer, WhisperRecognizer)


def test_auto_で読み込めず端末の偽物が動いていれば_打った文字の聞き取り(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    recognizer = _choose(bench, monkeypatch, "auto", whisper=False, typed=True)
    assert isinstance(recognizer, TypedRecognizer)
    assert recognizer.state is RecognizerState.READY


def test_auto_でどちらもなければ_押下におはなしできないと答える(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    recognizer = _choose(bench, monkeypatch, "auto", whisper=False, typed=False)
    assert recognizer.state is RecognizerState.UNAVAILABLE

    bench.start(None)
    bench.press_and_wait()

    assert bench.sayings() == _said(SAY_UNAVAILABLE)
