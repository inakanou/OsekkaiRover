"""日課の機能（3.5、3.8、3.9、6.2〜6.6、6.8、6.9、7.1〜7.7、9.1、9.3〜9.5、11.4）。

1 秒ごとに確かめ、始めるタスクがあれば資源を借りて `TaskRunner` で進め、結果を記録する。
なければ予告を話して記録する。資源はタスクと予告の間だけ借りる。
寝るタスクが終わるか、おやすみの時間になったら、静かな状態を求めておやすみに入る。
起床時刻になったら、声を出さずに静かな状態を終える。おやすみの間は資源を借りず、
ボタンの待ち手にもならないので、押下ではおやすみは終わらない（6.9）。
ログには、タスクの id・結果・例外の型の名前・決まった文言だけを出す
（呼び名・言葉・LCD の文字・設定の時刻は出さない）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date
from typing import ClassVar, Final

from osekkai.core.arbiter import Lease
from osekkai.core.config import FeatureConfig
from osekkai.core.feature import FeatureContext, FeatureSpec
from osekkai.core.types import Face, LeaseRevoked, Priority, Resource, ResourceBusy, SpeechFailed
from osekkai.features.routine import api
from osekkai.features.routine.calendar import DayKinds, HolidaySource, JpHolidaySource
from osekkai.features.routine.checks import ROUTINE_CHECKS
from osekkai.features.routine.config import RoutineConfig
from osekkai.features.routine.records import RecordBook, TaskResult
from osekkai.features.routine.runner import (
    LeaseIO,
    RoutineIO,
    TaskRunner,
    TaskStopped,
    Texts,
    Timings,
)
from osekkai.features.routine.schedule import DueNotice, DueTask, Phase, Scheduler
from osekkai.features.routine.texts import LCD_SLEEP, idle_info

IoFactory = Callable[[Lease], RoutineIO]

ALL_RESOURCES: Final = frozenset(Resource)
TICK_S: Final = 1.0  # 確かめの間隔（実際の秒）
ACQUIRE_WAIT_S: Final = 5.0  # 資源を借りるときに待つ上限。だめなら次の確かめでやり直す（3.8）


class _Hooks:
    """`TaskHooks` の実体。待ちの区切りごとに、止める知らせ・割り込み・予告を答える。"""

    def __init__(self, feature: RoutineFeature, running: DueTask, preempted: threading.Event):
        self._feature = feature
        self._running = running
        self._preempted = preempted

    def should_stop(self) -> bool:
        return self._feature._stop.is_set()

    def cut_in(self, phase: Phase) -> DueTask | None:
        if self._preempted.is_set():
            # 中断の知らせは来ない想定（日課より上の機能はない）。来たら取り上げと同じに扱う
            raise LeaseRevoked()
        f = self._feature
        return f._scheduler.cut_in(f._clock.now(), f._book, self._running, phase)

    def due_notice(self) -> DueNotice | None:
        f = self._feature
        return f._scheduler.due_notice(f._clock.now(), f._book)

    def notice_said(self, notice: DueNotice) -> None:
        self._feature._noticed(notice)

    def star_count_after_done(self) -> int:
        return self._feature._book.stars


class RoutineFeature:
    """日課の機能。基盤は `RoutineFeature(ctx, config)` だけで作る。

    `io_factory` は借りたリースから入出力を作る口（テストでは台本の入出力にする）。
    `holidays` は祝日の判定（既定は日本の祝日）。`sleep_show_s` はおやすみの表示を見せる秒数。
    """

    spec: ClassVar[FeatureSpec] = FeatureSpec(
        name="routine",
        priority=Priority.SCHEDULED + 5,
        resources=ALL_RESOURCES,
        config_model=RoutineConfig,
        checks=ROUTINE_CHECKS,
    )

    def __init__(
        self,
        ctx: FeatureContext,
        config: FeatureConfig,
        *,
        io_factory: IoFactory = LeaseIO,
        holidays: HolidaySource | None = None,
        sleep_show_s: float = 3.0,
    ) -> None:
        if not isinstance(config, RoutineConfig):
            raise TypeError("日課の設定ではありません")
        self._ctx = ctx
        self._clock = ctx.clock
        self._log = ctx.log
        self._config = config
        self._io_factory = io_factory
        self._sleep_show_s = sleep_show_s
        kinds = DayKinds(config.extra_holidays, holidays or JpHolidaySource())
        self._scheduler = Scheduler(config, kinds)
        self._texts = Texts(config.call_name)
        self._timings = Timings()
        self._book = RecordBook.load(ctx.store, config.keep_days, ctx.clock)
        self._stop = threading.Event()  # run() で基盤の旗に替える。tick() だけでも使える
        self._asleep = False  # 日課が求めた静かな状態か（おやすみの出入りで変わる）
        self._just_booted = True  # 時刻が確かになって最初の確かめの前か（起動の判定に使う）
        self._last_day: date | None = None  # 前回の確かめの日付（日付が変わったら整理する）
        self._shown_stars: int | None = None  # 待機画面に出した星の数

    # --- 基盤から呼ぶ口 ---

    def run(self, stop: threading.Event) -> None:
        """起動のときの準備の後、止める旗が立つまで 1 秒ごとに確かめる。"""
        self._stop = stop
        try:
            self._guarded(self._boot)
            while not stop.is_set():
                self.tick()
                stop.wait(TICK_S)
        finally:
            api._publish(None)
            self._guarded(lambda: self._ctx.set_idle_info(None))

    def tick(self) -> None:
        """1 回確かめる。中の例外は型の名前だけを記録して、次の確かめに進む（11.4）。"""
        self._guarded(self._tick)

    # --- 確かめの流れ ---

    def _boot(self) -> None:
        """起動のとき: 待機画面に星を出す。

        時刻が確かでなく、再起動の前におやすみだったら、確かになるまで静かなままにする（7.5）。
        前におやすみでなければ、確かになるまでタスクを始めずに待つ（7.6。`_tick` が待つ）。
        確かになってからの判定は、最初の確かめで行う（7.1〜7.4）。
        """
        self._just_booted = True
        self._show_stars()
        if not self._clock.is_synchronized() and self._ctx.was_quiet_before_restart:
            self._ctx.request_quiet()
            self._asleep = True
            self._log.info("時刻が確かでなく、前回おやすみだったので、静かなまま待ちます")

    def _tick(self) -> None:
        if not self._clock.is_synchronized():
            # 時刻が確かでない間は、タスクも予告も始めない（3.9、7.5、7.6）
            self._publish(in_task=False, known_time=False)
            return
        today = self._clock.now().date()
        if today != self._last_day:
            self._book.prune(today)
            self._last_day = today
        now = self._clock.now()
        just_booted, self._just_booted = self._just_booted, False
        if self._scheduler.in_sleep_period(now, self._book):
            if not self._asleep:
                if just_booted:
                    self._enter_sleep_quietly()  # 夜中に動かない（7.1、7.2）
                else:
                    self._enter_sleep_with_lease()  # 寝るタスクの窓を過ぎた（6.8）
            # おやすみの間は、タスクも予告も始めず、声も画面も出さない（6.4、6.5）
            self._show_stars()
            self._publish(in_task=False)
            return
        if self._asleep:
            self._wake()
        due = self._scheduler.due_task(now, self._book)
        if due is not None:
            self._run_tasks(due)
        else:
            notice = self._scheduler.due_notice(now, self._book)
            if notice is not None:
                self._say_notice(notice)
        self._show_stars()
        self._publish(in_task=False)

    def _run_tasks(self, due: DueTask) -> None:
        """資源を借りてタスクを進める。割り込んだタスクは、借りたまま続けて始める（5.1）。"""
        preempted = threading.Event()
        lease = self._acquire(preempted)
        if lease is None:
            return
        with lease:
            io = self._io_factory(lease)
            runner = TaskRunner(io, self._clock, self._texts, self._timings)
            next_due: DueTask | None = due
            while next_due is not None and not self._stop.is_set():
                next_due = self._run_one(runner, io, next_due, preempted)

    def _run_one(
        self, runner: TaskRunner, io: RoutineIO, due: DueTask, preempted: threading.Event
    ) -> DueTask | None:
        """1 つのタスクを進めて結果を記録する。割り込んだタスクがあれば返す。

        寝るタスクなら、どの結果で終わってもおやすみに入り、その後は何も始めない（6.2）。
        """
        task_id = due.task.id
        self._log.info("タスクを始めます: %s", task_id)
        self._publish(in_task=True)
        hooks = _Hooks(self, due, preempted)

        def on_done() -> None:
            self._book.record_outcome(due.day, task_id, TaskResult.DONE)
            self._show_stars()

        try:
            result = runner.run(due.task, hooks, on_done=on_done)
        except TaskStopped:
            # 結果を記録しないので、起動し直したら始め直す（7.7）
            self._log.info("止めるので、タスクを途中でやめました: %s", task_id)
            return None
        except LeaseRevoked:
            self._log.info("資源を取り上げられたので、タスクを中断にします: %s", task_id)
            self._book.record_outcome(due.day, task_id, TaskResult.INTERRUPTED)
            if due.task.sleep:
                self._enter_sleep(None)  # 資源がないので、表示はせずに静かにする
            return None
        except Exception as exc:
            # 記録しないと、1 秒ごとに同じタスクを始め直してしまうので、中断にする
            self._log.warning(
                "タスクの途中で失敗したので、中断にします: %s %s", task_id, type(exc).__name__
            )
            self._book.record_outcome(due.day, task_id, TaskResult.INTERRUPTED)
            if due.task.sleep:
                self._enter_sleep(io)
            return None
        # 「できた」は on_done で記録済み。もう結果があれば上書きしない
        self._book.record_outcome(due.day, task_id, result.result)
        if due.task.sleep:
            self._enter_sleep(io)
            return None
        return result.cut_by

    def _say_notice(self, notice: DueNotice) -> None:
        """資源を借りて予告を話し、記録する（3.5、3.6、3.8）。"""
        lease = self._acquire(threading.Event())
        if lease is None:
            return
        with lease:
            self._publish(in_task=True)
            io = self._io_factory(lease)
            task = notice.task
            try:
                io.backlight(True)
                io.show(task.lcd, Face.NORMAL.text())
                io.nod(1)
                if task.notice is not None:
                    try:
                        io.say(self._texts.fill(task.notice))
                    except SpeechFailed as exc:
                        self._log.warning("予告を話せませんでした: %s", type(exc).__name__)
            except LeaseRevoked:
                self._log.info("予告の途中で資源を取り上げられました: %s", task.id)
                return
            self._noticed(notice)

    def _noticed(self, notice: DueNotice) -> None:
        self._book.record_notice(notice.day, notice.task.id)
        self._log.info("予告を言いました: %s", notice.task.id)

    # --- おやすみ ---

    def _enter_sleep(self, io: RoutineIO | None) -> None:
        """おやすみに入る（6.2、6.3）。資源を持ったまま呼び、返す前に静かな状態にする。

        `io` があれば、首を下に向けて「ｵﾔｽﾐ」と眠った顔を見せる。見せられなくても静かにする。
        返す前に静かにするので、資源を返したときに待機画面が一瞬だけ光ることはない。
        """
        if io is not None:
            try:
                io.sleepy()
                io.backlight(True)
                io.show(LCD_SLEEP, Face.SLEEP.text())
                self._clock.sleep(self._sleep_show_s)
            except LeaseRevoked:
                self._log.info("おやすみの表示の途中で資源を取り上げられました")
            except Exception as exc:
                self._log.warning("おやすみの表示ができませんでした: %s", type(exc).__name__)
        self._ctx.request_quiet()
        self._asleep = True
        self._log.info("おやすみに入りました")

    def _enter_sleep_with_lease(self) -> None:
        """資源を借りておやすみの表示をしてから入る（6.8）。借りられなくても静かにする。"""
        lease = self._acquire(threading.Event())
        if lease is None:
            self._enter_sleep(None)
            return
        with lease:
            self._enter_sleep(self._io_factory(lease))

    def _enter_sleep_quietly(self) -> None:
        """起動の直後のおやすみ（7.1、7.2）。資源を借りず、表示も首の動きもしない。"""
        self._ctx.request_quiet()
        self._asleep = True
        self._log.info("起動のときがおやすみの時間なので、静かにします")

    def _wake(self) -> None:
        """起床時刻になったら、声を出さずに静かな状態を終える（6.6、7.3）。"""
        self._ctx.end_quiet()
        self._asleep = False
        self._log.info("おやすみを終えました")

    # --- 資源・待機画面・状況 ---

    def _acquire(self, preempted: threading.Event) -> Lease | None:
        """全部の資源を借りる。下位の機能は中断させる。借りられなければ None（次にやり直す）。"""
        try:
            return self._ctx.acquire(ALL_RESOURCES, wait=ACQUIRE_WAIT_S, on_preempt=preempted.set)
        except ResourceBusy:
            self._log.debug("資源が空いていないので、次の確かめでやり直します")
            return None

    def _show_stars(self) -> None:
        """待機画面の星の数を、変わったときだけ新しくする（9.1、9.2、9.3）。"""
        stars = self._book.stars
        if stars != self._shown_stars:
            self._ctx.set_idle_info(idle_info(stars))
            self._shown_stars = stars

    def _publish(self, *, in_task: bool, known_time: bool = True) -> None:
        """ほかの機能が読む状況を新しくする（9.4、9.5）。"""
        now = self._clock.now()
        next_at = self._scheduler.next_task_at(now, self._book) if known_time else None
        api._publish(api.RoutineStatus(next_at, in_task, self._asleep, now))

    def _guarded(self, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as exc:
            # 例外のメッセージには言葉や設定の値が混ざりうるので、型の名前だけを出す
            self._log.warning("日課の確かめに失敗しました: %s", type(exc).__name__)
