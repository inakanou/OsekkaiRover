"""押下から振り分けまでの流れ（1.1、1.2、1.4、1.5、1.7、2.5、3.3、3.4、4.3、6.1、6.2、7.3〜7.5）。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from voice_fakes import Cancel, Event, Fail, Outcome, ScriptedGatewayIO, ScriptedRecognizer

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Arbiter
from osekkai.core.cache import CacheManager
from osekkai.core.config import MotionSettings, VoiceSettings
from osekkai.core.display import DisplayService, LedService
from osekkai.core.lcd_text import center, to_lcd_codes
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.types import Face, Priority, Resource
from osekkai.core.voice import VoiceService
from osekkai.voice.flow import GatewayIO, LeaseGatewayIO, ListenFlow, ListenOutcome, OutcomeKind
from osekkai.voice.intents import Heard, IntentRegistry
from osekkai.voice.texts import (
    LCD_LISTENING,
    LCD_SPEAK,
    LCD_THINKING,
    SAY_CALL,
    SAY_FOLLOW_UP,
    SAY_NOT_HEARD,
    SAY_UNKNOWN,
)

LOGGER = "test.voice.flow"
TEXT = "だるまさん しよう"
PHRASES = ("だるまさん", "だるまさんがころんだ")
OTHER_PHRASES = ("おえかき",)
SECONDS = 5.0

LISTEN_START: list[Event] = [
    ("nod",),
    ("show", LCD_LISTENING, Face.CALL.text()),
    ("say", SAY_CALL),
    ("led", True),
    ("show", LCD_SPEAK, Face.CALL.text()),
    ("led", False),
    ("show", LCD_THINKING, Face.NORMAL.text()),
]


class _Bench:
    """流れを 1 回動かすための一式。資源を返すことと口を呼ぶことを、入出力と同じ並びに残す。"""

    def __init__(
        self,
        script: Sequence[Outcome],
        *,
        say_fails: bool = False,
        revoke_on: tuple[str, int] | None = None,
        record_seconds: float = 0.0,
        transcribe_seconds: float = 0.0,
    ) -> None:
        self.clock = FakeClock()
        self.events: list[Event] = []
        self.io = ScriptedGatewayIO(self.events, say_fails=say_fails, revoke_on=revoke_on)
        self.recognizer = ScriptedRecognizer(
            script,
            clock=self.clock,
            record_seconds=record_seconds,
            transcribe_seconds=transcribe_seconds,
        )
        self.registry = IntentRegistry()
        self.heard: dict[str, list[Heard]] = {}
        self.cancel = False
        self.releases = 0
        self.flow = ListenFlow(
            self.recognizer, self.registry, self.clock, logging.getLogger(LOGGER)
        )

    def handler(self, owner: str, *, fails: bool = False) -> Callable[[Heard], None]:
        def on_heard(heard: Heard) -> None:
            self.events.append(("handler", owner, heard))
            self.heard.setdefault(owner, []).append(heard)
            if fails:
                raise RuntimeError(TEXT)

        return on_heard

    def words(self, owner: str, phrases: Sequence[str], *, alive: bool = True) -> None:
        self.registry.add_words(owner, 0, phrases, self.handler(owner), alive=lambda: alive)

    def release(self) -> None:
        self.releases += 1
        self.events.append(("release",))

    def run(self, *, follow_up_owner: str | None = None) -> ListenOutcome:
        return self.flow.run(
            self.io,
            follow_up_owner=follow_up_owner,
            seconds=SECONDS,
            cancelled=lambda: self.cancel,
            release=self.release,
        )


def _no_words_in_log(caplog: pytest.LogCaptureFixture) -> None:
    text = caplog.text
    for hidden in (TEXT, *PHRASES, *OTHER_PHRASES, SAY_CALL, SAY_NOT_HEARD, SAY_UNKNOWN):
        assert hidden not in text
    assert LCD_LISTENING not in text
    assert LCD_SPEAK not in text
    assert LCD_THINKING not in text


@pytest.fixture(autouse=True)
def _capture(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)


def test_初めての聞き取りは_うなずいて呼びかけ_録音中にLEDを点け_資源を返してから登録の機能へ渡す(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bench = _Bench([TEXT])
    bench.words("daruma", PHRASES)

    outcome = bench.run()

    assert outcome == ListenOutcome(OutcomeKind.ROUTED, "daruma")
    assert bench.events == [
        *LISTEN_START,
        ("release",),
        ("handler", "daruma", Heard(TEXT, follow_up=False)),
    ]
    assert bench.releases == 1
    assert "聞き取りを始めました" in caplog.text
    assert "振り分けました: daruma" in caplog.text
    _no_words_in_log(caplog)


def test_録音の長さと登録の語句をヒントとして聞き取りに渡す() -> None:
    bench = _Bench([TEXT])
    bench.words("daruma", PHRASES)
    bench.words("oekaki", OTHER_PHRASES)

    bench.run()

    assert len(bench.recognizer.calls) == 1
    call = bench.recognizer.calls[0]
    assert call.seconds == SECONDS
    assert call.hints == bench.registry.hints() == (*PHRASES, *OTHER_PHRASES)


def test_当てはまらない言葉は_資源を返してから受け手に渡す() -> None:
    bench = _Bench(["きょうは はれ"])
    bench.words("daruma", PHRASES)
    bench.registry.set_fallback("talk", bench.handler("talk"), alive=lambda: True)

    outcome = bench.run()

    assert outcome == ListenOutcome(OutcomeKind.ROUTED, "talk")
    assert bench.events[-2:] == [
        ("release",),
        ("handler", "talk", Heard("きょうは はれ", follow_up=False)),
    ]
    assert "daruma" not in bench.heard


def test_どこにも当てはまらず受け手もなければ_わからないと話してから資源を返す() -> None:
    bench = _Bench(["きょうは はれ"])
    bench.words("daruma", PHRASES)

    outcome = bench.run()

    assert outcome == ListenOutcome(OutcomeKind.NOT_UNDERSTOOD)
    assert bench.events == [*LISTEN_START, ("say", SAY_UNKNOWN), ("release",)]
    assert bench.heard == {}


def test_空の言葉は_きこえなかったと話してから資源を返し_どこにも渡さない(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bench = _Bench([""])
    bench.words("daruma", PHRASES)
    bench.registry.set_fallback("talk", bench.handler("talk"), alive=lambda: True)

    outcome = bench.run()

    assert outcome == ListenOutcome(OutcomeKind.NOT_HEARD)
    assert bench.events == [*LISTEN_START, ("say", SAY_NOT_HEARD), ("release",)]
    assert bench.heard == {}
    assert "聞き取れませんでした" in caplog.text


def test_文字にするのに失敗したら_型の名前だけを記録し_きこえなかったと話す(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bench = _Bench([Fail("OSError")])
    bench.registry.set_fallback("talk", bench.handler("talk"), alive=lambda: True)

    outcome = bench.run()

    assert outcome == ListenOutcome(OutcomeKind.FAILED)
    assert bench.events == [*LISTEN_START, ("say", SAY_NOT_HEARD), ("release",)]
    assert bench.heard == {}
    assert "聞き取りに失敗しました: OSError" in caplog.text


def test_録音の途中で失敗しても_LEDを消して_きこえなかったと話す() -> None:
    bench = _Bench([Fail("CalledProcessError", while_recording=True)])

    outcome = bench.run()

    assert outcome == ListenOutcome(OutcomeKind.FAILED)
    assert bench.io.leds == [True, False]
    assert bench.io.sayings == [SAY_CALL, SAY_NOT_HEARD]
    assert bench.releases == 1


def test_取り消されたら_何も話さずに資源を返し_口を呼ばない(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bench = _Bench([Cancel()])
    bench.words("daruma", PHRASES)
    bench.registry.set_fallback("talk", bench.handler("talk"), alive=lambda: True)

    outcome = bench.run()

    assert outcome == ListenOutcome(OutcomeKind.DISCARDED)
    assert bench.io.sayings == [SAY_CALL]
    assert bench.io.leds == [True, False]
    assert bench.events[-1] == ("release",)
    assert bench.releases == 1
    assert bench.heard == {}
    _no_words_in_log(caplog)


def test_中断の旗が録音の間に立つと_聞き取りを取り消して捨てる() -> None:
    bench = _Bench([TEXT])
    bench.words("daruma", PHRASES)
    led = bench.io.led

    def led_then_preempted(on: bool) -> None:
        led(on)
        if on:
            bench.cancel = True  # 録音を始めたところで日課が資源を求めた

    bench.io.led = led_then_preempted  # type: ignore[method-assign]

    outcome = bench.run()

    assert outcome == ListenOutcome(OutcomeKind.DISCARDED)
    assert bench.heard == {}
    assert bench.releases == 1
    assert bench.io.sayings == [SAY_CALL]
    assert bench.io.leds == [True, False]
    assert (LCD_THINKING, Face.NORMAL.text()) not in bench.io.displays


def test_文字にし終えた後に中断の旗が立っても_言葉を捨てて口を呼ばない() -> None:
    bench = _Bench([TEXT])
    bench.words("daruma", PHRASES)
    listen = bench.recognizer.listen

    def listen_then_preempted(*args: object, **kwargs: object) -> str:
        text = listen(*args, **kwargs)  # type: ignore[arg-type]
        bench.cancel = True
        return text

    bench.recognizer.listen = listen_then_preempted  # type: ignore[method-assign]

    outcome = bench.run()

    assert outcome == ListenOutcome(OutcomeKind.DISCARDED)
    assert bench.heard == {}
    assert bench.io.sayings == [SAY_CALL]
    assert bench.releases == 1


def test_聞く前に資源を取り上げられたら_録音せずに捨てる() -> None:
    bench = _Bench([TEXT], revoke_on=("say", 1))
    bench.words("daruma", PHRASES)

    outcome = bench.run()

    assert outcome == ListenOutcome(OutcomeKind.DISCARDED)
    assert bench.recognizer.calls == []
    assert bench.heard == {}
    assert bench.events == [("nod",), ("show", LCD_LISTENING, Face.CALL.text()), ("release",)]


def test_録音の間にLEDの操作で取り上げられたら_取り消して捨てる() -> None:
    bench = _Bench([TEXT], revoke_on=("led", 1))
    bench.words("daruma", PHRASES)

    outcome = bench.run()

    assert outcome == ListenOutcome(OutcomeKind.DISCARDED)
    assert bench.heard == {}
    assert bench.releases == 1


def test_声を出せなくても_記録して表示だけで続ける(caplog: pytest.LogCaptureFixture) -> None:
    bench = _Bench([TEXT], say_fails=True)
    bench.words("daruma", PHRASES)

    outcome = bench.run()

    assert outcome == ListenOutcome(OutcomeKind.ROUTED, "daruma")
    assert bench.heard["daruma"] == [Heard(TEXT, follow_up=False)]
    assert "SpeechFailed" in caplog.text


def test_続けての聞き取りは_うなずかずに_うん_なあに_と聞き_振り分けずに同じ機能へ渡す(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bench = _Bench([TEXT])
    bench.words("daruma", PHRASES)  # 言葉はこちらに一致するが、振り分けない
    bench.words("quiz", OTHER_PHRASES)

    outcome = bench.run(follow_up_owner="quiz")

    assert outcome == ListenOutcome(OutcomeKind.ROUTED, "quiz")
    assert bench.events == [
        ("show", LCD_LISTENING, Face.CALL.text()),
        ("say", SAY_FOLLOW_UP),
        ("led", True),
        ("show", LCD_SPEAK, Face.CALL.text()),
        ("led", False),
        ("show", LCD_THINKING, Face.NORMAL.text()),
        ("release",),
        ("handler", "quiz", Heard(TEXT, follow_up=True)),
    ]
    assert "daruma" not in bench.heard
    assert "振り分けました: quiz" in caplog.text
    _no_words_in_log(caplog)


def test_続けて聞くを求めた機能が受け手なら_受け手の口へ渡す() -> None:
    bench = _Bench([TEXT])
    bench.words("daruma", PHRASES)
    bench.registry.set_fallback("talk", bench.handler("talk"), alive=lambda: True)

    outcome = bench.run(follow_up_owner="talk")

    assert outcome == ListenOutcome(OutcomeKind.ROUTED, "talk")
    assert bench.heard == {"talk": [Heard(TEXT, follow_up=True)]}


def test_続けて聞くを求めた機能の登録が生きていなければ_わからないと話して資源を返す() -> None:
    bench = _Bench([TEXT])
    bench.words("daruma", PHRASES)
    bench.words("quiz", OTHER_PHRASES, alive=False)

    outcome = bench.run(follow_up_owner="quiz")

    assert outcome == ListenOutcome(OutcomeKind.NOT_UNDERSTOOD)
    assert bench.events[-2:] == [("say", SAY_UNKNOWN), ("release",)]
    assert bench.heard == {}


def test_続けての聞き取りで空なら_きこえなかった() -> None:
    bench = _Bench([""])
    bench.words("quiz", OTHER_PHRASES)

    outcome = bench.run(follow_up_owner="quiz")

    assert outcome == ListenOutcome(OutcomeKind.NOT_HEARD)
    assert bench.io.sayings == [SAY_FOLLOW_UP, SAY_NOT_HEARD]
    assert bench.heard == {}


def test_渡した先の口が例外を出しても_機能の名前と型の名前だけを記録して続ける(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bench = _Bench([TEXT])
    bench.registry.add_words(
        "daruma", 0, PHRASES, bench.handler("daruma", fails=True), alive=lambda: True
    )

    outcome = bench.run()

    assert outcome == ListenOutcome(OutcomeKind.ROUTED, "daruma")
    assert "daruma" in caplog.text
    assert "RuntimeError" in caplog.text
    _no_words_in_log(caplog)


def test_録音と文字にするのにかかった時間を記録する(caplog: pytest.LogCaptureFixture) -> None:
    bench = _Bench([TEXT], record_seconds=5.0, transcribe_seconds=2.5)
    bench.words("daruma", PHRASES)

    bench.run()

    assert "かかった時間: 録音 5.0 秒、文字にする 2.5 秒" in caplog.text


@pytest.mark.parametrize(
    "script",
    [[TEXT], [""], [Fail()], [Cancel()], ["きょうは はれ"]],
    ids=["登録", "空", "失敗", "取り消し", "当てはまらない"],
)
def test_どの結果でもログに言葉と語句と話した言葉が出ない(
    caplog: pytest.LogCaptureFixture, script: list[Outcome]
) -> None:
    bench = _Bench(script, record_seconds=5.0, transcribe_seconds=1.0)
    bench.words("daruma", PHRASES)
    bench.words("oekaki", OTHER_PHRASES)

    bench.run()

    _no_words_in_log(caplog)


def test_台本の入出力は入出力の型を満たし_呼ばれた順に残す() -> None:
    fake = ScriptedGatewayIO()
    io: GatewayIO = fake  # 型を満たさなければ mypy が断る
    io.nod()
    io.show("a", "b")
    io.led(True)
    io.say("c")
    assert fake.events == [("nod",), ("show", "a", "b"), ("led", True), ("say", "c")]


# --- 借りた資源で動かす実体 ---


@pytest.fixture
def arbiter(xdg_dirs: dict[str, Path], clock: FakeClock, fakes: FakeDeviceSet) -> Arbiter:
    cache = CacheManager(Paths().cache_dir, max_bytes=10 * 1024 * 1024, clock=clock)
    return Arbiter(
        clock,
        MotionService(fakes.servos, fakes.motors, MotionSettings(), clock),
        voice=VoiceService(fakes.synth, fakes.player, cache, VoiceSettings(sink="speaker")),
        display=DisplayService(fakes.display),
        led=LedService(fakes.led),
    )


RESOURCES = {Resource.VOICE, Resource.DISPLAY, Resource.LED, Resource.BUTTON, Resource.NECK}


def _row(text: str) -> tuple[int, ...]:
    codes = to_lcd_codes(center(text))
    return tuple(codes) + (0x20,) * (16 - len(codes))


def test_借りた資源の入出力は_2行とも中央に置いて表示する(
    arbiter: Arbiter, fakes: FakeDeviceSet
) -> None:
    lease = arbiter.acquire("voice", Priority.INTERACTIVE + 5, RESOURCES)
    io = LeaseGatewayIO(lease)

    io.show(LCD_LISTENING, Face.CALL.text())

    assert fakes.display.rows == [_row(LCD_LISTENING), _row(Face.CALL.text())]
    assert fakes.display.rows[0][0] == 0x20  # 左に空白がある
    assert fakes.display.rows[1][0] == 0x20
    lease.release()


def test_借りた資源の入出力は_LEDと首と声をリースの口で動かす(
    arbiter: Arbiter, fakes: FakeDeviceSet
) -> None:
    lease = arbiter.acquire("voice", Priority.INTERACTIVE + 5, RESOURCES)
    io = LeaseGatewayIO(lease)

    states: list[bool] = []
    io.led(True)
    states.append(fakes.led.on)
    io.led(False)
    states.append(fakes.led.on)
    assert states == [True, False]

    before = len(fakes.servos.calls)
    io.nod()
    assert len(fakes.servos.calls) > before

    io.say(SAY_CALL)
    assert fakes.player.calls
    lease.release()


def test_借りた資源で流れを通すと_資源を返してから口が呼ばれ_その口で資源を借りられる(
    arbiter: Arbiter, clock: FakeClock, fakes: FakeDeviceSet
) -> None:
    registry = IntentRegistry()
    lease = arbiter.acquire("voice", Priority.INTERACTIVE + 5, RESOURCES)
    seen: list[bool] = []

    def on_heard(heard: Heard) -> None:
        del heard
        seen.append(lease.active)
        other = arbiter.acquire("daruma", Priority.INTERACTIVE, {Resource.DISPLAY})
        other.release()

    registry.add_words("daruma", 0, PHRASES, on_heard, alive=lambda: True)
    flow = ListenFlow(ScriptedRecognizer([TEXT]), registry, clock, logging.getLogger(LOGGER))

    outcome = flow.run(
        LeaseGatewayIO(lease),
        follow_up_owner=None,
        seconds=SECONDS,
        cancelled=lambda: lease.preempt_requested.is_set(),
        release=lease.release,
    )

    assert outcome == ListenOutcome(OutcomeKind.ROUTED, "daruma")
    assert seen == [False]
    assert not fakes.led.on
