"""音声の入口の機能の本体（1.6、1.7、4.2、4.4、4.5、5.2〜5.5、6.3〜6.5、8.5）。

- 起動のときに聞き取りの準備を短命の別のスレッド（`voice-prepare`）で始める（6.5）。
  入口のスレッドはすぐに押下の待ち手になり、準備の間の押下には「じゅんび」と答える（6.3）
- 押下はボタンのスレッドで受け、待ち行列に入れてすぐに戻る。入口のスレッドが 1 つずつ行う。
  仕事が終わるまでの押下は捨てる（1.6）
- 押下の判定: 静かな状態は何もしない（5.3）。時刻が確かでない・時間帯の外は
  「おやすみ」（5.2、5.4）。準備中は「じゅんび」（6.3）、
  使えないは「おはなし できない」（6.4）、使えるなら聞く
- 返事も聞き取りも、資源を借りてから行う。借りられなければ押下を捨てる。
  中断の知らせ（日課）が来たら聞き取りを取り消し、すぐに資源を返す（1.7）
- 続けて聞く: 機能が求めたら、決めた秒数の締め切りを置いて待つ。締め切りまでの押下は
  続けての聞き取りにし、振り分けずにその機能へ渡す（4.2）。締め切り・時間帯の終わり・
  時刻が確かでない・静かな状態・その機能の登録が生きていない、のどれかで黙って待ちを終える
  （4.4、4.5、5.5）。待ち行列を待つたびに（`job_poll` 秒ごとに）確かめる
- 設定で無効なら、基盤が機能を開始しないので、待ち手にならない（8.5）
- ログには決まった文言と例外の型の名前だけを出す。言葉・表示の文字・設定の値は出さない
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time
from typing import ClassVar, Final

from osekkai.core.config import FeatureConfig
from osekkai.core.feature import FeatureContext, FeatureSpec
from osekkai.core.paths import Paths
from osekkai.core.types import (
    ButtonPress,
    DeviceUnavailable,
    Face,
    LeaseRevoked,
    Priority,
    Resource,
    ResourceBusy,
    SpeechFailed,
)
from osekkai.voice import api
from osekkai.voice.adapters.typed import TypedRecognizer
from osekkai.voice.adapters.whisper import WhisperRecognizer, whisper_available
from osekkai.voice.checks import VOICE_CHECKS
from osekkai.voice.config import VoiceConfig, model_dir
from osekkai.voice.flow import GatewayIO, LeaseGatewayIO, ListenFlow
from osekkai.voice.intents import IntentRegistry
from osekkai.voice.recognizer import Recognizer, RecognizerState
from osekkai.voice.texts import (
    LCD_SLEEP_TIME,
    SAY_PREPARING,
    SAY_SLEEP_TIME,
    SAY_UNAVAILABLE,
)

__all__ = ["VoiceGatewayFeature", "choose_recognizer", "model_dir", "within_hours"]

RESOURCES: Final = frozenset(
    {Resource.VOICE, Resource.DISPLAY, Resource.LED, Resource.BUTTON, Resource.NECK}
)
ACQUIRE_WAIT_S: Final = 2.0  # 資源を待つ長さ。だめなら押下を捨てる
JOB_POLL_S: Final = 1.0  # 待ち行列を待つ 1 回の長さ（止める旗と続けて聞くの締め切りを確かめる）
PREPARE_THREAD_NAME: Final = "voice-prepare"


def within_hours(now: time, hours: tuple[time, time]) -> bool:
    """`now` が時間帯の中か。始めは含み、終わりは含まない。始めが終わりより後なら日をまたぐ。

    始めと終わりが同じ帯は設定で断るので、ここでは空の帯とする。
    """
    start, end = hours
    if start < end:
        return start <= now < end
    if start > end:
        return now >= start or now < end
    return False


def choose_recognizer(config: VoiceConfig, paths: Paths) -> Recognizer:
    """設定から聞き取りを選ぶ（作るだけで、準備はしない）。

    - `whisper`: 実機の聞き取り。`typed`: 打った文字の聞き取り
    - `auto`: faster_whisper を読み込めれば実機の聞き取り、読み込めなければ打った文字の聞き取り。
      打った文字の聞き取りは、端末の偽物が動いていなければ「使えない」になるので、
      どちらもないときは押下に「おはなし できない」と答える
    """
    kind = config.recognizer
    if kind == "whisper" or (kind == "auto" and whisper_available()):
        return WhisperRecognizer(config, model_dir(paths, config))
    return TypedRecognizer()


@dataclass(frozen=True)
class _Job:
    """入口のスレッドで行う 1 つの仕事。

    `follow_up_owner` があれば、その機能のための続けての聞き取り（4.2）。
    """

    follow_up_owner: str | None = None


@dataclass(frozen=True)
class _FollowUp:
    """続けて聞くの待ち。`deadline` は Clock の単調な時刻（秒）。"""

    owner: str
    deadline: float


class VoiceGatewayFeature:
    """ボタンを押して聞き取り、登録した機能に言葉を渡す入口。"""

    spec: ClassVar[FeatureSpec] = FeatureSpec(
        name="voice",
        priority=Priority.INTERACTIVE + 5,
        resources=RESOURCES,
        config_model=VoiceConfig,
        checks=VOICE_CHECKS,
    )

    def __init__(
        self,
        ctx: FeatureContext,
        config: FeatureConfig,
        *,
        recognizer: Recognizer | None = None,
        registry: IntentRegistry | None = None,
        acquire_wait: float = ACQUIRE_WAIT_S,
        job_poll: float = JOB_POLL_S,
    ) -> None:
        """`recognizer` を渡さなければ設定から選ぶ（モデルの置き場所は XDG のデータの置き場所）。

        `registry` を渡さなければ、プロセスの中の登録表を使う。
        `acquire_wait`（資源を待つ秒数）と `job_poll`（待ち行列を待つ 1 回の秒数）は、
        テストで短くするためのもの。
        """
        if not isinstance(config, VoiceConfig):
            raise TypeError("設定のモデルは VoiceConfig にしてください")
        self._ctx = ctx
        self._config = config
        self._log = ctx.log
        self._recognizer = (
            recognizer if recognizer is not None else choose_recognizer(config, Paths())
        )
        self._registry = registry if registry is not None else api.registry()
        self._flow = ListenFlow(self._recognizer, self._registry, ctx.clock, self._log)
        self._jobs: queue.Queue[_Job] = queue.Queue()
        self._lock = threading.Lock()
        self._busy = False
        self._follow_up: _FollowUp | None = None
        self._acquire_wait = acquire_wait
        self._job_poll = job_poll

    @property
    def recognizer(self) -> Recognizer:
        """使う聞き取り。"""
        return self._recognizer

    @property
    def busy(self) -> bool:
        """仕事を待ち行列に入れてから終わるまでの間か（この間の押下は捨てる）。"""
        with self._lock:
            return self._busy

    @property
    def follow_up_owner(self) -> str | None:
        """続けて聞くを待っている機能の名前（待っていなければ None）。"""
        with self._lock:
            follow_up = self._follow_up
        return follow_up.owner if follow_up is not None else None

    def within_listening_hours(self) -> bool:
        """時刻が確かで、聞ける時間帯の中か（確かでなければ外として扱う。5.4）。"""
        clock = self._ctx.clock
        if not clock.is_synchronized():
            return False
        return within_hours(clock.now().time(), self._config.hours)

    def run(self, stop: threading.Event) -> None:
        self._start_prepare()
        unregister = self._ctx.want_button_presses(self._on_press)
        try:
            while not stop.is_set():
                self._between_jobs(stop)
                try:
                    job = self._jobs.get(timeout=self._job_poll)
                except queue.Empty:
                    continue
                try:
                    if not stop.is_set():
                        self._do(job, stop)
                except Exception as error:  # 1 回の仕事の誤りで入口を止めない
                    self._log.warning(
                        "押下の仕事の途中で例外がありました: %s", type(error).__name__
                    )
                finally:
                    with self._lock:
                        self._busy = False
        finally:
            unregister()

    # --- 内側 ---

    def _start_prepare(self) -> None:
        """聞き取りの準備を短命の別のスレッドで始める（6.5）。"""
        thread = threading.Thread(target=self._prepare, name=PREPARE_THREAD_NAME, daemon=True)
        thread.start()

    def _prepare(self) -> None:
        try:
            self._recognizer.prepare()
        except Exception as error:  # 型に従わない失敗も、記録だけにする
            self._log.warning("聞き取りの準備で例外がありました: %s", type(error).__name__)

    def _on_press(self, press: ButtonPress) -> None:
        """押下を受ける（ボタンのスレッド）。仕事の間なら捨てる（1.6）。"""
        del press
        now = self._ctx.clock.monotonic()
        with self._lock:
            if self._busy:
                self._log.debug("聞いている間の押下を捨てました")
                return
            self._busy = True
            # 続けて聞くの待ちは、この押下で使い切る。締め切りを過ぎていれば初めての聞き取り
            follow_up, self._follow_up = self._follow_up, None
        # 求めたばかりで、入口のスレッドがまだ取り出していない要求も、この押下で使う
        # （登録表のロックは入口のロックの外で取る）
        requested = self._registry.take_follow_up_request()
        if requested is not None:
            owner: str | None = requested
        elif follow_up is not None and now < follow_up.deadline:
            owner = follow_up.owner
        else:
            owner = None
        self._jobs.put(_Job(follow_up_owner=owner))

    def _between_jobs(self, stop: threading.Event) -> None:
        """仕事を待つたびに（`job_poll` 秒ごとに）呼ぶ。続けて聞くの要求と待ちの終わりを確かめる。

        新しい要求があれば、締め切りを「今 + 続けて聞くの秒数」にして待つ（後の要求で置き直す）。
        待ちを終えるときは何も話さない（4.4）。
        """
        del stop
        requested = self._registry.take_follow_up_request()
        now = self._ctx.clock.monotonic()
        with self._lock:
            if requested is not None:
                self._follow_up = _FollowUp(requested, now + self._config.follow_up_seconds)
                self._log.debug("続けて聞くを待ちます: %s", requested)
            follow_up = self._follow_up
        if follow_up is None:
            return
        reason = self._follow_up_end_reason(follow_up, now)
        if reason is None:
            return
        with self._lock:
            if self._follow_up is follow_up:  # 押下で使い切られていなければ終える
                self._follow_up = None
        self._log.debug("続けて聞くの待ちを終えました: %s", reason)

    def _follow_up_end_reason(self, follow_up: _FollowUp, now: float) -> str | None:
        """続けて聞くの待ちを終える理由（決まった文言）。続けてよければ None。"""
        if now >= follow_up.deadline:
            return "締め切り"  # 4.4
        if self._quiet():
            return "静かな状態"  # 5.5
        if not self.within_listening_hours():
            return "時間帯の外"  # 5.5（時刻が確かでないときも）
        if not self._registry.owner_alive(follow_up.owner):
            return "機能の登録がない"  # 4.5
        return None

    def _quiet(self) -> bool:
        """静かな状態か（読むたびに確かめる。関数にして、型の確かめが値を決めつけないようにする）。"""
        return self._ctx.is_quiet

    def _do(self, job: _Job, stop: threading.Event) -> None:
        """押下を判定して、返事をするか聞く（System Flows の図）。

        返事も聞き取りも資源を借りてから行う。借りるのを待つ間に静かな状態や時間帯が
        変わることがあるので、借りた後にもう一度判定する。
        """
        if self._quiet():
            return  # 声も画面も出さない（5.3）。資源も借りない
        preempted = threading.Event()
        try:
            lease = self._ctx.acquire(RESOURCES, wait=self._acquire_wait, on_preempt=preempted.set)
        except ResourceBusy:
            self._log.info("資源を借りられないので、押下を捨てました")
            return
        with lease:
            if self._quiet():
                return  # 待つ間に静かな状態になった（5.3）
            io = LeaseGatewayIO(lease)
            if not self.within_listening_hours():
                self._reply(io, preempted, _SLEEP_REPLY)
                return
            state = self._recognizer.state
            if state is RecognizerState.PREPARING:
                self._reply(io, preempted, _PREPARING_REPLY)
                return
            if state is not RecognizerState.READY:
                self._reply(io, preempted, _UNAVAILABLE_REPLY)
                return
            outcome = self._flow.run(
                io,
                follow_up_owner=job.follow_up_owner,
                seconds=self._config.listen_seconds,
                cancelled=lambda: preempted.is_set() or stop.is_set(),
                release=lease.release,
            )
        self._log.debug("聞き取りの結果: %s", outcome.kind.value)

    def _reply(
        self,
        io: GatewayIO,
        preempted: threading.Event,
        steps: tuple[Callable[[GatewayIO], None], ...],
    ) -> None:
        """返事を 1 つずつ行う。中断の知らせが来たら、残りを行わずに戻る（資源は呼ぶ側が返す）。"""
        for step in steps:
            if preempted.is_set():
                self._log.info("中断が来たので、返事をやめました")
                return
            try:
                step(io)
            except LeaseRevoked:
                self._log.info("返事の途中で資源を取り上げられました")
                return
            except (SpeechFailed, DeviceUnavailable) as error:
                self._log.warning("返事に失敗しました: %s", type(error).__name__)


# 返事（借りた資源で順に行う）
# 時間帯の外: 「ｵﾔｽﾐ ｼﾞｶﾝ」と眠った顔を出して、ねる じかんと話す（5.2）
_SLEEP_REPLY: Final[tuple[Callable[[GatewayIO], None], ...]] = (
    lambda io: io.show(LCD_SLEEP_TIME, Face.SLEEP.text()),
    lambda io: io.say(SAY_SLEEP_TIME),
)
_PREPARING_REPLY: Final[tuple[Callable[[GatewayIO], None], ...]] = (
    lambda io: io.say(SAY_PREPARING),  # 6.3
)
_UNAVAILABLE_REPLY: Final[tuple[Callable[[GatewayIO], None], ...]] = (
    lambda io: io.say(SAY_UNAVAILABLE),  # 6.4
)
