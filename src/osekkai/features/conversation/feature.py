"""おしゃべりの機能（ConversationFeature）。要件 1.6、2.2、6.5、7.1、9.1、9.2、10.2。

当てはまらない言葉の受け手として登録し、言葉を大きさ 1 の待ち行列に入れてすぐ戻る。
ループは 1 秒ごとに待ち行列を見て、言葉があれば 1 回の返事の流れ（ReplyFlow）に渡す。
返事・拒否・失敗を話して資源を返した後に、続けて聞くを頼む（1.6）。
押されないまま 130 秒たったやりとりは捨てる（2.2）。
ログには、決まった文言・種類の名前・例外の型の名前だけを出す（8.4）。
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import ClassVar, Final

from osekkai.adapters.fake.console import typed_input_active
from osekkai.core.config import FeatureConfig
from osekkai.core.feature import FeatureContext, FeatureSpec
from osekkai.core.paths import Paths
from osekkai.core.types import Priority
from osekkai.features.conversation.adapters.claude import ClaudeChatModel, anthropic_available
from osekkai.features.conversation.adapters.scripted import ScriptedChatModel
from osekkai.features.conversation.checks import CONVERSATION_CHECKS
from osekkai.features.conversation.config import ConversationConfig
from osekkai.features.conversation.envfile import SetupProblem, read_setup
from osekkai.features.conversation.flow import (
    RESOURCES,
    IoFactory,
    LeaseConversationIO,
    Outcome,
    ReplyFlow,
)
from osekkai.features.conversation.ports import ChatModelPort
from osekkai.features.conversation.privacy import NameGuard
from osekkai.features.conversation.session import Session
from osekkai.features.conversation.usage import DailyUsage
from osekkai.voice.api import FallbackTaken, Heard, listen_again, register_fallback

logger = logging.getLogger(__name__)

OWNER: Final = "conversation"
POLL_S: Final = 1.0  # 待ち行列を見る間隔（実際の秒）
# 資源を返した後に続けて聞くを頼む結果（返事・拒否・空・失敗を話した）
_LISTEN_AGAIN: Final = frozenset({Outcome.REPLIED, Outcome.FAILED})

ModelChooser = Callable[[], ChatModelPort | None]


def choose_chat_model(paths: Paths) -> ChatModelPort | None:
    """会話の AI を選ぶ（10.2）。どれも使えなければ None（門は鍵なしになる。9.2）。

    鍵を読め、ライブラリがあれば Claude。そうでなく端末の偽物が動いていれば台本の返事。
    ログには SetupProblem の名前か決まった文言だけを出す（鍵の値は出さない）。
    """
    found = read_setup(paths)
    if isinstance(found, SetupProblem):
        logger.info("鍵を使えません: %s", found.name)
    elif anthropic_available():
        logger.info("会話の AI に Claude を使います")
        return ClaudeChatModel(found)
    else:
        logger.info("会話のライブラリがないので Claude を使いません")
    if typed_input_active():
        logger.info("端末の偽物が動いているので、台本の返事を使います")
        return ScriptedChatModel()
    logger.info("会話の AI がないので、おしゃべりはできません")
    return None


class ConversationFeature:
    """おしゃべりの機能。基盤は `ConversationFeature(ctx, config)` だけで作る。

    `io_factory` は借りたリースから声と画面の口を作る口、`choose_model` は会話の AI を
    選ぶ口（既定は `choose_chat_model`）、`poll_s` は待ち行列を見る間隔（テストで短くする）。
    """

    spec: ClassVar[FeatureSpec] = FeatureSpec(
        name=OWNER,
        priority=Priority.INTERACTIVE,
        resources=RESOURCES,
        config_model=ConversationConfig,
        checks=CONVERSATION_CHECKS,
    )

    def __init__(
        self,
        ctx: FeatureContext,
        config: FeatureConfig,
        *,
        io_factory: IoFactory = LeaseConversationIO,
        choose_model: ModelChooser | None = None,
        poll_s: float = POLL_S,
    ) -> None:
        if not isinstance(config, ConversationConfig):
            raise TypeError("おしゃべりの設定ではありません")
        self._ctx = ctx
        self._clock = ctx.clock
        self._choose = choose_model or (lambda: choose_chat_model(Paths()))
        self._chosen = False
        self._model: ChatModelPort | None = None
        self._poll_s = poll_s
        self._heard: queue.Queue[Heard] = queue.Queue(maxsize=1)
        self._session: Session | None = None
        self._stop = threading.Event()  # run() で基盤の旗に替える
        self._flow = ReplyFlow(
            ctx,
            config,
            self._chat_model,
            DailyUsage.load(ctx.store, ctx.clock),
            NameGuard(config.call_name, config.call_name_variants),
            lambda: self._stop.is_set(),
            io_factory=io_factory,
        )

    @property
    def remembering(self) -> bool:
        """いま覚えているやりとりがあるか。"""
        return self._session is not None

    # --- 基盤から呼ぶ口 ---

    def run(self, stop: threading.Event) -> None:
        """受け手として登録し、止める旗が立つまで言葉に答える。終わりに登録を外す。"""
        self._stop = stop
        try:
            registration = register_fallback(OWNER, self._on_heard, alive=lambda: not stop.is_set())
        except FallbackTaken:
            logger.warning("ほかの受け手がいるので、おしゃべりは言葉を受けません")
            stop.wait()
            return
        try:
            while not stop.is_set():
                self.step(self._poll_s)
        finally:
            registration.remove()

    def step(self, wait_s: float) -> None:
        """待ち行列を `wait_s` 秒まで見て、言葉があれば答える。切れたやりとりは捨てる。"""
        try:
            heard: Heard | None = self._heard.get(timeout=wait_s)
        except queue.Empty:
            heard = None
        session = self._session
        if session is not None and session.expired(self._clock.monotonic()):  # 2.2
            self._session = None
            logger.info("やりとりを忘れました")
        if heard is not None:
            self._answer(heard)

    # --- 中の流れ ---

    def _on_heard(self, heard: Heard) -> None:
        """入口のスレッドから呼ばれる。待ち行列に入れてすぐ戻る。"""
        try:
            self._heard.put_nowait(heard)
        except queue.Full:
            logger.info("前の言葉に答える前なので、言葉を捨てました")

    def _answer(self, heard: Heard) -> None:
        try:
            outcome, self._session = self._flow.handle(heard, self._session)
        except Exception as error:  # 6.5。会話だけ終えて、次の言葉に進む
            logger.warning("おしゃべりで思いがけない誤りが起きました: %s", type(error).__name__)
            self._session = None
            return
        # 流れは資源を返してから戻る。その後に続けて聞くを頼む（1.6）
        if outcome in _LISTEN_AGAIN:
            listen_again(OWNER)

    def _chat_model(self) -> ChatModelPort | None:
        """会話の AI を、初めて使うときに選ぶ。"""
        if not self._chosen:
            self._model = self._choose()
            self._chosen = True
        return self._model
