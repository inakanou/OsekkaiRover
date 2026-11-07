"""日課の 1 日の流れを通して確かめるテスト。

要件: 1.9、3.5、3.6、3.8、5.1、6.1、6.6、7.1、11.1、11.2、11.4。

偽の時計と台本の入出力で、平日（金曜）の朝から次の日（土曜・休日）の朝までを動かす。
基盤の部品（Arbiter・IdleScreen・QuietMode・EventBus）は偽のデバイスで組んだ本物を使う。
確かめは、何も起きない間は 60 秒ごと、タスクの最中は台本の入出力が時計を進める。

日付をまたぐ予告（3.6）は、別の設定で確かめる。寝るタスクがあると、0 時 5 分のタスクは
起床時刻より前なので始めず（6.5）、23 時 55 分はおやすみの時間なので予告も言わない（6.4）。
日付をまたぐ予告が言われるのは、寝るタスクのない設定（6.10）のときだけだから。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime, timedelta

import pytest
from routine_fixtures import TOKYO, build_config, task_dict
from routine_kit import Kit, backlight_ons, is_quiet, move_to, restart_quiet, screen_blank
from scripted_io import ScriptedIO

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Lease
from osekkai.core.feature import FeatureSpec
from osekkai.core.storage import StateStore
from osekkai.core.types import Face, Priority, Resource
from osekkai.features.routine.config import RoutineConfig
from osekkai.features.routine.feature import RoutineFeature
from osekkai.features.routine.records import TaskResult
from osekkai.features.routine.texts import (
    LATER_SPEECH,
    LCD_LATER,
    LCD_SLEEP,
    fill_name,
    idle_info,
)

CALL_NAME = "テストくん"  # テスト用の呼び名（個人の値ではない）
FRI = date(2026, 4, 10)  # 平日
SAT = date(2026, 4, 11)  # 休日
TALK = FeatureSpec("talk", Priority.INTERACTIVE, frozenset(Resource))  # 会話の代わり
STEP_S = 60.0  # 何も起きない間の確かめの間隔（偽の時計の秒）

# 設定の言葉と表示と時刻。ログに出ないことを確かめるので、決まったログの文言と重ならないものにする
WAKE_CALL = "{name} おきる じかんだよ"
TOILET_CALL = "トイレに いこうね"
WASH_CALL = "かおを あらおうね"
BRUSH_CALL = "はを みがこうね"
LEAVE_CALL = "{name} でかける じかんだよ"
LEAVE_NOTICE = "{name} もうすぐ でかけるよ"
SNACK_CALL = "おやつの まえに てを あらおう"
SNACK_NOTICE = "もうすぐ おやつの じかんだよ"
TOYS_CALL = "おもちゃを かたづけようね"
SLEEP_CALL = "{name} ねる じかんだよ"
NIGHT_CALL = "まだ おきてるのかな"
HOLIDAY_WAKE_CALL = "{name} きょうは やすみだね"
BREAKFAST_CALL = "あさごはんを たべようね"

DAY_TASKS = [
    task_dict(
        "wake_up", at="06:30", days="weekday", call=WAKE_CALL, praise="はやおき えらいね", lcd="ｵｷﾙ"
    ),
    task_dict(
        "toilet",
        at=None,
        after="wake_up",
        days="weekday",
        call=TOILET_CALL,
        praise="すっきり したね",
        lcd="ﾄｲﾚ",
    ),
    task_dict(
        "wash",
        at=None,
        after="toilet",
        days="weekday",
        call=WASH_CALL,
        praise="ぴかぴかに なったね",
        lcd="ｶｵ ｱﾗｳ",
        max_reminders=10,  # 押さないので、でかける時刻まで催促を続ける
    ),
    task_dict(
        "brush",
        at=None,
        after="wash",
        days="weekday",
        call=BRUSH_CALL,
        praise="はが きれいだね",
        lcd="ﾊﾐｶﾞｷ",
    ),
    task_dict(
        "leave",
        at="07:30",
        days="weekday",
        firm=True,
        call=LEAVE_CALL,
        praise="いってらっしゃいね",
        lcd="ｲｯﾃﾗｯｼｬｲ",
        notice=LEAVE_NOTICE,
        notice_min=10,
    ),
    task_dict(
        "snack",
        at="15:00",
        days="weekday",
        call=SNACK_CALL,
        praise="てが きれいに なったね",
        lcd="ﾃｱﾗｲ",
        notice=SNACK_NOTICE,
        notice_min=10,
    ),
    task_dict(
        "toys",
        at="20:30",
        days="weekday",
        call=TOYS_CALL,
        praise="おへやが きれいだね",
        lcd="ｶﾀﾂﾞｹ",
        max_reminders=10,  # 押さないので、寝る時刻まで催促を続ける
    ),
    task_dict("sleep", at="21:00", sleep=True, call=SLEEP_CALL, praise="ぐっすり ねてね", lcd="ﾈﾙ"),
    task_dict("night", at="23:00", firm=True, call=NIGHT_CALL, praise="もう ねようね", lcd="ﾖﾙ"),
    task_dict(
        "wake_up_h",
        at="08:10",
        days="holiday",
        call=HOLIDAY_WAKE_CALL,
        praise="よく ねたね",
        lcd="ｵﾊﾖｳ",
    ),
    task_dict(
        "breakfast",
        at=None,
        after="wake_up_h",
        days="holiday",
        call=BREAKFAST_CALL,
        praise="おいしかったね",
        lcd="ｱｻｺﾞﾊﾝ",
    ),
]
DAY_WAKE = {"weekday": "06:30", "holiday": "08:00"}
DAY_TIMES = ("06:30", "07:30", "15:00", "20:30", "21:00", "23:00", "08:10", "08:00")


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TOKYO)


def _filled(text: str) -> str:
    return fill_name(text, CALL_NAME)


class _Script:
    """子どもの押下の台本と、話した言葉の時刻の控え。

    `presses` は押す時刻。入出力を作るときに、その時刻の押下を入れる。
    `press_on` は、話している間に押す言葉（呼び名を入れた後の言葉）。
    """

    def __init__(self, kit: Kit, clock: FakeClock) -> None:
        self._kit = kit
        self._clock = clock
        self.presses: list[datetime] = []
        self.press_on: set[str] = set()
        self.started: list[tuple[datetime, ScriptedIO]] = []  # 入出力を作った時刻
        kit.prepare = self._prepare

    def _prepare(self, io: ScriptedIO) -> None:
        now = self._clock.now()
        self.started.append((now, io))
        for moment in self.presses:
            if moment > now:
                io.press_at((moment - now).total_seconds())
        io.press_while_saying |= self.press_on

    def said(self) -> list[tuple[datetime, str]]:
        """話した言葉を、話し始めた時刻と一緒に順に。"""
        return [
            (start + timedelta(seconds=elapsed), text)
            for start, io in self.started
            for elapsed, text in io.said_at
        ]

    def said_at(self, text: str) -> list[datetime]:
        return [moment for moment, said in self.said() if said == text]

    def run_until(self, feature: RoutineFeature, moment: datetime) -> None:
        """その時刻まで、確かめと待機画面を動かす（最後はちょうどその時刻に確かめる）。"""
        while True:
            feature.tick()
            self._kit.idle.tick()
            remaining = (moment - self._clock.now()).total_seconds()
            if remaining <= 0:
                return
            self._clock.advance(min(STEP_S, remaining))


def _boot(feature: RoutineFeature) -> None:
    feature._boot()


def _within(moments: list[datetime], start: datetime, span: timedelta) -> bool:
    return len(moments) == 1 and start <= moments[0] < start + span


def _assert_no_hidden(caplog: pytest.LogCaptureFixture, hidden_values: Iterable[str]) -> None:
    messages = [record.getMessage() for record in caplog.records]
    assert messages, "ログが集まっていません"
    for hidden in hidden_values:
        leaked = [m for m in messages if hidden in m]
        assert leaked == [], f"ログに設定の値が出ています: {leaked}"


def _day_hidden(config: RoutineConfig) -> list[str]:
    hidden_values: list[str] = [CALL_NAME, LATER_SPEECH, LCD_LATER, LCD_SLEEP, "ﾔｯﾀﾈ", *DAY_TIMES]
    for task in config.tasks:
        hidden_values += [_filled(task.call), _filled(task.praise), task.lcd]
        if task.notice is not None:
            hidden_values.append(_filled(task.notice))
    return hidden_values


def test_平日の朝から休日の朝までの1日の流れ(
    kit: Kit, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    config = build_config(DAY_TASKS, wake=DAY_WAKE, call_name=CALL_NAME)
    script = _Script(kit, clock)
    script.presses = [_at(FRI, 6, 31), _at(FRI, 6, 35)]  # 起きる・トイレは少し後に押す
    script.press_on = {
        _filled(LEAVE_CALL),
        SNACK_CALL,
        _filled(SLEEP_CALL),
        _filled(HOLIDAY_WAKE_CALL),
        BREAKFAST_CALL,
    }
    with caplog.at_level(logging.DEBUG, logger="osekkai"):
        # --- 金曜の朝: 起床時刻の前に起動して静かに待ち、起床時刻に朝の続きが始まる ---
        move_to(clock, _at(FRI, 6, 0))
        feature = kit.feature(config, sleep_show_s=3.0)
        _boot(feature)
        script.run_until(feature, _at(FRI, 6, 29))
        assert is_quiet(kit)
        assert kit.ios == []
        script.run_until(feature, _at(FRI, 7, 40))
        book = kit.book(config)
        assert book.outcome(FRI, "wake_up") is TaskResult.DONE
        assert book.outcome(FRI, "toilet") is TaskResult.DONE
        assert _within(script.said_at(_filled(WAKE_CALL)), _at(FRI, 6, 30), timedelta(minutes=1))
        assert script.said_at(TOILET_CALL)[0] > _at(FRI, 6, 31)  # 起きるの後に続く
        # 催促の最中に予告を言い（3.5、3.7）、でかけるタスクが割り込む（5.1）
        assert book.outcome(FRI, "wash") is TaskResult.INTERRUPTED
        assert book.outcome(FRI, "leave") is TaskResult.DONE
        assert len(script.said_at(WASH_CALL)) > 1  # 催促していた
        notice_at = script.said_at(_filled(LEAVE_NOTICE))
        assert _within(notice_at, _at(FRI, 7, 20), timedelta(minutes=1))
        leave_at = script.said_at(_filled(LEAVE_CALL))
        assert _within(leave_at, _at(FRI, 7, 30), timedelta(minutes=1))
        assert notice_at[0] < leave_at[0]
        assert book.notice_said(FRI, "leave")
        ended = book.ended_at(FRI, "wash")
        assert ended is not None and _at(FRI, 7, 30) <= ended < _at(FRI, 7, 31)
        # 中断したタスクの続きは、その日は始めない（3.4）
        script.run_until(feature, _at(FRI, 9, 0))
        assert kit.book(config).outcome(FRI, "brush") is None

        # --- 昼: 会話の機能が資源を持っていても、1 分以内に予告とタスクを始める（3.8） ---
        talk = kit.context(TALK)
        holders: list[Lease] = []

        def hold() -> None:
            holders.append(talk.acquire(Resource, on_preempt=lambda: holders[-1].release()))

        script.run_until(feature, _at(FRI, 14, 45))
        hold()
        script.run_until(feature, _at(FRI, 14, 56))
        assert not holders[-1].active
        assert _within(script.said_at(SNACK_NOTICE), _at(FRI, 14, 50), timedelta(minutes=1))
        hold()
        script.run_until(feature, _at(FRI, 15, 10))
        assert not holders[-1].active
        assert _within(script.said_at(SNACK_CALL), _at(FRI, 15, 0), timedelta(minutes=1))
        assert kit.book(config).outcome(FRI, "snack") is TaskResult.DONE
        assert kit.arbiter.holder(Resource.VOICE) is None

        # --- 夜: 長引くタスクに寝るタスクが割り込み（6.1）、できたでおやすみに入る ---
        script.run_until(feature, _at(FRI, 21, 10))
        book = kit.book(config)
        assert book.outcome(FRI, "toys") is TaskResult.INTERRUPTED
        assert book.outcome(FRI, "sleep") is TaskResult.DONE
        assert len(script.said_at(TOYS_CALL)) > 1
        # 寝るタスクは、長引くおもちゃの片付けを 30 分待ってから割り込む（6.1、6.11）
        assert _within(script.said_at(_filled(SLEEP_CALL)), _at(FRI, 21, 30), timedelta(minutes=1))
        ended = book.ended_at(FRI, "toys")
        assert ended is not None and _at(FRI, 21, 30) <= ended < _at(FRI, 21, 31)
        assert kit.ios[-1].calls[-1] == ("show", LCD_SLEEP, Face.SLEEP.text())
        assert is_quiet(kit)
        assert screen_blank(kit)
        assert kit.arbiter.holder(Resource.VOICE) is None

        # --- 夜中: おやすみの間のタスクは声を出さない（6.4、6.5） ---
        ios = len(kit.ios)
        since = len(kit.fakes.display.calls)
        script.run_until(feature, _at(SAT, 1, 0))
        assert len(kit.ios) == ios
        assert backlight_ons(kit, since) == []
        assert kit.book(config).outcome(FRI, "night") is None
        assert script.said_at(NIGHT_CALL) == []
        assert is_quiet(kit)

        # --- 夜中の再起動: 静かな状態と記録を引き継ぎ、表示も首の動きもなく暗いまま（7.1） ---
        restart_quiet(kit, was_quiet=True)
        servo_calls = len(kit.fakes.servos.calls)
        since = len(kit.fakes.display.calls)
        feature = kit.feature(config, sleep_show_s=3.0)  # 起動し直した機能
        _boot(feature)
        script.run_until(feature, _at(SAT, 7, 59))
        assert is_quiet(kit)
        assert len(kit.ios) == ios
        assert len(kit.fakes.servos.calls) == servo_calls
        assert backlight_ons(kit, since) == []
        assert screen_blank(kit)
        book = kit.book(config)
        assert book.stars == 5
        assert book.outcome(FRI, "sleep") is TaskResult.DONE

        # --- 土曜（休日）: 休日の起床時刻に声を出さずに戻り（6.6）、休日の続きが始まる ---
        script.run_until(feature, _at(SAT, 8, 0))
        assert not is_quiet(kit)
        assert len(kit.ios) == ios  # 声も表示もタスクの入出力も使わない
        kit.idle.tick()
        assert kit.fakes.display.backlight
        assert kit.shows(idle_info(5))
        script.run_until(feature, _at(SAT, 9, 0))
        book = kit.book(config)
        assert book.outcome(SAT, "wake_up_h") is TaskResult.DONE
        assert book.outcome(SAT, "breakfast") is TaskResult.DONE
        assert book.outcome(SAT, "wake_up") is None  # 平日のタスクは休日に始めない
        assert _within(
            script.said_at(_filled(HOLIDAY_WAKE_CALL)), _at(SAT, 8, 10), timedelta(minutes=1)
        )

    # --- 星の数は「できた」の数と同じで、記録のファイルに 2 日分がある ---
    done = [
        (day, task.id)
        for day in (FRI, SAT)
        for task in config.tasks
        if book.outcome(day, task.id) is TaskResult.DONE
    ]
    assert len(done) == 7
    assert book.stars == len(done)
    assert kit.shows(idle_info(len(done)))
    records = StateStore.for_feature(kit.paths, "routine", clock).load("records")
    assert records is not None
    days = records["days"]
    assert isinstance(days, dict)
    assert {FRI.isoformat(), SAT.isoformat()} <= set(days)
    assert script.said_at(BRUSH_CALL) == []
    # ログに呼び名・言葉・LCD の文字・設定の時刻が出ていない（1.9、11.4）
    _assert_no_hidden(caplog, _day_hidden(config))
    assert any("leave" in record.getMessage() for record in caplog.records)


MEDICINE_CALL = "{name} くすりを のもうね"
MEDICINE_NOTICE = "{name} もうすぐ くすりの じかんだよ"


def test_0時5分のタスクの予告を前の日の23時55分に話す(
    kit: Kit, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    # 寝るタスクのない設定（6.10）。起床時刻は最初の時刻のタスクの 0 時 5 分になる
    config = build_config(
        [
            task_dict(
                "medicine",
                at="00:05",
                call=MEDICINE_CALL,
                praise="ちゃんと のめたね",
                lcd="ｸｽﾘ",
                notice=MEDICINE_NOTICE,
                notice_min=10,
            )
        ],
        call_name=CALL_NAME,
    )
    script = _Script(kit, clock)
    script.press_on = {_filled(MEDICINE_CALL)}
    with caplog.at_level(logging.DEBUG, logger="osekkai"):
        move_to(clock, _at(FRI, 23, 50))
        feature = kit.feature(config)
        _boot(feature)
        script.run_until(feature, _at(SAT, 0, 20))
    notice_at = script.said_at(_filled(MEDICINE_NOTICE))
    assert _within(notice_at, _at(FRI, 23, 55), timedelta(minutes=1))
    book = kit.book(config)
    assert book.notice_said(SAT, "medicine")  # 予告はタスクの日に記録する
    assert not book.notice_said(FRI, "medicine")
    assert _within(script.said_at(_filled(MEDICINE_CALL)), _at(SAT, 0, 5), timedelta(minutes=1))
    assert book.outcome(SAT, "medicine") is TaskResult.DONE
    assert book.stars == 1
    hidden_values = [
        CALL_NAME,
        _filled(MEDICINE_CALL),
        _filled(MEDICINE_NOTICE),
        "ちゃんと のめたね",
        "ｸｽﾘ",
        "00:05",
        "23:55",
    ]
    _assert_no_hidden(caplog, hidden_values)
