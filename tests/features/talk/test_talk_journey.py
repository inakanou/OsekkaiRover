"""朝と夜のお話を、本物の日課と一緒に 1 日通して確かめるテスト（E2E）。

要件 2.1、3.3、3.8、4.2、7.6、8.1、12.3。

偽の時計・偽の天気・偽の注意報・偽の会話の AI と、偽のデバイスで組んだ本物の基盤の部品
（Arbiter・声・画面・静かな状態）の上で、本物の日課の機能と本物のお話の機能を動かす。
日課の入出力は本物（`LeaseIO`）で、ボタンの押下だけを「1 秒後に押す」に替える。
確かめは、1 本のスレッドで日課の `tick` とお話の `step` を順に呼び、偽の時計を進めて行う。
話した言葉は、声の部品が、話し始めた時刻と声を持っていた機能と一緒に順に覚える。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from talk_fakes import (
    FakeAlerts,
    FakeChatModel,
    FakeWeather,
    settle_preparation,
    sunny_forecast,
)

import osekkai.features.routine.api as routine_api
from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import JST, FakeClock
from osekkai.core.arbiter import Arbiter, Lease
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.config import MotionSettings, VoiceSettings
from osekkai.core.display import DisplayService, LedService
from osekkai.core.events import EventBus
from osekkai.core.feature import CoreFeatureContext, FeatureSpec
from osekkai.core.idle import IdleScreen
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.quiet import QuietMode
from osekkai.core.storage import StateStore
from osekkai.core.types import Resource
from osekkai.core.voice import VoiceService
from osekkai.features.conversation.api import ChatResult, Failed, FailureKind, Replied
from osekkai.features.routine.config import RoutineConfig
from osekkai.features.routine.feature import RoutineFeature
from osekkai.features.routine.records import RecordBook, TaskResult
from osekkai.features.routine.runner import LeaseIO
from osekkai.features.talk import texts
from osekkai.features.talk.config import Location, TalkConfig, WarningArea
from osekkai.features.talk.feature import Sources, TalkFeature
from osekkai.features.talk.model import TalkKind, WeatherProblem, WeatherUnavailable
from osekkai.features.talk.prepare import ChatFactSource
from osekkai.features.talk.record import TalkRecord
from osekkai.features.talk.weather_talk import weather_sentences

HIDDEN_NAME = "ひみつの{x}なまえ"  # 波かっこを含む呼び名（format にかけると壊れる）
HIDDEN_TOPIC = "ひみつのすきなもの"
FACT = "ぞうさんは はなで おみずを のむんだよ。"
THU = date(2026, 11, 19)  # 平日
FRI = date(2026, 11, 20)  # 平日
STEP_S = 60.0  # 何も起きない間の確かめの間隔（偽の時計の秒）
PRESS_AFTER_S = 1.0  # 声かけの後、子どもが押すまでの秒（偽の時計）
WAIT_S = 5.0  # 別のスレッドを待つ上限（実際の秒）
LOCATION = Location(latitude=35.68, longitude=139.69)
AREA = WarningArea(office="130000", city="1311300")

# 日課の言葉（テスト用の決まった値）
BRUSH_CALL = "はを みがこうね"
BRUSH_NOTICE = "もうすぐ はみがきの じかんだよ"
SLEEP_CALL = "ねる じかんだよ"
SLEEP_NOTICE = "もうすぐ ねる じかんだよ"
WAKE_CALL = "おきる じかんだよ"
WAKE_NOTICE = "もうすぐ おきる じかんだよ"
PRAISE = "すごいね"


def _at(day: date, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=JST)


def _task(task_id: str, at: str, call: str, **fields: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"id": task_id, "at": at, "call": call, "praise": PRAISE, "lcd": "ﾃｽﾄ"}
    data.update(fields)
    return data


def _routine(*, sleep_notice_min: int = 10) -> RoutineConfig:
    """起床 06:30、07:00 のはみがき（予告 06:50）、20:30 の寝るタスク（予告つき）。"""
    return RoutineConfig.model_validate(
        {
            "wake": {"weekday": "06:30", "holiday": "06:30"},
            "tasks": [
                _task("brush", "07:00", BRUSH_CALL, notice=BRUSH_NOTICE),
                _task(
                    "sleep",
                    "20:30",
                    SLEEP_CALL,
                    notice=SLEEP_NOTICE,
                    notice_min=sleep_notice_min,
                    sleep=True,
                ),
            ],
        }
    )


def _routine_wake_task() -> RoutineConfig:
    """起床時刻の設定がなく、最初のタスクが 06:30（予告 06:20 はおやすみの間）。"""
    return RoutineConfig.model_validate(
        {
            "tasks": [
                _task("wake_up", "06:30", WAKE_CALL, notice=WAKE_NOTICE),
                _task("sleep", "20:30", SLEEP_CALL, notice=SLEEP_NOTICE, sleep=True),
            ],
        }
    )


def _talk(**changes: object) -> TalkConfig:
    values: dict[str, object] = {
        "enabled": True,
        "call_name": HIDDEN_NAME,
        "location": LOCATION,
        "warning_area": AREA,
        "topics": [HIDDEN_TOPIC],
    }
    values.update(changes)
    return TalkConfig.model_validate(values)


def _opener(kind: TalkKind) -> str:
    return texts.fill_name(texts.OPENERS[kind], HIDDEN_NAME)


def _closer(kind: TalkKind) -> str:
    return texts.fill_name(texts.CLOSERS[kind], HIDDEN_NAME)


@dataclass(frozen=True)
class Said:
    """話した 1 回分。話し始めた時刻と、そのとき声を持っていた機能。"""

    at: datetime
    owner: str | None
    text: str


class JourneyVoice(VoiceService):
    """本物の声に、話した言葉を順に覚えさせる。

    お話の 1 文ごとに、話す前に `before` を呼び、話した後に偽の時計を `talk_sentence_s`
    秒進める（話す長さの代わり）。
    """

    def __init__(self, clock: FakeClock, *args: Any) -> None:
        super().__init__(*args)
        self._clock = clock
        self.arbiter: Arbiter | None = None
        self.said: list[Said] = []
        self.before: Callable[[str], None] | None = None
        self.talk_sentence_s = 0.0

    def speak(
        self,
        text: str,
        *,
        keep: bool = True,
        should_continue: Callable[[], bool] | None = None,
    ) -> bool:
        owner = self.arbiter.holder(Resource.VOICE) if self.arbiter is not None else None
        if owner == "talk" and self.before is not None:
            self.before(text)
        start = self._clock.now()
        spoke = super().speak(text, keep=keep, should_continue=should_continue)
        if spoke:
            self.said.append(Said(start, owner, text))
            if owner == "talk":
                self._clock.advance(self.talk_sentence_s)
        return spoke


class PressingIO(LeaseIO):
    """日課の本物の入出力。ボタンだけは、待ち始めて 1 秒後に押されたことにする。"""

    def __init__(self, lease: Lease, clock: FakeClock) -> None:
        super().__init__(lease)
        self._clock = clock

    def clear_presses(self) -> None:
        pass

    def wait_press(self, timeout: float) -> bool:
        self._clock.advance(min(PRESS_AFTER_S, timeout))
        return True


class Journey:
    """日課とお話を、偽のデバイスで組んだ基盤の部品で一緒に動かす一式。"""

    def __init__(self, fakes: FakeDeviceSet, clock: FakeClock) -> None:
        self.fakes = fakes
        self.clock = clock
        self.paths = Paths()
        self.router = ButtonRouter(fakes.buttons, clock)
        self.cache = CacheManager.from_paths(self.paths, 10, clock)
        self.voice = JourneyVoice(
            clock, fakes.synth, fakes.player, self.cache, VoiceSettings(sink="speaker")
        )
        self.arbiter = Arbiter(
            clock,
            MotionService(fakes.servos, fakes.motors, MotionSettings(), clock),
            voice=self.voice,
            display=DisplayService(fakes.display),
            led=LedService(fakes.led),
            buttons=self.router,
        )
        self.voice.arbiter = self.arbiter
        self.idle = IdleScreen(self.arbiter, clock)
        self.bus = EventBus()
        self.quiet = QuietMode(
            self.idle, self.bus, StateStore.for_feature(self.paths, "core", clock), clock
        )
        self.weather = FakeWeather()
        self.alerts = FakeAlerts()
        self.chat = FakeChatModel([Replied(FACT)] * 10)
        self.routine_config = _routine()
        self.routine: RoutineFeature | None = None
        self.talk: TalkFeature | None = None

    def ctx(self, spec: FeatureSpec) -> CoreFeatureContext:
        return CoreFeatureContext(
            spec,
            arbiter=self.arbiter,
            buttons=self.router,
            idle=self.idle,
            quiet=self.quiet,
            bus=self.bus,
            clock=self.clock,
            paths=self.paths,
            cache=self.cache,
        )

    def boot(self, routine: RoutineConfig, talk: TalkConfig) -> None:
        """日課とお話の機能を作る（起動し直すときも同じ保存先を使う）。"""
        self.routine_config = routine
        self.routine = RoutineFeature(
            self.ctx(RoutineFeature.spec),
            routine,
            io_factory=lambda lease: PressingIO(lease, self.clock),
            sleep_show_s=0.0,
        )
        self.routine._boot()
        self.talk = TalkFeature(
            self.ctx(TalkFeature.spec),
            talk,
            sources=Sources(self.weather, self.alerts, ChatFactSource(self.chat)),
        )

    def tick(self) -> None:
        """1 回の確かめ。日課が先に確かめて状況を出し、お話がそれを読む。"""
        assert self.routine is not None and self.talk is not None
        self.routine.tick()
        self.talk.step()
        settle_preparation()  # 用意を終えてから時計を進める（決まった順で進めるため）

    def run_until(self, moment: datetime) -> None:
        """その時刻まで確かめを続ける（最後はちょうどその時刻か、少し後に確かめる）。"""
        while True:
            self.tick()
            remaining = (moment - self.clock.now()).total_seconds()
            if remaining <= 0:
                return
            self.clock.advance(min(STEP_S, remaining))

    def move_to(self, moment: datetime) -> None:
        """確かめずに、その時刻まで時計を進める（ロボットが止まっていた間）。"""
        self.clock.advance((moment - self.clock.now()).total_seconds())

    # --- 確かめの道具 ---

    @property
    def said(self) -> list[Said]:
        return list(self.voice.said)

    def said_by(self, owner: str) -> list[Said]:
        return [s for s in self.said if s.owner == owner]

    def talk_lines(self) -> list[str]:
        return [s.text for s in self.said_by("talk")]

    def times_of(self, text: str) -> list[datetime]:
        return [s.at for s in self.said if s.text == text]

    def record(self) -> TalkRecord:
        return TalkRecord(StateStore.for_feature(self.paths, "talk", self.clock))

    def book(self) -> RecordBook:
        store = StateStore.for_feature(self.paths, "routine", self.clock)
        return RecordBook.load(store, self.routine_config.keep_days, self.clock)

    def is_quiet(self) -> bool:
        return self.quiet.is_quiet


@pytest.fixture
def clock() -> FakeClock:
    """木曜の朝 5 時（日本の時刻）で始まる偽の時計。`fakes` もこの時計を使う。"""
    return FakeClock(_at(THU, 5, 0))


@pytest.fixture
def journey(xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock) -> Iterator[Journey]:
    routine_api._publish(None)
    j = Journey(fakes, clock)
    j.router.start()
    j.bus.start()
    j.idle.start(run_thread=False)
    yield j
    routine_api._publish(None)
    j.idle.stop()
    j.bus.close()
    j.router.close()


def _just_before(said: list[Said], text: str) -> str:
    """`text` の 1 つ前に話した言葉。"""
    index = [s.text for s in said].index(text)
    assert index > 0
    return said[index - 1].text


# --- 1 日の流れ（2.1、3.8、4.1、12.3） ---


def test_朝の予告_朝のお話_夜の予告_夜のお話_おやすみ_声なしの起床_朝のお話が_1_日に_1_回ずつ(
    journey: Journey,
) -> None:
    j = journey
    j.move_to(_at(THU, 6, 0))
    j.boot(_routine(), _talk())  # 起床時刻の前に起動し、静かに待つ

    j.run_until(_at(FRI, 7, 30))

    said = j.said
    morning, night = _opener(TalkKind.MORNING), _opener(TalkKind.NIGHT)
    # 夜のお話は木曜に 1 回、朝のお話は木曜と金曜に 1 回ずつ
    morning_at = j.times_of(morning)
    night_at = j.times_of(night)
    assert [t.date() for t in morning_at] == [THU, FRI]
    assert [t.date() for t in night_at] == [THU]
    # 夜: 寝るタスクの予告（20:20）を言い終えた後に続けて話し、寝るタスク（20:30）の前に終える
    assert _at(THU, 20, 20) <= night_at[0] < _at(THU, 20, 21)
    assert _just_before(said, night) == SLEEP_NOTICE
    assert j.times_of(_closer(TalkKind.NIGHT))[0] < j.times_of(SLEEP_CALL)[0]
    assert _at(THU, 20, 30) <= j.times_of(SLEEP_CALL)[0] < _at(THU, 20, 31)
    # 朝: 最初のタスクの予告（06:50）の後に続けて話し、07:00 の声かけは遅れない
    for day, opened in zip((THU, FRI), morning_at, strict=True):
        assert _at(day, 6, 50) <= opened < _at(day, 6, 51)
    texts_said = [s.text for s in said]
    openers = [i for i, text in enumerate(texts_said) if text == morning]
    assert [texts_said[i - 1] for i in openers] == [BRUSH_NOTICE, BRUSH_NOTICE]
    brush_at = j.times_of(BRUSH_CALL)
    assert len(brush_at) == 2
    for day, call_at in zip((THU, FRI), brush_at, strict=True):
        assert _at(day, 7, 0) <= call_at < _at(day, 7, 1)
    closers = j.times_of(_closer(TalkKind.MORNING))
    assert all(c < b for c, b in zip(closers, brush_at, strict=True))
    # 3.8: おやすみ（寝るタスクの後）から金曜の朝の予告まで、声を出さない（起床の 06:30 も）
    quiet_span = [s for s in said if _at(THU, 20, 31) <= s.at < _at(FRI, 6, 50)]
    assert quiet_span == []
    friday_first = next(s for s in said if s.at.date() == FRI)
    assert friday_first.text == BRUSH_NOTICE
    assert friday_first.owner == "routine"
    # 記録: その日に話したお話は済み。日課の結果も付いている
    record = j.record()
    assert record.done(THU, TalkKind.MORNING)
    assert record.done(THU, TalkKind.NIGHT)
    assert record.done(FRI, TalkKind.MORNING)
    assert not record.done(FRI, TalkKind.NIGHT)
    book = j.book()
    assert book.outcome(THU, "sleep") is TaskResult.DONE
    assert book.outcome(FRI, "brush") is TaskResult.DONE
    # 天気は、朝はきょう・夜はあした。用意はお話ごとに 1 回だけ取りに行く
    assert [call.day for call in j.weather.calls] == [THU, FRI, FRI]
    assert j.chat.call_count == 3
    assert FACT in j.talk_lines()


def test_起床の時に声を出さず_静かな状態だけを終える(journey: Journey) -> None:
    j = journey
    j.move_to(_at(THU, 6, 0))
    j.boot(_routine(), _talk())

    j.run_until(_at(THU, 6, 29))
    assert j.is_quiet()
    j.run_until(_at(THU, 6, 49))

    assert not j.is_quiet()
    assert j.said == []  # 3.8: 起床時刻（06:30）に、お話も日課も声を出さない
    assert not j.record().done(THU, TalkKind.MORNING)


# --- 予告がおやすみの間に当たる朝（3.3） ---


def test_予告がおやすみの間なら_最初のタスクが終わった後に朝のお話を_1_回話す(
    journey: Journey,
) -> None:
    j = journey
    j.move_to(_at(FRI, 6, 0))
    j.boot(_routine_wake_task(), _talk(night=False))

    j.run_until(_at(FRI, 6, 29))
    assert j.said == []
    assert j.is_quiet()

    j.run_until(_at(FRI, 8, 0))

    said = j.said
    morning = _opener(TalkKind.MORNING)
    assert WAKE_NOTICE not in [s.text for s in said]  # 予告はおやすみの間なので言わない
    assert said[0].text == WAKE_CALL  # 06:30 の最初の声は日課の声かけ
    assert _at(FRI, 6, 30) <= said[0].at < _at(FRI, 6, 31)
    assert j.times_of(morning) and len(j.times_of(morning)) == 1
    ended = j.book().ended_at(FRI, "wake_up")
    assert ended is not None
    assert j.times_of(morning)[0] >= ended
    assert _just_before(said, morning) == PRAISE  # 最初のタスクのほめる言葉の後に続ける
    assert j.record().done(FRI, TalkKind.MORNING)
    assert [call.day for call in j.weather.calls] == [FRI]  # 朝はきょうの天気


# --- 夜のお話の後の再起動（4.2） ---


def test_夜のお話の後に起動し直しても_もう一度は話さない(journey: Journey) -> None:
    j = journey
    j.move_to(_at(THU, 20, 0))
    j.boot(_routine(), _talk(morning=False))
    j.run_until(_at(THU, 20, 22))
    assert len(j.times_of(_opener(TalkKind.NIGHT))) == 1

    # 日課もお話も作り直す（保存先は同じ）。新しい出どころは問い合わせを数える
    j.weather = FakeWeather()
    j.alerts = FakeAlerts()
    j.chat = FakeChatModel([Replied(FACT)] * 10)
    j.boot(_routine(), _talk(morning=False))
    j.run_until(_at(THU, 20, 32))

    assert len(j.times_of(_opener(TalkKind.NIGHT))) == 1
    assert j.weather.call_count == 0
    assert j.chat.call_count == 0
    assert len(j.times_of(SLEEP_NOTICE)) == 1  # 日課も予告を言い直さない
    assert len(j.times_of(SLEEP_CALL)) == 1  # 寝るタスクはいつもどおり始まる
    assert j.is_quiet()


# --- 天気なし・豆知識なし・両方なし（5.10、6.7、7.6） ---

_WEATHER_FAILS = WeatherUnavailable(WeatherProblem.CONNECTION)
_FACT_FAILS: ChatResult = Failed(FailureKind.CONNECTION)


@pytest.mark.parametrize(
    ("weather_fails", "fact_fails"),
    [(True, False), (False, True), (True, True)],
    ids=["天気なし", "豆知識なし", "両方なし"],
)
def test_取れなかった部分を除いて話し_両方なければ決まった短い言葉だけ(
    journey: Journey, weather_fails: bool, fact_fails: bool
) -> None:
    j = journey
    config = _talk(morning=False)
    if weather_fails:
        j.weather = FakeWeather(_WEATHER_FAILS)
    if fact_fails:
        j.chat = FakeChatModel([_FACT_FAILS] * 10)
    j.move_to(_at(THU, 20, 10))
    j.boot(_routine(), config)

    j.run_until(_at(THU, 20, 25))

    night = TalkKind.NIGHT
    weather = weather_sentences(night, sunny_forecast(FRI), frozenset(), config)
    if weather_fails and fact_fails:
        expected = [texts.SAY_FALLBACK_NIGHT]  # 始まりの言葉も言わない（7.6）
    elif weather_fails:
        expected = [_opener(night), FACT, _closer(night)]
    else:
        expected = [_opener(night), *weather, _closer(night)]
    assert j.talk_lines() == expected
    assert _just_before(j.said, expected[0]) == SLEEP_NOTICE
    assert j.record().done(THU, night)


# --- 日課の声かけをお話で遅らせない（8.1） ---


def test_お話の途中で寝るタスクの時刻になったら_文の切れ目で日課に渡し_声かけは遅れない(
    journey: Journey,
) -> None:
    j = journey
    sentence_s = 25.0  # お話の 1 文を話す長さ（偽の時計）
    bedtime = _at(THU, 20, 30)
    j.voice.talk_sentence_s = sentence_s
    routine_threads: list[threading.Thread] = []

    def routine_cuts_in(_text: str) -> None:
        """寝るタスクの時刻になったら、別のスレッドの日課が確かめて資源を借りに来る。"""
        if routine_threads or j.clock.now() < bedtime:
            return
        lease = j.arbiter.lease_for(Resource.VOICE)
        assert lease is not None and lease.owner == "talk"
        routine = j.routine
        assert routine is not None
        thread = threading.Thread(target=routine.tick, daemon=True)
        routine_threads.append(thread)
        thread.start()
        assert lease.preempt_requested.wait(WAIT_S)  # 日課が借りに来た（この文は言い終える）

    j.voice.before = routine_cuts_in
    j.move_to(_at(THU, 20, 25))
    j.boot(_routine(sleep_notice_min=1), _talk(morning=False))  # 予告は 20:29

    j.run_until(_at(THU, 20, 29))  # 予告の後にお話を始め、話している間に 20:30 になる
    assert len(routine_threads) == 1
    routine_threads[0].join(WAIT_S)
    assert not routine_threads[0].is_alive()

    talk = j.said_by("talk")
    full = 1 + len(weather_sentences(TalkKind.NIGHT, sunny_forecast(FRI), frozenset(), _talk()))
    full += 2  # 豆知識と終わりの言葉
    # 20:30 を過ぎた文を言い終えたところでやめ、続き（終わりの言葉）は話さない（8.1、4.4）
    assert 1 < len(talk) < full
    assert talk[0].text == _opener(TalkKind.NIGHT)
    assert talk[-1].at >= bedtime > talk[-2].at
    assert _closer(TalkKind.NIGHT) not in j.talk_lines()
    assert j.record().done(THU, TalkKind.NIGHT)
    # 日課の声かけは、お話の最後の文を言い終えた直後に始まり、1 分以内（日課の 3.8）
    call_at = j.times_of(SLEEP_CALL)
    assert len(call_at) == 1
    talk_end = talk[-1].at + timedelta(seconds=sentence_s)
    assert talk_end <= call_at[0] < bedtime + timedelta(minutes=1)
    assert call_at[0] - talk_end < timedelta(seconds=sentence_s)
    assert j.book().outcome(THU, "sleep") is TaskResult.DONE
    assert j.is_quiet()
    assert j.arbiter.holder(Resource.VOICE) is None

    # その日は、もう話さない
    j.voice.before = None
    j.run_until(_at(THU, 20, 40))
    assert len(j.times_of(_opener(TalkKind.NIGHT))) == 1
