"""朝と夜のお話の機能（TalkFeature）。

要件 1.2、1.7、2.4、3.6、4.4、8.4、8.6、8.7、10.3、12.2。

1 秒ごとに、時計・静かな状態・日課の公開の状況・記録から `plan.decide` で判定し、
用意する（`Preparer.start`）・話す（`Preparer.take` から `TalkFlow.speak`）・諦める
（「話さない」を記録して用意を捨てる）を起動する。借りられなければ次の秒に判定し直す。

- 声と画面だけを申告する（8.6）。優先度は見守りより少し上（`BACKGROUND + 5`）で、
  ほかの機能が借りていれば待つ（8.4）
- 記録を書けない間は、その日のその種類のやり直しを 1 分に 1 回にする（警告を出し続けない）
- 現在地と好きなものの両方がなければ判定せずに待つ（1.7。self-check で知らせる）
- 思いがけない例外では、用意を捨て、種類だけをログに出して外へ出す。基盤が資源を取り上げ、
  この機能だけを止める（10.3）
- おしゃべりの回数の記録を読まず、書かない（8.7）

ログには決まった文言と種類の名前・例外の型の名前だけを出す（1.8）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from random import Random
from typing import ClassVar, Final

import osekkai.features.conversation.api as conversation_api
import osekkai.features.routine.api as routine_api
from osekkai.adapters.fake.console import typed_input_active
from osekkai.core.config import FeatureConfig
from osekkai.core.feature import FeatureContext, FeatureSpec
from osekkai.core.paths import Paths
from osekkai.core.types import Priority
from osekkai.features.talk import plan
from osekkai.features.talk.adapters.canned import CannedAlerts, CannedFacts, CannedWeather
from osekkai.features.talk.adapters.jma_alerts import JmaAlerts
from osekkai.features.talk.adapters.open_meteo import OpenMeteoWeather
from osekkai.features.talk.checks import TALK_CHECKS
from osekkai.features.talk.config import TalkConfig
from osekkai.features.talk.flow import RESOURCES, FlowOutcome, TalkFlow
from osekkai.features.talk.model import AlertPort, FactSource, Prepared, TalkKind, WeatherPort
from osekkai.features.talk.prepare import ChatFactSource, Preparer
from osekkai.features.talk.record import TalkOutcome, TalkRecord

logger = logging.getLogger(__name__)

OWNER: Final = "talk"
TICK_S: Final = 1.0  # 判定の間隔（実際の秒）
TAKE_WAIT_S: Final = 5.0  # 話す時に用意を待つ長さ（7.4）
RECORD_RETRY_S: Final = 60.0  # 記録を書けなかった後、同じ日・種類をやり直すまでの秒（単調な時刻）
# 話した扱いになった結果（記録済み）。直前の好きなものを記録し、用意を手放す
_SPOKEN_OUTCOMES: Final = frozenset(
    {FlowOutcome.SPOKEN, FlowOutcome.INTERRUPTED, FlowOutcome.FAILED}
)


@dataclass(frozen=True)
class Sources:
    """天気・注意報・豆知識の出どころ。使わない（使えない）ものは None。"""

    weather: WeatherPort | None
    alerts: AlertPort | None
    facts: FactSource | None


def choose_sources(config: TalkConfig, now: Callable[[], datetime], paths: Paths) -> Sources:
    """出どころを選ぶ（12.2）。

    端末の偽物が動いていれば、外へ問い合わせない偽の天気・注意報・豆知識。そうでなければ
    Open-Meteo・気象庁・おしゃべりの口の Claude。どちらでも、現在地がなければ天気も注意報も、
    注意報の地域がなければ注意報を、好きなものがなければ豆知識を使わない（1.5、1.6）。
    鍵かライブラリがなければ豆知識を使わない（10.4）。ログには決まった文言だけを出す。
    """
    location = config.location
    area = config.warning_area if location is not None else None
    if typed_input_active():
        logger.info("端末の偽物が動いているので、偽の天気と偽の豆知識を使います")
        return Sources(
            weather=CannedWeather() if location is not None else None,
            alerts=CannedAlerts() if area is not None else None,
            facts=CannedFacts() if config.topics else None,
        )
    weather = (
        OpenMeteoWeather(location.latitude, location.longitude) if location is not None else None
    )
    alerts = JmaAlerts(area.office, area.city, now) if area is not None else None
    facts: FactSource | None = None
    if config.topics:
        model = conversation_api.claude_chat_model(paths)
        if model is None:
            logger.info("会話の AI を使えないので、豆知識を話しません")
        else:
            facts = ChatFactSource(model)
    return Sources(weather, alerts, facts)


class TalkFeature:
    """朝と夜のお話の機能。基盤は `TalkFeature(ctx, config)` だけで作る。

    `sources` は出どころ（既定は `choose_sources` で最初の判定のときに選ぶ）、`rng` は好きなもの
    の選び方の乱数（テストで差し替える）。
    """

    spec: ClassVar[FeatureSpec] = FeatureSpec(
        name=OWNER,
        priority=Priority.BACKGROUND + 5,
        resources=RESOURCES,  # 声と画面だけ（8.6）
        config_model=TalkConfig,
        checks=TALK_CHECKS,
    )

    def __init__(
        self,
        ctx: FeatureContext,
        config: FeatureConfig,
        *,
        sources: Sources | None = None,
        rng: Random | None = None,
    ) -> None:
        if not isinstance(config, TalkConfig):
            raise TypeError("お話の設定ではありません")
        self._ctx = ctx
        self._config = config
        self._clock = ctx.clock
        self._record = TalkRecord(ctx.store)
        self._flow = TalkFlow(ctx, config)
        self._sources = sources
        self._rng = rng or Random()
        self._preparer: Preparer | None = None
        self._preparing: TalkKind | None = None  # 用意を始めた種類（捨てる相手を選ぶため）
        self._held: tuple[date, Prepared] | None = None  # 受け取って、まだ話せていない用意
        self._retry_at: dict[tuple[date, TalkKind], float] = {}  # 記録を書けなかった日と種類
        # 現在地と好きなものの両方がなければ話すことがない（1.7）
        self._has_content = config.location is not None or bool(config.topics)

    # --- 基盤から呼ぶ口 ---

    def run(self, stop: threading.Event) -> None:
        """止める旗が立つまで、1 秒ごとに判定する。思いがけない例外は種類だけ記録して外へ出す。"""
        if not self._has_content:
            logger.info("現在地も好きなものもないので、お話をしません")
            stop.wait()
            return
        try:
            while not stop.is_set():
                self.step()
                stop.wait(TICK_S)
        except Exception as error:  # 10.3。資源は流れと基盤が返す
            logger.error("お話で思いがけない誤りが起きたので止めます: %s", type(error).__name__)
            self._drop_all()
            raise

    def step(self) -> None:
        """1 回の判定と、その判定の起動。"""
        if not self._has_content:
            return
        now = self._clock.now()
        today = now.date()
        self._forget_other_days(today)
        decisions = plan.decide(
            now,
            self._clock.is_synchronized(),
            self._ctx.is_quiet,
            routine_api.current_status(),
            lambda kind: self._record.done(today, kind),
            self._config,
        )
        speaking = any(isinstance(d, plan.Speak) for d in decisions)
        prepared_once = False
        for decision in decisions:
            if isinstance(decision, plan.Miss):
                self._miss(decision.kind, today)
            elif isinstance(decision, plan.Speak):
                self._speak(decision, today)
            elif isinstance(decision, plan.Prepare) and not (speaking or prepared_once):
                # 用意は 1 つずつ（2 つを交互に始めると、毎秒取りに行き直すため）
                prepared_once = True
                self._prepare(decision, today)

    # --- 中の流れ ---

    def _prepare(self, decision: plan.Prepare, today: date) -> None:
        held = self._held
        if held is not None and held[1].kind is decision.kind:
            return  # 受け取った用意を持っている（話せるのを待っている）
        self._get_preparer().start(decision.kind, decision.weather_day, today)
        self._preparing = decision.kind

    def _speak(self, decision: plan.Speak, today: date) -> None:
        kind = decision.kind
        if self._waiting_retry(today, kind):
            return
        prepared = self._take(decision, today)
        outcome = self._flow.speak(
            prepared, on_started=lambda: self._record.mark(today, kind, TalkOutcome.SPOKEN)
        )
        if outcome is FlowOutcome.RECORD_FAILED:
            self._retry_at[(today, kind)] = self._clock.monotonic() + RECORD_RETRY_S
            return
        if outcome not in _SPOKEN_OUTCOMES:
            return  # 静か・借りられない。用意を持ったまま次の秒に判定し直す（8.4）
        self._held = None  # 話した後は用意を手放す（6.8）
        if prepared.topic_hash is not None:
            self._record.set_last_topic(prepared.topic_hash)

    def _take(self, decision: plan.Speak, today: date) -> Prepared:
        """持っている用意を使うか、用意を受け取る（最大 5 秒待つ）。"""
        held = self._held
        if held is not None:
            held_day, prepared = held
            if (held_day, prepared.kind, prepared.weather_day) == (
                today,
                decision.kind,
                decision.weather_day,
            ):
                return prepared
        prepared = self._get_preparer().take(
            decision.kind, decision.weather_day, today, TAKE_WAIT_S
        )
        self._preparing = None
        self._held = (today, prepared)
        return prepared

    def _miss(self, kind: TalkKind, today: date) -> None:
        """その日のその種類は話さないと記録し、用意を捨てる（2.4、3.6、8.4）。"""
        held = self._held
        if held is not None and held[1].kind is kind:
            self._held = None
        if self._preparing is kind:
            self._get_preparer().discard()
            self._preparing = None
        if self._waiting_retry(today, kind):
            return
        try:
            self._record.mark(today, kind, TalkOutcome.MISSED)
        except OSError:  # 記録の側が警告を出す。1 分は書き直さない
            self._retry_at[(today, kind)] = self._clock.monotonic() + RECORD_RETRY_S
            return
        logger.info("お話の時刻を過ぎたので、話さないことにしました: %s", kind.name)

    def _waiting_retry(self, today: date, kind: TalkKind) -> bool:
        retry_at = self._retry_at.get((today, kind))
        if retry_at is None:
            return False
        if self._clock.monotonic() < retry_at:
            return True
        del self._retry_at[(today, kind)]
        return False

    def _forget_other_days(self, today: date) -> None:
        """日付が変わったら、前の日の用意とやり直しの印を捨てる。"""
        for key in [key for key in self._retry_at if key[0] != today]:
            del self._retry_at[key]
        held = self._held
        if held is not None and held[0] != today:
            self._held = None

    def _drop_all(self) -> None:
        self._held = None
        self._preparing = None
        preparer = self._preparer
        if preparer is not None:
            preparer.discard()

    def _get_preparer(self) -> Preparer:
        """用意の係を、初めて使うときに作る（出どころもそのときに選ぶ）。"""
        preparer = self._preparer
        if preparer is None:
            sources = self._sources
            if sources is None:
                sources = choose_sources(self._config, self._clock.now, Paths())
                self._sources = sources
            preparer = Preparer(
                weather=sources.weather,
                alerts=sources.alerts,
                facts=sources.facts,
                record=self._record,
                config=self._config,
                rng=self._rng,
                monotonic=self._clock.monotonic,
            )
            self._preparer = preparer
        return preparer
