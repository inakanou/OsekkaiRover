"""朝と夜のお話の機能（TalkFeature）のテスト。

要件 1.7、2.4、3.6、4.2、4.4、8.4、8.6、8.7、10.3、12.2。

本物の資源の調停・声・画面・静かな状態・保存先を偽のデバイスで組み、偽の時計を進めて、
日課の公開の状況を差し替えながら 1 秒ごとの判定（`step`）を呼んで確かめる。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pytest
from talk_fakes import (
    HOLD_S,
    FakeAlerts,
    FakeChatModel,
    FakeWeather,
    make_moment,
    make_status,
    settle_preparation,
)

import osekkai.features.conversation.api as conversation_api
import osekkai.features.routine.api as routine_api
from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import JST, FakeClock
from osekkai.core.arbiter import Arbiter, Lease
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.config import FeatureConfig, MotionSettings, VoiceSettings
from osekkai.core.display import DisplayService
from osekkai.core.events import EventBus
from osekkai.core.feature import CoreFeatureContext, FeatureContext, FeatureSpec
from osekkai.core.idle import IdleScreen
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.quiet import QuietMode
from osekkai.core.registry import FeaturePlan
from osekkai.core.storage import StateStore
from osekkai.core.supervisor import FeatureState, FeatureSupervisor
from osekkai.core.types import Priority, Resource
from osekkai.core.voice import VoiceService
from osekkai.features.conversation.api import Replied
from osekkai.features.routine.api import TaskMoment
from osekkai.features.talk import feature as talk_feature
from osekkai.features.talk import texts
from osekkai.features.talk.adapters.canned import CannedAlerts, CannedFacts, CannedWeather
from osekkai.features.talk.adapters.jma_alerts import JmaAlerts
from osekkai.features.talk.adapters.open_meteo import OpenMeteoWeather
from osekkai.features.talk.config import Location, TalkConfig, WarningArea
from osekkai.features.talk.feature import Sources, TalkFeature, choose_sources
from osekkai.features.talk.model import TalkKind
from osekkai.features.talk.prepare import ChatFactSource
from osekkai.features.talk.record import TalkOutcome, TalkRecord

HIDDEN_NAME = "ひみつの{x}なまえ"
HIDDEN_TOPIC = "ひみつのすきなもの"
HIDDEN_ERROR = "ひみつの中身"
FACT = "ぞうさんは はなで おみずを のむんだよ。"
DAY = date(2026, 11, 20)
BEDTIME = datetime(2026, 11, 20, 21, 0, tzinfo=JST)  # 予告は 20:50
WAIT_S = 5.0  # 別のスレッドの終わりを待つ上限（実際の秒）
LOCATION = Location(latitude=35.68, longitude=139.69)
AREA = WarningArea(office="130000", city="1311300")


def _config(**changes: object) -> TalkConfig:
    values: dict[str, object] = {
        "enabled": True,
        "call_name": HIDDEN_NAME,
        "location": LOCATION,
        "topics": [HIDDEN_TOPIC],
    }
    values.update(changes)
    return TalkConfig.model_validate(values)


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 20, hour, minute, second, tzinfo=JST)


class Kit:
    """機能を、偽のデバイスで組んだ基盤の部品で動かす一式。"""

    def __init__(self, fakes: FakeDeviceSet, clock: FakeClock) -> None:
        self.fakes = fakes
        self.clock = clock
        self.paths = Paths()
        self.router = ButtonRouter(fakes.buttons, clock)
        self.cache = CacheManager.from_paths(self.paths, 10, clock)
        self.arbiter = Arbiter(
            clock,
            MotionService(fakes.servos, fakes.motors, MotionSettings(), clock),
            voice=VoiceService(
                fakes.synth, fakes.player, self.cache, VoiceSettings(sink="speaker")
            ),
            display=DisplayService(fakes.display),
            buttons=self.router,
        )
        self.idle = IdleScreen(self.arbiter, clock)
        self.bus = EventBus()
        self.quiet = QuietMode(
            self.idle, self.bus, StateStore.for_feature(self.paths, "core", clock), clock
        )
        self.weather = FakeWeather()
        self.alerts = FakeAlerts()
        self.chat = FakeChatModel([Replied(FACT)] * 10)

    def ctx(self, spec: FeatureSpec = TalkFeature.spec) -> CoreFeatureContext:
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

    def sources(self) -> Sources:
        return Sources(self.weather, self.alerts, ChatFactSource(self.chat))

    def feature(self, config: TalkConfig | None = None) -> TalkFeature:
        return TalkFeature(self.ctx(), config or _config(), sources=self.sources())

    def record(self) -> TalkRecord:
        return TalkRecord(StateStore.for_feature(self.paths, "talk", self.clock))

    @property
    def said(self) -> list[str]:
        return list(self.fakes.synth.lines)

    def said_opener(self, kind: TalkKind = TalkKind.NIGHT) -> int:
        opener = texts.fill_name(texts.OPENERS[kind], HIDDEN_NAME)
        return self.said.count(opener)

    def hold_voice(self, owner: str = "conversation") -> Lease:
        """ほかの、より高い優先度の機能が声を借りる。"""
        return self.arbiter.acquire(owner, Priority.INTERACTIVE, {Resource.VOICE})

    def assert_released(self) -> None:
        for resource in (Resource.VOICE, Resource.DISPLAY):
            assert self.arbiter.holder(resource) != "talk"


@pytest.fixture(autouse=True)
def _reset_routine() -> Iterator[None]:
    routine_api._publish(None)
    yield
    routine_api._publish(None)


@pytest.fixture
def kit(xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock) -> Kit:
    return Kit(fakes, clock)


def _night(
    now: datetime, *, notice_said: bool = False, in_task: bool = False, asleep: bool = False
) -> None:
    """日課の状況を、今日の寝るタスク（21:00、予告は 20:50）で出す。"""
    bedtime = make_moment(BEDTIME, notice_said=notice_said)
    routine_api._publish(make_status(now, in_task=in_task, asleep=asleep, bedtime=bedtime))


def _go(kit: Kit, now: datetime) -> None:
    kit.clock.set_now(now)


def _wait_for(condition: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


# --- 予告の後に 1 回だけ話す（2.1、4.1、4.4） ---


def test_予告の前から用意し_予告を言い終えたら_1_回だけ話す(kit: Kit) -> None:
    feature = kit.feature()
    _go(kit, _at(20, 46))
    _night(_at(20, 46))

    feature.step()  # 予告の 5 分前から用意する

    assert kit.weather.called.wait(WAIT_S)
    assert kit.chat.called.wait(WAIT_S)
    settle_preparation()  # 用意を終えてから時計を進める
    assert kit.said == []

    _go(kit, _at(20, 50, 30))
    _night(_at(20, 50, 30), notice_said=True)
    feature.step()

    assert kit.said_opener() == 1
    assert FACT in kit.said
    assert kit.record().done(DAY, TalkKind.NIGHT)
    assert kit.record().last_topic is not None
    kit.assert_released()
    # 用意を二重に取りに行かない
    assert kit.weather.call_count == 1
    assert kit.chat.call_count == 1

    for _ in range(3):
        kit.clock.advance(1.0)
        feature.step()
    assert kit.said_opener() == 1


def test_起動し直しても_その日に話したお話は話さない(kit: Kit) -> None:
    _go(kit, _at(20, 51))
    _night(_at(20, 51), notice_said=True)
    kit.feature().step()
    assert kit.said_opener() == 1

    again = Kit(kit.fakes, kit.clock)
    restarted = again.feature()
    kit.clock.advance(1.0)
    restarted.step()

    assert kit.said_opener() == 1
    assert again.weather.call_count == 0
    assert again.chat.call_count == 0


def test_日課の最中は待ち_終わったら話す(kit: Kit) -> None:
    feature = kit.feature()
    _go(kit, _at(20, 50, 10))
    _night(_at(20, 50, 10), in_task=True)
    feature.step()
    assert kit.said == []

    _night(_at(20, 50, 40), notice_said=True)
    feature.step()
    assert kit.said_opener() == 1


# --- ほかの機能が借りている間は待ち、締め切りで諦める（2.4、3.6、8.4） ---


def test_ほかの機能が声を使っている間は待ち_寝るタスクの時刻で諦める(kit: Kit) -> None:
    feature = kit.feature()
    lease = kit.hold_voice()
    try:
        for second in range(5):
            now = _at(20, 51, second)
            _go(kit, now)
            _night(now, notice_said=True)
            feature.step()
        assert kit.said == []
        assert not kit.record().done(DAY, TalkKind.NIGHT)
        # 待つ間に、用意を何度も取りに行かない
        assert kit.chat.call_count == 1
        assert kit.weather.call_count == 1

        _go(kit, BEDTIME)
        _night(BEDTIME, notice_said=True)
        feature.step()
        assert kit.record().done(DAY, TalkKind.NIGHT)
    finally:
        lease.release()

    _go(kit, _at(21, 0, 5))
    _night(_at(21, 0, 5), notice_said=True)
    feature.step()
    assert kit.said == []
    data = kit.record()._store.load("record")
    assert data is not None
    assert data["days"] == {DAY.isoformat(): {"night": TalkOutcome.MISSED.value}}


def test_朝の最初のタスクが終わって_60_分たっても話せなければ諦める(kit: Kit) -> None:
    feature = kit.feature()
    wake = _at(6, 30)
    first = make_moment(_at(6, 35), ended_at=_at(7, 0))  # 予告がおやすみの間なので、後で話す
    lease = kit.hold_voice()
    try:
        for now in (_at(7, 1), _at(7, 59, 59), _at(8, 0)):
            _go(kit, now)
            routine_api._publish(make_status(now, wake_at=wake, first_task=first))
            feature.step()
    finally:
        lease.release()

    assert kit.said == []
    record = kit.record()
    assert record.done(DAY, TalkKind.MORNING)
    assert not record.done(DAY, TalkKind.NIGHT)


# --- 夜と朝の用意が重なるとき・日付が変わるとき ---


def _both(now: datetime, *, bedtime: TaskMoment | None = None) -> None:
    """夜の寝るタスクと、まだ結果のない朝の最初のタスク（後の道で一日中用意する）を出す。"""
    first = make_moment(_at(6, 35), notice_min=10)  # 予告 6:25 は起床 6:30 より前
    routine_api._publish(
        make_status(
            now,
            in_task=True,
            wake_at=_at(6, 30),
            bedtime=bedtime or make_moment(BEDTIME),
            first_task=first,
        )
    )


def test_夜と朝の両方が用意を求めても_始めるのは夜の用意だけ(kit: Kit) -> None:
    feature = kit.feature()
    for second in range(3):
        now = _at(20, 46, second)
        _go(kit, now)
        _both(now)
        feature.step()

    assert kit.weather.called.wait(WAIT_S)
    assert _wait_for(lambda: kit.chat.call_count == 1)
    time.sleep(0.05)
    # 夜（あしたの天気）だけを、1 回だけ取りに行く
    assert [call.day for call in kit.weather.calls] == [date(2026, 11, 21)]
    assert kit.chat.call_count == 1
    assert kit.said == []


def test_一方の諦めは_もう一方の用意を捨てない(kit: Kit) -> None:
    kit.weather = FakeWeather(delay_s=HOLD_S)  # 用意を取りに行ったまま返さない
    feature = kit.feature()
    try:
        _go(kit, _at(20, 0))  # 夜はまだ何もしない時刻。朝の用意だけを始める
        _both(_at(20, 0))
        feature.step()
        assert kit.weather.called.wait(WAIT_S)
        assert [call.day for call in kit.weather.calls] == [DAY]

        for second in range(3):  # 夜は寝るタスクの時刻で諦める
            now = _at(21, 0, second)
            _go(kit, now)
            _both(now)
            feature.step()

        assert kit.record().done(DAY, TalkKind.NIGHT)
        assert not kit.record().done(DAY, TalkKind.MORNING)
        time.sleep(0.05)
        # 朝の用意は捨てられず、取りに行き直さない
        assert kit.weather.call_count == 1
        assert kit.chat.call_count == 1
    finally:
        kit.weather.release()


def test_借りられずに持っていた用意は_日付が変わったら捨てて次の日に話さない(kit: Kit) -> None:
    feature = kit.feature()
    lease = kit.hold_voice()
    try:
        _go(kit, _at(20, 51))
        _night(_at(20, 51), notice_said=True)
        feature.step()
        assert kit.said == []
        assert kit.weather.call_count == 1
    finally:
        lease.release()

    next_day = datetime(2026, 11, 21, 20, 51, tzinfo=JST)
    _go(kit, next_day)
    bedtime = make_moment(datetime(2026, 11, 21, 21, 0, tzinfo=JST), notice_said=True)
    routine_api._publish(make_status(next_day, bedtime=bedtime))
    feature.step()

    # 前の日の用意（11 月 21 日の天気）は使わず、次の日の用意（22 日の天気）を取り直して話す
    assert [call.day for call in kit.weather.calls] == [date(2026, 11, 21), date(2026, 11, 22)]
    assert kit.chat.call_count == 2
    assert kit.said_opener() == 1
    record = kit.record()
    assert record.done(date(2026, 11, 21), TalkKind.NIGHT)
    assert not record.done(DAY, TalkKind.NIGHT)


# --- 記録を書けないとき（4.4） ---


def test_記録を書けなければ話さず_やり直しは_1_分に_1_回(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    feature = kit.feature()
    calls: list[TalkOutcome] = []

    def failing_mark(day: date, kind: TalkKind, outcome: TalkOutcome) -> None:
        calls.append(outcome)
        raise OSError("書けない")

    monkeypatch.setattr(feature._record, "mark", failing_mark)
    _go(kit, _at(20, 51))
    for _ in range(10):
        _night(kit.clock.now(), notice_said=True)
        feature.step()
        kit.clock.advance(1.0)

    assert kit.said == []
    assert calls == [TalkOutcome.SPOKEN]
    kit.assert_released()

    kit.clock.advance(60.0)
    _night(kit.clock.now(), notice_said=True)
    feature.step()
    assert calls == [TalkOutcome.SPOKEN, TalkOutcome.SPOKEN]


def test_諦めの記録を書けなくても_毎秒は書き直さない(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    feature = kit.feature()
    calls: list[TalkOutcome] = []

    def failing_mark(day: date, kind: TalkKind, outcome: TalkOutcome) -> None:
        calls.append(outcome)
        raise OSError("書けない")

    monkeypatch.setattr(feature._record, "mark", failing_mark)
    _go(kit, BEDTIME)
    for _ in range(10):
        _night(kit.clock.now(), notice_said=True)
        feature.step()
        kit.clock.advance(1.0)

    assert calls == [TalkOutcome.MISSED]
    assert kit.said == []


# --- 話すことがないとき（1.7） ---


def test_現在地も好きなものもなければ判定せずに待つ(kit: Kit) -> None:
    feature = kit.feature(_config(location=None, topics=[]))
    _go(kit, _at(20, 51))
    _night(_at(20, 51), notice_said=True)

    feature.step()
    _go(kit, BEDTIME)
    feature.step()

    assert kit.said == []
    assert not kit.record().done(DAY, TalkKind.NIGHT)
    assert kit.weather.call_count == 0


def test_現在地も好きなものもなければ_止めるまで待って戻る(kit: Kit) -> None:
    feature = kit.feature(_config(location=None, topics=[]))
    stop = threading.Event()
    thread = threading.Thread(target=feature.run, args=(stop,), daemon=True)
    thread.start()
    time.sleep(0.05)
    assert thread.is_alive()
    stop.set()
    thread.join(WAIT_S)
    assert not thread.is_alive()


# --- おしゃべりの回数を数えない（8.7）・モーターを使わない（8.6） ---


def test_おしゃべりの記録を読まず書かず_モーターを借りない(kit: Kit) -> None:
    _go(kit, _at(20, 51))
    _night(_at(20, 51), notice_said=True)
    kit.feature().step()

    assert kit.said_opener() == 1
    assert not kit.paths.state_dir_for("conversation").exists()
    assert kit.arbiter.holder(Resource.DRIVE) is None
    assert kit.arbiter.holder(Resource.NECK) is None


# --- 思いがけない例外（10.3） ---


def test_思いがけない例外では資源を返し_種類だけを記録して止まる(
    kit: Kit, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feature = kit.feature()

    def broken_mark(day: date, kind: TalkKind, outcome: TalkOutcome) -> None:
        raise RuntimeError(HIDDEN_ERROR)

    monkeypatch.setattr(feature._record, "mark", broken_mark)
    _go(kit, _at(20, 51))
    _night(_at(20, 51), notice_said=True)
    caplog.set_level(logging.INFO)

    with pytest.raises(RuntimeError):
        feature.run(threading.Event())

    kit.assert_released()
    assert kit.said == []
    assert "RuntimeError" in caplog.text
    assert HIDDEN_ERROR not in caplog.text


@dataclass
class _Other:
    """ほかの機能（止まらずに動き続ける）。"""

    ctx: FeatureContext
    config: FeatureConfig
    spec = FeatureSpec(
        name="other", priority=Priority.INTERACTIVE, resources=frozenset({Resource.VOICE})
    )

    def run(self, stop: threading.Event) -> None:
        stop.wait()


def test_思いがけない例外ではこの機能だけが止まる(
    kit: Kit, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken_status() -> None:
        raise RuntimeError(HIDDEN_ERROR)

    monkeypatch.setattr(routine_api, "current_status", broken_status)
    caplog.set_level(logging.DEBUG)
    supervisor = FeatureSupervisor(kit.arbiter, kit.ctx)
    supervisor.start(
        [
            FeaturePlan(TalkFeature, _config(location=None)),
            FeaturePlan(_Other, FeatureConfig()),  # type: ignore[arg-type]
        ]
    )
    try:
        assert _wait_for(lambda: supervisor.states()["talk"] is FeatureState.FAILED)
        assert _wait_for(lambda: supervisor.states()["other"] is FeatureState.RUNNING)
        time.sleep(0.05)
        assert supervisor.states()["other"] is FeatureState.RUNNING
    finally:
        supervisor.stop_all(timeout=2.0)
    kit.assert_released()
    assert "RuntimeError" in caplog.text
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert all(HIDDEN_ERROR not in message for message in messages)


# --- 出どころの選び方（12.2） ---


def _choose(config: TalkConfig) -> Sources:
    return choose_sources(config, lambda: _at(20, 0), Paths())


def test_端末の偽物が動いていれば偽の天気と偽の豆知識を使う(
    xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(talk_feature, "typed_input_active", lambda: True)

    def no_claude(paths: Paths) -> None:
        raise AssertionError("偽物の間は会話の AI を作らない")

    monkeypatch.setattr(conversation_api, "claude_chat_model", no_claude)

    sources = _choose(_config(warning_area=AREA))

    assert isinstance(sources.weather, CannedWeather)
    assert isinstance(sources.alerts, CannedAlerts)
    assert isinstance(sources.facts, CannedFacts)


def test_偽物でも現在地や好きなものがなければその出どころはない(
    xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(talk_feature, "typed_input_active", lambda: True)

    sources = _choose(_config(location=None, warning_area=AREA))
    assert sources.weather is None
    assert sources.alerts is None
    assert isinstance(sources.facts, CannedFacts)
    sources = _choose(_config(topics=[]))
    assert isinstance(sources.weather, CannedWeather)
    assert sources.alerts is None
    assert sources.facts is None


def test_ふだんは本物のアダプタとおしゃべりの口の_Claude_を使う(
    xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(talk_feature, "typed_input_active", lambda: False)
    chat = FakeChatModel([])
    asked: list[Paths] = []

    def claude(paths: Paths) -> FakeChatModel:
        asked.append(paths)
        return chat

    monkeypatch.setattr(conversation_api, "claude_chat_model", claude)

    sources = _choose(_config(warning_area=AREA))

    assert isinstance(sources.weather, OpenMeteoWeather)
    assert isinstance(sources.alerts, JmaAlerts)
    assert isinstance(sources.facts, ChatFactSource)
    assert len(asked) == 1
    assert chat.call_count == 0  # 作るだけで問い合わせない


def test_鍵がなければ豆知識はなく_地域がなければ注意報はない(
    xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(talk_feature, "typed_input_active", lambda: False)

    sources = _choose(_config())  # 鍵のファイルを置いていない

    assert isinstance(sources.weather, OpenMeteoWeather)
    assert sources.alerts is None
    assert sources.facts is None
    assert _choose(_config(location=None, warning_area=AREA, topics=[])) == Sources(
        None, None, None
    )
