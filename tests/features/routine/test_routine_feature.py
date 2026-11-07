"""日課の機能の確かめの流れのテスト（3.8、3.9、9.1、9.3、9.4、9.5、11.4）。

偽のデバイスで組んだ基盤の部品（Arbiter・IdleScreen・QuietMode・EventBus）と
`CoreFeatureContext` を使い、入出力は `io_factory` で台本の入出力に差し替える。
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from routine_fixtures import TOKYO, build_config, task_dict
from routine_kit import Kit, backlight_ons, is_quiet, move_to, restart_quiet, screen_blank
from scripted_io import ScriptedIO

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Lease
from osekkai.core.feature import FeatureSpec
from osekkai.core.types import Face, Priority, Resource
from osekkai.features.routine import api
from osekkai.features.routine import feature as routine_feature
from osekkai.features.routine.checks import ROUTINE_CHECKS
from osekkai.features.routine.config import RoutineConfig
from osekkai.features.routine.feature import RoutineFeature
from osekkai.features.routine.records import TaskResult
from osekkai.features.routine.texts import LCD_SLEEP, idle_info

TODAY = date(2026, 4, 6)
AT_7 = datetime(2026, 4, 6, 7, 0, tzinfo=TOKYO)
CALL_NAME = "テストくん"  # テスト用の呼び名（個人の値ではない）
TALK = FeatureSpec("talk", Priority.INTERACTIVE, frozenset(Resource))


def _wash(**fields: Any) -> RoutineConfig:
    return build_config([task_dict("wash", **fields)], call_name=CALL_NAME)


# --- 申告 ---


def test_申告は名前_優先度_6つの資源_設定のモデル_確認の項目() -> None:
    spec = RoutineFeature.spec
    assert spec.name == "routine"
    assert spec.priority == Priority.SCHEDULED + 5
    assert spec.resources == frozenset(Resource)
    assert spec.config_model is RoutineConfig
    assert spec.checks == ROUTINE_CHECKS


def test_基盤は設定だけで作れる(kit: Kit) -> None:
    feature = RoutineFeature(kit.context(RoutineFeature.spec), _wash())
    feature.tick()  # 06:00 は何もしない
    assert kit.ios == []


# --- タスク ---


def test_時刻になるとタスクを進めてできたを記録する(kit: Kit, clock: FakeClock) -> None:
    config = _wash()
    feature = kit.feature(config)
    kit.prepare = lambda io: io.press_at(3)
    move_to(clock, AT_7)
    feature.tick()
    assert len(kit.ios) == 1
    assert "かおを あらおう" in kit.ios[0].spoken()
    assert kit.book(config).outcome(TODAY, "wash") is TaskResult.DONE
    assert kit.arbiter.holder(Resource.VOICE) is None  # 終わったら資源を返す
    feature.tick()
    assert len(kit.ios) == 1  # 結果があるので始め直さない


def test_低い優先度の機能が資源を持っていても1分以内にタスクが始まる(
    kit: Kit, clock: FakeClock
) -> None:
    config = _wash()
    feature = kit.feature(config)
    talk_ctx = kit.context(TALK)
    holder: list[Lease] = []

    def on_preempt() -> None:
        holder[0].release()  # 会話の機能は、言い終えたら返す

    holder.append(talk_ctx.acquire(Resource, on_preempt=on_preempt))
    started: list[datetime] = []

    def prepare(io: ScriptedIO) -> None:
        started.append(clock.now())
        io.press_at(2)

    kit.prepare = prepare
    move_to(clock, AT_7)
    feature.tick()
    assert not holder[0].active
    assert started and started[0] - AT_7 < timedelta(minutes=1)
    assert kit.book(config).outcome(TODAY, "wash") is TaskResult.DONE


def test_予告も低い優先度の機能から資源を借りて話す(kit: Kit, clock: FakeClock) -> None:
    config = _wash(notice="もうすぐ かおを あらうよ")
    feature = kit.feature(config)
    talk = kit.context(TALK)
    holder: list[Lease] = []
    holder.append(talk.acquire(Resource, on_preempt=lambda: holder[0].release()))
    move_to(clock, AT_7 - timedelta(minutes=10))
    feature.tick()
    assert len(kit.ios) == 1
    io = kit.ios[0]
    assert io.spoken() == ["もうすぐ かおを あらうよ"]
    assert ("nod", 1) in io.calls
    assert any(c[0] == "show" and c[1] == "ｶｵ ｱﾗｳ" for c in io.calls)
    assert kit.book(config).notice_said(TODAY, "wash")
    feature.tick()
    assert len(kit.ios) == 1  # 1 回だけ


def test_時刻が確かでない間はタスクも予告も始めない(kit: Kit, clock: FakeClock) -> None:
    config = _wash(notice="もうすぐ かおを あらうよ")
    feature = kit.feature(config)
    clock.set_synchronized(False)
    move_to(clock, AT_7 - timedelta(minutes=5))
    feature.tick()
    move_to(clock, AT_7)
    feature.tick()
    assert kit.ios == []
    clock.set_synchronized(True)
    kit.prepare = lambda io: io.press_at(1)
    feature.tick()
    assert len(kit.ios) == 1
    assert kit.book(config).outcome(TODAY, "wash") is TaskResult.DONE


def test_資源を取り上げられたら中断を記録する(kit: Kit, clock: FakeClock) -> None:
    config = _wash()
    feature = kit.feature(config)

    def prepare(io: ScriptedIO) -> None:
        io.revoke_after = 10.0

    kit.prepare = prepare
    move_to(clock, AT_7)
    feature.tick()
    assert kit.book(config).outcome(TODAY, "wash") is TaskResult.INTERRUPTED
    assert kit.arbiter.holder(Resource.VOICE) is None


def test_割り込んだタスクを同じ確かめの中ですぐに始める(kit: Kit, clock: FakeClock) -> None:
    config = build_config(
        [
            task_dict("wash"),
            task_dict("shoes", at="07:03", firm=True, call="くつを はこう", lcd="ｸﾂ"),
        ]
    )
    feature = kit.feature(config)

    def prepare(io: ScriptedIO) -> None:
        io.press_while_saying.add("くつを はこう")

    kit.prepare = prepare
    move_to(clock, AT_7)
    feature.tick()
    book = kit.book(config)
    assert book.outcome(TODAY, "wash") is TaskResult.INTERRUPTED
    assert book.outcome(TODAY, "shoes") is TaskResult.DONE
    assert len(kit.ios) == 1  # 資源を借りたまま続ける
    ended = book.ended_at(TODAY, "wash")
    assert ended is not None and ended - AT_7 < timedelta(minutes=4)


# --- 待機画面と状況 ---


def test_待機画面に星の数を出し_できたで増やす(kit: Kit, clock: FakeClock) -> None:
    feature = kit.feature(_wash())
    feature.tick()
    assert kit.shows(idle_info(0))
    kit.prepare = lambda io: io.press_at(2)
    move_to(clock, AT_7)
    feature.tick()
    assert kit.shows(idle_info(1))


def test_状況の口が次の予定とタスクの最中を返す(kit: Kit, clock: FakeClock) -> None:
    assert api.current_status() is None
    feature = kit.feature(_wash())
    feature.tick()
    status = api.current_status()
    assert status is not None
    assert status.next_task_at == AT_7
    assert not status.in_task
    assert not status.asleep
    assert status.updated_at == clock.now()

    seen: list[api.RoutineStatus | None] = []

    def prepare(io: ScriptedIO) -> None:
        io.press_at(2)
        original = io.say

        def say(text: str) -> None:
            seen.append(api.current_status())
            original(text)

        io.say = say  # type: ignore[method-assign]

    kit.prepare = prepare
    move_to(clock, AT_7)
    feature.tick()
    assert seen and seen[0] is not None and seen[0].in_task
    after = api.current_status()
    assert after is not None and not after.in_task
    assert after.next_task_at is None  # 今日のこの後の時刻のタスクはない


def test_状況は時刻と真偽値だけを持ち_変えられない() -> None:
    status = api.RoutineStatus(None, False, False, AT_7)
    with pytest.raises(AttributeError):
        status.in_task = True  # type: ignore[misc]


# --- 例外・止める・ログ ---


def test_1回の確かめの例外は型の名前だけを記録して次の確かめに進む(
    kit: Kit, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    config = _wash()
    calls: list[int] = []

    def factory(lease: Lease) -> ScriptedIO:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("ひみつの メッセージ")
        io = ScriptedIO(clock)
        io.press_at(1)
        return io

    feature = kit.feature(config, io_factory=factory)
    move_to(clock, AT_7)
    with caplog.at_level(logging.DEBUG):
        feature.tick()
    assert "RuntimeError" in caplog.text
    assert "ひみつ" not in caplog.text
    assert kit.arbiter.holder(Resource.VOICE) is None
    feature.tick()
    assert len(calls) == 2
    assert kit.book(config).outcome(TODAY, "wash") is TaskResult.DONE


class _StopIO(ScriptedIO):
    """押下の待ちが `after` 回を超えたら、止める旗を立てる。"""

    def __init__(self, clock: FakeClock, stop: threading.Event, after: int) -> None:
        super().__init__(clock)
        self._stop = stop
        self._after = after
        self.stopped_at: float | None = None

    def wait_press(self, timeout: float) -> bool:
        if self.count("wait_press") >= self._after and not self._stop.is_set():
            self._stop.set()
            self.stopped_at = self.elapsed()
        return super().wait_press(timeout)


def test_タスクの最中に止めると速やかに戻り_結果を記録しない(kit: Kit, clock: FakeClock) -> None:
    config = _wash(ready="じゅんびを しよう")  # 準備の待ちは時間の上限がない
    stop = threading.Event()
    ios: list[_StopIO] = []

    def factory(lease: Lease) -> _StopIO:
        io = _StopIO(clock, stop, after=30)
        ios.append(io)
        return io

    feature = kit.feature(config, io_factory=factory)
    move_to(clock, AT_7)
    done = threading.Event()

    def run() -> None:
        feature.run(stop)
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert done.wait(5.0), "止めても戻りませんでした"
    io = ios[0]
    assert io.stopped_at is not None
    assert io.elapsed() - io.stopped_at <= 2.0  # 偽の時計で 1〜2 秒の中で抜ける
    assert kit.book(config).outcome(TODAY, "wash") is None  # 起動し直したら始め直す（7.7）
    assert kit.arbiter.holder(Resource.VOICE) is None
    assert api.current_status() is None


def test_止めると状況を消し_待機画面の星も消す(kit: Kit) -> None:
    feature = kit.feature(_wash())
    stop = threading.Event()
    stop.set()
    feature.run(stop)
    assert api.current_status() is None
    assert not kit.shows(idle_info(0))


def test_ログに呼び名_言葉_LCDの文字_設定の時刻を出さない(
    kit: Kit, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    config = build_config(
        [
            task_dict(
                "wash",
                at="07:10",
                call="{name} かおを あらおう",
                praise="{name} すごいね",
                notice="{name} もうすぐだよ",
                lcd="ｶｵ ｱﾗｳ",
            ),
            task_dict("brush", at=None, after="wash", call="はを みがこう", lcd="ﾊﾐｶﾞｷ"),
        ],
        call_name=CALL_NAME,
    )
    feature = kit.feature(config)
    kit.prepare = lambda io: io.press_at(400)
    with caplog.at_level(logging.DEBUG):
        move_to(clock, AT_7)
        feature.tick()  # 予告
        move_to(clock, AT_7 + timedelta(minutes=10))
        feature.tick()  # wash を催促の後に押す
        feature.tick()  # 続きの brush
    assert kit.book(config).outcome(TODAY, "brush") is TaskResult.DONE
    spoken = [s for io in kit.ios for s in io.spoken()]
    assert any(CALL_NAME in s for s in spoken)
    text = caplog.text
    for hidden in (CALL_NAME, "かおを", "すごいね", "もうすぐ", "みがこう", "ｶｵ", "ﾊﾐｶﾞｷ", "07:10"):
        assert hidden not in text
    assert "wash" in text


# --- 資源の借り方のやり直し・中断の知らせ・思いがけない例外 ---

URGENT = FeatureSpec("urgent", Priority.SCHEDULED + 9, frozenset(Resource))


def test_同じか高い優先度の機能が資源を持っていたら次の確かめでやり直す(
    kit: Kit, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routine_feature, "ACQUIRE_WAIT_S", 0.0)  # 実際の秒を待たない
    config = _wash()
    feature = kit.feature(config)
    holder = kit.context(URGENT).acquire(Resource)
    kit.prepare = lambda io: io.press_at(1)
    move_to(clock, AT_7)
    feature.tick()
    assert kit.ios == []
    assert kit.book(config).outcome(TODAY, "wash") is None
    holder.release()
    clock.advance(1)
    feature.tick()
    assert len(kit.ios) == 1
    assert kit.book(config).outcome(TODAY, "wash") is TaskResult.DONE


class _PreemptIO(ScriptedIO):
    """押下の待ちの `after` 回目に、リースの中断の知らせを呼ぶ（基盤の知らせの代わり）。"""

    def __init__(self, clock: FakeClock, lease: Lease, after: int) -> None:
        super().__init__(clock)
        self._lease = lease
        self._after = after

    def wait_press(self, timeout: float) -> bool:
        if self.count("wait_press") == self._after and self._lease.on_preempt is not None:
            self._lease.on_preempt()
        return super().wait_press(timeout)


def test_中断の知らせが来たら次の区切りで中断を記録する(kit: Kit, clock: FakeClock) -> None:
    config = _wash()
    ios: list[_PreemptIO] = []

    def factory(lease: Lease) -> _PreemptIO:
        io = _PreemptIO(clock, lease, after=2)
        ios.append(io)
        return io

    feature = kit.feature(config, io_factory=factory)
    move_to(clock, AT_7)
    feature.tick()
    assert kit.book(config).outcome(TODAY, "wash") is TaskResult.INTERRUPTED
    assert ios[0].count("wait_press") <= 4  # すぐに抜ける
    assert kit.arbiter.holder(Resource.VOICE) is None


class _BrokenIO(ScriptedIO):
    def wait_press(self, timeout: float) -> bool:
        raise RuntimeError("ひみつの メッセージ")


def test_タスクの途中の思いがけない例外は中断として記録し_始め直さない(
    kit: Kit, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    config = _wash()
    ios: list[_BrokenIO] = []

    def factory(lease: Lease) -> _BrokenIO:
        io = _BrokenIO(clock)
        ios.append(io)
        return io

    feature = kit.feature(config, io_factory=factory)
    move_to(clock, AT_7)
    with caplog.at_level(logging.DEBUG):
        feature.tick()
        clock.advance(1)
        feature.tick()
    assert len(ios) == 1
    assert kit.book(config).outcome(TODAY, "wash") is TaskResult.INTERRUPTED
    assert "RuntimeError" in caplog.text
    assert "ひみつ" not in caplog.text
    assert kit.arbiter.holder(Resource.VOICE) is None


# --- おやすみ（6.2〜6.6、6.8、6.9） ---

TOMORROW = TODAY + timedelta(days=1)
AT_19 = datetime(2026, 4, 6, 19, 0, tzinfo=TOKYO)
AT_20 = datetime(2026, 4, 6, 20, 0, tzinfo=TOKYO)
SLEEP_SPEECH = "ねようね"


def _night(*extra: dict[str, Any]) -> RoutineConfig:
    """朝のタスク、夜の長いタスク、20:00 の寝るタスク。起床時刻は 06:30。"""
    return build_config(
        [
            task_dict("wash", notice="もうすぐ かおを あらうよ"),
            task_dict(
                "toys",
                at="19:30",
                call="おもちゃを かたづけよう",
                lcd="ｶﾀﾂﾞｹ",
                max_reminders=10,
            ),
            task_dict("sleep", at="20:00", sleep=True, call=SLEEP_SPEECH, lcd="ﾈﾙ"),
            *extra,
        ],
        wake={"weekday": "06:30", "holiday": "06:30"},
        call_name=CALL_NAME,
    )


def _boot(feature: RoutineFeature) -> None:
    feature._boot()


def _asleep_status() -> bool:
    status = api.current_status()
    assert status is not None
    return status.asleep


def test_夜のタスクが長引いたら寝るタスクは30分待ってから割り込んでおやすみに入り画面が消える(
    kit: Kit, clock: FakeClock
) -> None:
    config = _night()
    feature = kit.feature(config, sleep_show_s=3.0)
    move_to(clock, AT_19)
    _boot(feature)
    feature.tick()

    def prepare(io: ScriptedIO) -> None:
        io.press_while_saying.add(SLEEP_SPEECH)

    kit.prepare = prepare
    move_to(clock, AT_20 - timedelta(minutes=30))
    since = len(kit.fakes.display.calls)
    feature.tick()
    book = kit.book(config)
    assert book.outcome(TODAY, "toys") is TaskResult.INTERRUPTED
    assert book.outcome(TODAY, "sleep") is TaskResult.DONE
    ended = book.ended_at(TODAY, "toys")
    # 6.1、6.11: 20:00 の寝るタスクは、長引くタスクを 30 分待ってから割り込む
    wait = timedelta(minutes=30)
    assert ended is not None and AT_20 + wait <= ended < AT_20 + wait + timedelta(minutes=1)
    io = kit.ios[-1]
    assert SLEEP_SPEECH in io.spoken()
    # 首を下に向け、「ｵﾔｽﾐ」と眠った顔を 3 秒見せる（6.3）
    tail = io.names()[-3:]
    assert tail[0] == "sleepy"
    assert io.calls[-1] == ("show", LCD_SLEEP, Face.SLEEP.text())
    assert 3.0 in clock.sleeps
    assert is_quiet(kit)
    assert kit.arbiter.holder(Resource.VOICE) is None
    # 資源を返す前に静かになるので、待機画面は光らない
    assert backlight_ons(kit, since) == []
    assert screen_blank(kit)
    feature.tick()
    assert _asleep_status()


def test_寝るタスクがスキップで終わってもおやすみに入る(kit: Kit, clock: FakeClock) -> None:
    config = _night()
    feature = kit.feature(config)
    move_to(clock, AT_19)
    feature.tick()
    move_to(clock, AT_20)
    feature.tick()  # 押さないので、催促・あとでの後にスキップ
    assert kit.book(config).outcome(TODAY, "sleep") is TaskResult.SKIPPED
    assert kit.ios[-1].calls[-1] == ("show", LCD_SLEEP, Face.SLEEP.text())
    assert is_quiet(kit)


def test_おやすみの間はタスクの時刻にも声を出さず_押下で起きず_起床時刻に声を出さずに戻る(
    kit: Kit, clock: FakeClock
) -> None:
    late = task_dict("late", at="21:00", firm=True, call="まだ おきてる?", lcd="ｵｷﾃﾙ")
    config = _night(late)
    feature = kit.feature(config)
    move_to(clock, AT_19)
    feature.tick()
    kit.prepare = lambda io: io.press_while_saying.add(SLEEP_SPEECH)
    move_to(clock, AT_20)
    feature.tick()
    assert is_quiet(kit)
    count = len(kit.ios)
    since = len(kit.fakes.display.calls)
    for moment in (
        datetime(2026, 4, 6, 21, 0, tzinfo=TOKYO),
        datetime(2026, 4, 6, 21, 5, tzinfo=TOKYO),
        datetime(2026, 4, 7, 6, 0, tzinfo=TOKYO),
    ):
        move_to(clock, moment)
        kit.fakes.buttons.press()  # 押してもおやすみは終わらない（6.9）
        feature.tick()
        assert is_quiet(kit)
        assert kit.arbiter.holder(Resource.VOICE) is None
        assert kit.arbiter.holder(Resource.BUTTON) is None
    assert len(kit.ios) == count  # 声も画面も使わない（6.4）
    assert backlight_ons(kit, since) == []
    assert kit.book(config).outcome(TODAY, "late") is None  # 結果は記録しない（6.5）
    assert _asleep_status()
    move_to(clock, datetime(2026, 4, 7, 6, 30, tzinfo=TOKYO))
    feature.tick()
    assert not is_quiet(kit)
    assert len(kit.ios) == count  # 声を出さずに戻る（6.6）
    assert not _asleep_status()
    kit.idle.tick()
    assert kit.fakes.display.backlight


def test_寝るタスクを始められずに窓を過ぎたら_その時点でおやすみの表示をして入る(
    kit: Kit, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routine_feature, "ACQUIRE_WAIT_S", 0.0)
    config = _night()
    feature = kit.feature(config)
    move_to(clock, AT_19)
    feature.tick()
    holder = kit.context(URGENT).acquire(Resource)
    for minutes in (30, 60):  # 夜のタスクも寝るタスクも始められない
        move_to(clock, AT_19 + timedelta(minutes=minutes))
        feature.tick()
    assert kit.ios == []
    holder.release()
    move_to(clock, AT_20 + timedelta(minutes=60))
    since = len(kit.fakes.display.calls)
    feature.tick()
    # 静かな状態を求めてから資源を返すので、待機画面が一瞬光ることはない
    assert backlight_ons(kit, since) == []
    assert kit.book(config).outcome(TODAY, "sleep") is None
    assert len(kit.ios) == 1
    io = kit.ios[0]
    assert io.spoken() == []
    assert "sleepy" in io.names()
    assert io.calls[-1] == ("show", LCD_SLEEP, Face.SLEEP.text())
    assert is_quiet(kit)
    assert kit.arbiter.holder(Resource.VOICE) is None
    assert screen_blank(kit)


def test_窓を過ぎたときに資源を借りられなくても静かになる(
    kit: Kit, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routine_feature, "ACQUIRE_WAIT_S", 0.0)
    config = _night()
    feature = kit.feature(config)
    move_to(clock, AT_19)
    feature.tick()
    holder = kit.context(URGENT).acquire(Resource)
    move_to(clock, AT_20 + timedelta(minutes=61))
    feature.tick()
    assert kit.ios == []
    assert is_quiet(kit)
    holder.release()


# --- 起動のとき（7.1〜7.7） ---


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 4, 6, 23, 0, tzinfo=TOKYO),  # 寝るタスクの窓の後（7.1）
        datetime(2026, 4, 7, 2, 0, tzinfo=TOKYO),  # 夜中
        datetime(2026, 4, 6, 6, 10, tzinfo=TOKYO),  # 起床時刻の前（7.2）
    ],
)
def test_おやすみの時間の起動では表示も首の動きもなく静かになる(
    kit: Kit, clock: FakeClock, moment: datetime
) -> None:
    move_to(clock, moment)
    config = _night()
    feature = kit.feature(config)
    servo_calls = len(kit.fakes.servos.calls)
    since = len(kit.fakes.display.calls)
    _boot(feature)
    feature.tick()
    assert is_quiet(kit)
    assert kit.ios == []  # 資源を借りない
    assert len(kit.fakes.servos.calls) == servo_calls
    assert backlight_ons(kit, since) == []
    assert screen_blank(kit)
    assert _asleep_status()


def test_時刻が確かでない起動で前回おやすみなら_確かになるまで静かなまま(
    kit: Kit, clock: FakeClock
) -> None:
    restart_quiet(kit, was_quiet=True)
    config = _night()
    feature = kit.feature(config)
    clock.set_synchronized(False)
    _boot(feature)
    assert is_quiet(kit)  # 7.5
    move_to(clock, datetime(2026, 4, 6, 9, 0, tzinfo=TOKYO))
    feature.tick()
    assert is_quiet(kit)
    assert kit.ios == []
    clock.set_synchronized(True)
    feature.tick()
    assert not is_quiet(kit)  # おやすみの時間でないので、声を出さずに戻る
    assert kit.ios == []


def test_時刻が確かでない起動で前回おやすみなら_確かになっておやすみの時間なら静かなまま(
    kit: Kit, clock: FakeClock
) -> None:
    restart_quiet(kit, was_quiet=True)
    feature = kit.feature(_night())
    clock.set_synchronized(False)
    _boot(feature)
    feature.tick()
    clock.set_synchronized(True)
    feature.tick()  # 06:00 は起床時刻の前
    assert is_quiet(kit)
    assert kit.ios == []


def test_時刻が確かでない起動で前回おやすみでなければ_待ってから判定する(
    kit: Kit, clock: FakeClock
) -> None:
    restart_quiet(kit, was_quiet=False)
    feature = kit.feature(_night())
    clock.set_synchronized(False)
    move_to(clock, datetime(2026, 4, 6, 23, 0, tzinfo=TOKYO))
    _boot(feature)
    feature.tick()
    assert not is_quiet(kit)  # 7.6: タスクを始めずに待つ
    assert kit.ios == []
    clock.set_synchronized(True)
    feature.tick()
    assert is_quiet(kit)  # おやすみの時間なので、表示なしで静かに
    assert kit.ios == []


def test_昼の起動では記録を引き継ぎ_結果のないタスクを時刻どおりに始める(
    kit: Kit, clock: FakeClock
) -> None:
    lunch = task_dict("lunch", at="12:00", call="ごはんだよ", lcd="ｺﾞﾊﾝ")
    config = _night(lunch)
    book = kit.book(config)
    book.record_outcome(TODAY, "wash", TaskResult.DONE)
    restart_quiet(kit, was_quiet=True)  # 前回おやすみでも、昼なら静かにしない
    move_to(clock, datetime(2026, 4, 6, 12, 10, tzinfo=TOKYO))
    feature = kit.feature(config)
    kit.prepare = lambda io: io.press_at(1)
    _boot(feature)
    feature.tick()
    assert not is_quiet(kit)
    assert len(kit.ios) == 1
    assert kit.ios[0].spoken()[0] == "ごはんだよ"  # wash は結果があるので始めない
    after = kit.book(config)
    assert after.outcome(TODAY, "wash") is TaskResult.DONE
    assert after.outcome(TODAY, "lunch") is TaskResult.DONE
    assert after.stars == 2
    assert kit.shows(idle_info(2))


def test_寝るタスクの窓の中の起動では_結果がなければ寝るタスクを始める(
    kit: Kit, clock: FakeClock
) -> None:
    config = _night()
    move_to(clock, AT_20 + timedelta(minutes=30))
    feature = kit.feature(config)
    kit.prepare = lambda io: io.press_while_saying.add(SLEEP_SPEECH)
    _boot(feature)
    feature.tick()
    assert len(kit.ios) == 1
    assert SLEEP_SPEECH in kit.ios[0].spoken()
    assert kit.book(config).outcome(TODAY, "sleep") is TaskResult.DONE
    assert kit.ios[0].calls[-1] == ("show", LCD_SLEEP, Face.SLEEP.text())
    assert is_quiet(kit)


def test_再起動の前に途中だったタスクは_起動の後に始め直す(kit: Kit, clock: FakeClock) -> None:
    config = _wash()
    stop = threading.Event()

    def factory(lease: Lease) -> _StopIO:
        return _StopIO(clock, stop, after=3)

    first = kit.feature(config, io_factory=factory)
    move_to(clock, AT_7)
    thread = threading.Thread(target=first.run, args=(stop,), daemon=True)
    thread.start()
    thread.join(5.0)
    assert not thread.is_alive()
    assert kit.book(config).outcome(TODAY, "wash") is None
    second = kit.feature(config)  # 起動し直し
    kit.prepare = lambda io: io.press_at(1)
    _boot(second)
    second.tick()
    assert len(kit.ios) == 1
    assert kit.book(config).outcome(TODAY, "wash") is TaskResult.DONE
