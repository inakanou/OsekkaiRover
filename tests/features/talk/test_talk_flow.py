"""資源を借りて話す流れ（TalkFlow）。

要件 4.4、6.8、7.1、7.2、7.3、7.5、7.6、7.7、7.8、8.1、8.2、8.3。

本物の資源の調停（Arbiter）・声（VoiceService）・画面（DisplayService）・静かな状態を、
偽のデバイスで組んで確かめる。声と画面は、受けたものを順に覚える形に包む。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from talk_fakes import sunny_forecast

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Arbiter
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.config import MotionSettings, VoiceSettings
from osekkai.core.display import DisplayService
from osekkai.core.events import EventBus
from osekkai.core.feature import CoreFeatureContext, FeatureSpec
from osekkai.core.idle import IdleScreen
from osekkai.core.lcd_text import center
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.quiet import QuietMode
from osekkai.core.storage import StateStore
from osekkai.core.types import Face, Priority, Resource
from osekkai.core.voice import VoiceService
from osekkai.features.talk import texts
from osekkai.features.talk.config import TalkConfig
from osekkai.features.talk.flow import (
    ACQUIRE_WAIT_S,
    RESOURCES,
    FlowOutcome,
    Line,
    TalkFlow,
    compose_lines,
)
from osekkai.features.talk.model import AlertKind, Condition, DayForecast, Prepared, TalkKind
from osekkai.features.talk.weather_talk import lcd_line, weather_sentences

HIDDEN_NAME = "ひみつの{x}なまえ"  # 波かっこを含む呼び名（format にかけると壊れる）
FACT = "ぞうさんは はなで おみずを のむんだよ。"
DAY = date(2026, 11, 20)
WAIT_S = 5.0  # 別のスレッドの終わりを待つ上限（実際の秒）
NORMAL = Face.NORMAL.text()
HAPPY = Face.HAPPY.text()
# 機能（6.1）と同じ申告。資源は声と画面だけ
TALK_SPEC = FeatureSpec(
    name="talk", priority=Priority.BACKGROUND + 5, resources=RESOURCES, config_model=TalkConfig
)


class SpyVoice(VoiceService):
    """本物の声に、話した言葉と残すかを順に覚えさせる。`before` は話す前に呼ぶ。"""

    def __init__(self, events: list[tuple[object, ...]], *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.events = events
        self.before: Callable[[str], None] | None = None

    def speak(
        self,
        text: str,
        *,
        keep: bool = True,
        should_continue: Callable[[], bool] | None = None,
    ) -> bool:
        if self.before is not None:
            self.before(text)
        spoke = super().speak(text, keep=keep, should_continue=should_continue)
        if spoke:
            self.events.append(("say", text, keep))
        return spoke


class SpyDisplay(DisplayService):
    """本物の画面に、お話が画面を持つ間に出した 2 行とバックライトを順に覚えさせる。

    返した後に待機画面が出すものは覚えない。
    """

    def __init__(self, events: list[tuple[object, ...]], *args: object) -> None:
        super().__init__(*args)  # type: ignore[arg-type]
        self.events = events
        self.arbiter: Arbiter | None = None

    def _talking(self) -> bool:
        return self.arbiter is not None and self.arbiter.holder(Resource.DISPLAY) == TALK_SPEC.name

    def show(self, line1: str, line2: str = "") -> None:
        super().show(line1, line2)
        if self._talking():
            self.events.append(("show", line1, line2))

    def set_backlight(self, on: bool) -> None:
        super().set_backlight(on)
        if self._talking():
            self.events.append(("backlight", on))


class TalkKit:
    """流れを、偽のデバイスで組んだ基盤の部品で動かす一式。"""

    def __init__(self, fakes: FakeDeviceSet, clock: FakeClock) -> None:
        self.events: list[tuple[object, ...]] = []
        self.clock = clock
        self.paths = Paths()
        self.router = ButtonRouter(fakes.buttons, clock)
        self.cache = CacheManager.from_paths(self.paths, 10, clock)
        display = SpyDisplay(self.events, fakes.display)
        self.voice = SpyVoice(
            self.events, fakes.synth, fakes.player, self.cache, VoiceSettings(sink="speaker")
        )
        self.arbiter = Arbiter(
            clock,
            MotionService(fakes.servos, fakes.motors, MotionSettings(), clock),
            voice=self.voice,
            display=display,
            buttons=self.router,
        )
        display.arbiter = self.arbiter
        self.idle = IdleScreen(self.arbiter, clock)
        self.bus = EventBus()
        self.quiet = QuietMode(
            self.idle, self.bus, StateStore.for_feature(self.paths, "core", clock), clock
        )
        self.ctx = CoreFeatureContext(
            TALK_SPEC,
            arbiter=self.arbiter,
            buttons=self.router,
            idle=self.idle,
            quiet=self.quiet,
            bus=self.bus,
            clock=clock,
            paths=self.paths,
            cache=self.cache,
        )
        self.started: list[int] = []
        self.stop = threading.Event()  # 機能を止める旗（基盤が立てる）

    def flow(self, config: TalkConfig | None = None) -> TalkFlow:
        return TalkFlow(
            self.ctx,
            config or TalkConfig(enabled=True, call_name=HIDDEN_NAME),
            stopped=self.stop.is_set,
        )

    def on_started(self) -> None:
        self.started.append(len(self.events))

    @property
    def said(self) -> list[str]:
        return [str(e[1]) for e in self.events if e[0] == "say"]

    def assert_released(self) -> None:
        """お話の資源を返した（画面は返した後に待機画面が借りる）。"""
        for resource in RESOURCES:
            assert self.arbiter.holder(resource) != TALK_SPEC.name


@pytest.fixture
def kit(xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock) -> TalkKit:
    return TalkKit(fakes, clock)


def _prepared(
    kind: TalkKind = TalkKind.NIGHT,
    *,
    forecast: DayForecast | None = None,
    alerts: frozenset[AlertKind] | None = None,
    fact: str | None = None,
) -> Prepared:
    return Prepared(kind, DAY, forecast, alerts, fact, None)


def _full(kind: TalkKind = TalkKind.NIGHT) -> Prepared:
    return _prepared(kind, forecast=sunny_forecast(DAY), alerts=frozenset(), fact=FACT)


# --- 文の並べ方（compose_lines） ---


@pytest.mark.parametrize("kind", [TalkKind.NIGHT, TalkKind.MORNING])
def test_始まり_天気_豆知識_終わりの順に並べ_呼び名を入れる(kind: TalkKind) -> None:
    config = TalkConfig(call_name=HIDDEN_NAME)
    forecast = DayForecast(DAY, Condition.RAIN, 12, 5, 80)
    alerts = frozenset({AlertKind.THUNDER})

    lines = compose_lines(_prepared(kind, forecast=forecast, alerts=alerts, fact=FACT), config)

    weather = weather_sentences(kind, forecast, alerts, config)
    weather_lcd = lcd_line(kind, forecast)
    assert len(weather) >= 3
    assert lines == [
        Line(texts.OPENERS[kind].replace("{name}", HIDDEN_NAME), texts.LCD_TALK, HAPPY),
        *(Line(s, weather_lcd, NORMAL) for s in weather),
        Line(FACT, texts.LCD_TALK, HAPPY),
        Line(texts.CLOSERS[kind].replace("{name}", HIDDEN_NAME), texts.LCD_TALK, HAPPY),
    ]


def test_呼び名がなければきみと呼ぶ() -> None:
    lines = compose_lines(_full(), TalkConfig())

    assert lines[0].text == texts.OPENERS[TalkKind.NIGHT].replace("{name}", "きみ")
    assert lines[-1].text == texts.CLOSERS[TalkKind.NIGHT].replace("{name}", "きみ")


def test_天気がなければ豆知識だけを始まりと終わりで挟む() -> None:
    lines = compose_lines(_prepared(fact=FACT), TalkConfig(call_name=HIDDEN_NAME))

    assert [line.text for line in lines] == [
        texts.OPENERS[TalkKind.NIGHT].replace("{name}", HIDDEN_NAME),
        FACT,
        texts.CLOSERS[TalkKind.NIGHT].replace("{name}", HIDDEN_NAME),
    ]


def test_豆知識がなければ天気だけを始まりと終わりで挟む() -> None:
    config = TalkConfig()
    forecast = sunny_forecast(DAY)

    lines = compose_lines(_prepared(TalkKind.MORNING, forecast=forecast), config)

    weather = weather_sentences(TalkKind.MORNING, forecast, None, config)
    assert [line.text for line in lines][1:-1] == weather
    assert FACT not in [line.text for line in lines]


@pytest.mark.parametrize(
    ("kind", "fallback"),
    [
        (TalkKind.NIGHT, texts.SAY_FALLBACK_NIGHT),
        (TalkKind.MORNING, texts.SAY_FALLBACK_MORNING),
    ],
)
def test_天気も豆知識もなければ決まった短い言葉だけ(kind: TalkKind, fallback: str) -> None:
    # 注意報だけがあっても天気の話はしない
    prepared = _prepared(kind, alerts=frozenset({AlertKind.HEAVY_RAIN}))

    lines = compose_lines(prepared, TalkConfig(call_name=HIDDEN_NAME))

    assert lines == [Line(fallback, texts.LCD_TALK, HAPPY)]


def test_お話はおおむね60秒に収まる長さ() -> None:
    # 天気は最大 6 文。始まり・豆知識（2 文以内）・終わりで最大 9 文（7.3）
    forecast = DayForecast(DAY, Condition.RAIN, 31, 2, 90)
    alerts = frozenset(AlertKind)

    lines = compose_lines(_prepared(forecast=forecast, alerts=alerts, fact=FACT), TalkConfig())

    assert len(lines) <= 9


# --- 話す（TalkFlow.speak） ---


def test_借りて記録してから_順に残さない声で話し_LCDを出して返す(kit: TalkKit) -> None:
    flow = kit.flow()
    prepared = _full()
    lines = compose_lines(prepared, TalkConfig(call_name=HIDDEN_NAME))

    outcome = flow.speak(prepared, kit.on_started)

    assert outcome is FlowOutcome.SPOKEN
    assert kit.started == [0]  # 声と画面より前に記録した（4.4）
    expected: list[tuple[object, ...]] = [("backlight", True)]
    for line in lines:
        expected.append(("show", center(line.lcd1), center(line.lcd2)))
        expected.append(("say", line.text, False))  # 6.8
    assert kit.events == expected
    kit.assert_released()  # 7.8


def test_天気の文の間は天気のLCD_そのほかはｵﾊﾅｼと喜んだ顔(kit: TalkKit) -> None:
    prepared = _full(TalkKind.MORNING)

    kit.flow().speak(prepared, kit.on_started)

    shows = [(e[1], e[2]) for e in kit.events if e[0] == "show"]
    weather_lcd = center(lcd_line(TalkKind.MORNING, sunny_forecast(DAY)))
    talk = (center(texts.LCD_TALK), center(HAPPY))
    assert shows[0] == talk
    assert shows[-2:] == [talk, talk]
    assert set(shows[1:-2]) == {(weather_lcd, center(NORMAL))}


def test_決まった短い言葉だけのときも残さない声で話す(kit: TalkKit) -> None:
    outcome = kit.flow().speak(_prepared(), kit.on_started)

    assert outcome is FlowOutcome.SPOKEN
    assert [e for e in kit.events if e[0] == "say"] == [("say", texts.SAY_FALLBACK_NIGHT, False)]


def test_静かな状態なら借りず記録せず話さない(kit: TalkKit) -> None:
    kit.quiet.request_quiet()
    kit.events.clear()

    outcome = kit.flow().speak(_full(), kit.on_started)

    assert outcome is FlowOutcome.QUIET
    assert kit.started == []
    assert kit.events == []  # 声も画面も出さない（8.2）
    kit.assert_released()


def test_ほかの機能が声を持っていれば待たずに借りられないを返す(kit: TalkKit) -> None:
    held = kit.arbiter.acquire("conversation", Priority.INTERACTIVE, {Resource.VOICE})

    outcome = kit.flow().speak(_full(), kit.on_started)

    assert outcome is FlowOutcome.BUSY
    assert kit.started == []
    assert kit.events == []
    assert held.active
    assert kit.arbiter.holder(Resource.DISPLAY) is None
    held.release()
    assert ACQUIRE_WAIT_S == 0.0


def test_記録が書けなければ話さずに返す(kit: TalkKit) -> None:
    def broken() -> None:
        raise OSError("disk")

    outcome = kit.flow().speak(_full(), broken)

    assert outcome is FlowOutcome.RECORD_FAILED
    assert kit.events == []
    kit.assert_released()


def test_思いがけない例外でも資源を返す(kit: TalkKit) -> None:
    def broken() -> None:
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        kit.flow().speak(_full(), broken)

    kit.assert_released()


def test_日課が借りに来たら言い終えた文の切れ目でやめて渡す(kit: TalkKit) -> None:
    handed = threading.Event()
    count = {"n": 0}

    def preempt(_text: str) -> None:
        count["n"] += 1
        if count["n"] != 2:
            return
        lease = kit.arbiter.lease_for(Resource.VOICE)
        assert lease is not None

        def take() -> None:
            routine = kit.arbiter.acquire(
                "routine", Priority.SCHEDULED + 5, {Resource.VOICE, Resource.DISPLAY}
            )
            handed.set()
            routine.release()

        threading.Thread(target=take, daemon=True).start()
        assert lease.preempt_requested.wait(WAIT_S)

    kit.voice.before = preempt
    prepared = _full()
    lines = compose_lines(prepared, TalkConfig(call_name=HIDDEN_NAME))

    outcome = kit.flow().speak(prepared, kit.on_started)

    assert outcome is FlowOutcome.INTERRUPTED
    assert kit.said == [lines[0].text, lines[1].text]  # 2 文目を言い終えてやめた（8.1）
    assert kit.started == [0]  # 話した扱いの記録は付いている（4.4）
    assert handed.wait(WAIT_S)  # 日課に渡した


def test_途中で静かな状態になったら言い終えた文の切れ目でやめる(kit: TalkKit) -> None:
    kit.voice.before = lambda _text: kit.quiet.request_quiet()
    prepared = _full()
    lines = compose_lines(prepared, TalkConfig(call_name=HIDDEN_NAME))

    outcome = kit.flow().speak(prepared, kit.on_started)

    assert outcome is FlowOutcome.INTERRUPTED
    assert kit.said == [lines[0].text]  # 8.3
    kit.assert_released()


def test_止める旗が立ったら言い終えた文の切れ目でやめる(kit: TalkKit) -> None:
    kit.voice.before = lambda _text: kit.stop.set()
    prepared = _full()
    lines = compose_lines(prepared, TalkConfig(call_name=HIDDEN_NAME))

    outcome = kit.flow().speak(prepared, kit.on_started)

    assert outcome is FlowOutcome.INTERRUPTED
    assert kit.said == [lines[0].text]  # 1 文目を言い終えてやめた
    assert kit.started == [0]  # 話した扱いの記録は付いている（4.4）
    kit.assert_released()


def test_止める旗が立っていれば借りずに記録もしない(kit: TalkKit) -> None:
    kit.stop.set()

    outcome = kit.flow().speak(_full(), kit.on_started)

    assert outcome is FlowOutcome.BUSY
    assert kit.said == []
    assert kit.started == []
    kit.assert_released()


def test_話せなければやめて資源を返す(kit: TalkKit, fakes: FakeDeviceSet) -> None:
    fakes.synth.fail_on.add("synthesize")

    outcome = kit.flow().speak(_full(), kit.on_started)

    assert outcome is FlowOutcome.FAILED
    assert kit.said == []
    kit.assert_released()


def test_呼び名_天気_豆知識をログに出さない(kit: TalkKit, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="osekkai.features.talk")

    kit.flow().speak(_full(), kit.on_started)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "お話を話しました" in logged
    assert HIDDEN_NAME not in logged
    assert FACT not in logged
    assert "はれ" not in logged
