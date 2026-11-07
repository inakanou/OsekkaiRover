"""おしゃべり（conversation）のテストで共通に使う偽物（10.1）。

`from conversation_fakes import ...` で読む（tests/features/conversation が import の道にある）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Arbiter, Lease
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.config import MotionSettings, VoiceSettings
from osekkai.core.display import DisplayService
from osekkai.core.events import EventBus
from osekkai.core.feature import CoreFeatureContext, FeatureSpec
from osekkai.core.idle import IdleScreen
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.quiet import QuietMode
from osekkai.core.storage import StateStore
from osekkai.core.types import Resource
from osekkai.core.voice import VoiceService
from osekkai.features.conversation.feature import ConversationFeature
from osekkai.features.conversation.ports import ChatResult, Turn

MSG_SCRIPT_EMPTY = "偽の会話の AI の台本が尽きました"


@dataclass(frozen=True)
class ChatCall:
    """偽の会話の AI が受け取った 1 回分。"""

    system: str
    turns: tuple[Turn, ...]
    timeout_s: float


class FakeChatModel:
    """偽の会話の AI。台本の結果を順に返し、受け取ったものを覚える。

    `delay_s` を決めると、返す前にその長さだけ止まる（締め切りのテスト用）。
    `release()` で止まっているのを早めに放す。`called` は呼ばれると立つ。
    """

    def __init__(self, script: Iterable[ChatResult], *, delay_s: float = 0.0) -> None:
        self._script = list(script)
        self.delay_s = delay_s
        self.calls: list[ChatCall] = []
        self.called = threading.Event()
        self._released = threading.Event()
        self._lock = threading.Lock()

    def reply(self, system: str, turns: Sequence[Turn], timeout_s: float) -> ChatResult:
        with self._lock:
            self.calls.append(ChatCall(system, tuple(turns), timeout_s))
            if not self._script:
                raise AssertionError(MSG_SCRIPT_EMPTY)
            result = self._script.pop(0)
        self.called.set()
        if self.delay_s > 0:
            self._released.wait(self.delay_s)
        return result

    def release(self) -> None:
        """止まっている呼び出し（とこの後の呼び出し）をすぐ返させる。"""
        self._released.set()


@dataclass(frozen=True)
class Shown:
    """偽の声と画面の口に出した 2 行。"""

    line1: str
    line2: str


@dataclass(frozen=True)
class Spoken:
    """偽の声と画面の口で話した言葉と、声を残すか。"""

    text: str
    keep: bool


class FakeConversationIO:
    """偽の声と画面の口（ConversationIO）。出した文字と話した言葉を順に覚える。

    `before_say` を決めると、話す前に呼ぶ（取り上げを起こす・静かにする、など）。
    `say_error` を決めると、話す代わりにその例外を投げる（取り上げや話す失敗）。
    """

    def __init__(self) -> None:
        self.events: list[Shown | Spoken] = []
        self.before_say: Callable[[str], None] | None = None
        self.say_error: Exception | None = None

    def show(self, line1: str, line2: str) -> None:
        self.events.append(Shown(line1, line2))

    def say(self, text: str, *, keep: bool) -> None:
        if self.before_say is not None:
            self.before_say(text)
        if self.say_error is not None:
            raise self.say_error
        self.events.append(Spoken(text, keep))

    @property
    def shown(self) -> list[Shown]:
        return [e for e in self.events if isinstance(e, Shown)]

    @property
    def spoken(self) -> list[Spoken]:
        return [e for e in self.events if isinstance(e, Spoken)]


class FlowKit:
    """流れを、偽のデバイスで組んだ基盤の部品（資源の調停・静かな状態・保存先）で動かす一式。

    入出力は `io_factory` で偽の声と画面の口にする。`prepare` で作った口を整えられる。
    """

    def __init__(self, fakes: FakeDeviceSet, clock: FakeClock) -> None:
        self.fakes = fakes
        self.clock = clock
        self.paths = Paths()
        self.router = ButtonRouter(fakes.buttons, clock)
        self.cache = CacheManager.from_paths(self.paths, 10, clock)
        self.voice = VoiceService(
            fakes.synth, fakes.player, self.cache, VoiceSettings(sink="speaker")
        )
        self.arbiter = Arbiter(
            clock,
            MotionService(fakes.servos, fakes.motors, MotionSettings(), clock),
            voice=self.voice,
            display=DisplayService(fakes.display),
            buttons=self.router,
        )
        self.idle = IdleScreen(self.arbiter, clock)
        self.bus = EventBus()
        self.quiet = QuietMode(
            self.idle, self.bus, StateStore.for_feature(self.paths, "core", clock), clock
        )
        self.ctx = self.context(FLOW_SPEC)
        self.ios: list[FakeConversationIO] = []
        self.leases: list[Lease] = []
        self.prepare: Callable[[FakeConversationIO], None] = lambda io: None

    def context(self, spec: FeatureSpec) -> KitContext:
        return KitContext(
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

    def io_factory(self, lease: Lease) -> FakeConversationIO:
        io = FakeConversationIO()
        self.prepare(io)
        self.ios.append(io)
        self.leases.append(lease)
        return io


class KitContext(CoreFeatureContext):
    """基盤の FeatureContext に、借りた直後に呼ぶ口（`after_acquire`）を足したもの。"""

    after_acquire: Callable[[], None] | None = None

    def acquire(
        self,
        resources: Iterable[Resource],
        *,
        wait: float = 0.0,
        on_preempt: Callable[[], None] | None = None,
    ) -> Lease:
        lease = super().acquire(resources, wait=wait, on_preempt=on_preempt)
        if self.after_acquire is not None:
            self.after_acquire()
        return lease


# 流れのテストで使う申告（機能の申告と同じもの）
FLOW_SPEC = ConversationFeature.spec
