"""音声の入口の機能を、本物の基盤の部品と偽のデバイスで動かす一式（テストで共通に使う）。

`from voice_bench import ...` で読む（tests/voice が import の道にある）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Arbiter
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.config import MotionSettings, VoiceSettings
from osekkai.core.display import DisplayService, LedService
from osekkai.core.events import EventBus
from osekkai.core.feature import CoreFeatureContext
from osekkai.core.idle import IdleScreen
from osekkai.core.lcd_text import center, to_lcd_codes
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.quiet import QuietMode
from osekkai.core.storage import StateStore
from osekkai.core.voice import VoiceService
from osekkai.voice import VoiceGatewayFeature
from osekkai.voice.config import VoiceConfig
from osekkai.voice.intents import Heard, IntentRegistry
from osekkai.voice.recognizer import Recognizer

TEXT = "だるまさん しよう"
PHRASES = ("だるまさん",)
WAIT_S = 5.0
# テストでは待ちを短くする（既定は資源 2 秒、待ち行列 1 秒）
ACQUIRE_WAIT_S = 0.2
JOB_POLL_S = 0.05


def wait_for(condition: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def lcd_row(text: str) -> tuple[int, ...]:
    codes = to_lcd_codes(center(text))
    return tuple(codes) + (0x20,) * (16 - len(codes))


class Bench:
    """基盤の部品を偽のデバイスで組み、入口の機能を動かす一式。"""

    def __init__(self, fakes: FakeDeviceSet, clock: FakeClock) -> None:
        self.fakes = fakes
        self.clock = clock
        self.paths = Paths()
        self.router = ButtonRouter(fakes.buttons, clock)
        cache = CacheManager.from_paths(self.paths, 10, clock)
        self.voice = VoiceService(fakes.synth, fakes.player, cache, VoiceSettings(sink="speaker"))
        self.arbiter = Arbiter(
            clock,
            MotionService(fakes.servos, fakes.motors, MotionSettings(), clock),
            voice=self.voice,
            display=DisplayService(fakes.display),
            led=LedService(fakes.led),
            buttons=self.router,
        )
        self.idle = IdleScreen(self.arbiter, clock)
        self.bus = EventBus()
        self.quiet = QuietMode(
            self.idle, self.bus, StateStore.for_feature(self.paths, "core", clock), clock
        )
        self.cache = cache
        self.registry = IntentRegistry()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.feature: VoiceGatewayFeature | None = None
        self.router.start()

    def context(self) -> CoreFeatureContext:
        return CoreFeatureContext(
            VoiceGatewayFeature.spec,
            arbiter=self.arbiter,
            buttons=self.router,
            idle=self.idle,
            quiet=self.quiet,
            bus=self.bus,
            clock=self.clock,
            paths=self.paths,
            cache=self.cache,
        )

    def make(
        self,
        recognizer: Recognizer | None,
        config: VoiceConfig | None = None,
        *,
        acquire_wait: float = ACQUIRE_WAIT_S,
    ) -> None:
        self.feature = VoiceGatewayFeature(
            self.context(),
            config or VoiceConfig(),
            recognizer=recognizer,
            registry=self.registry,
            acquire_wait=acquire_wait,
            job_poll=JOB_POLL_S,
        )

    def start(
        self,
        recognizer: Recognizer | None,
        config: VoiceConfig | None = None,
        *,
        acquire_wait: float = ACQUIRE_WAIT_S,
    ) -> None:
        self.make(recognizer, config, acquire_wait=acquire_wait)
        feature = self.feature
        assert feature is not None
        self.thread = threading.Thread(target=feature.run, args=(self.stop,), daemon=True)
        self.thread.start()
        assert wait_for(lambda: bool(self.router._waiters)), "待ち手になっていません"

    def press(self) -> None:
        """押す。前の押下と重ならないよう、時計を進めてから押す（チャタリングの除去）。"""
        self.clock.advance(1.0)
        self.fakes.buttons.press()

    def press_and_wait(self) -> None:
        self.press()
        assert self.wait_idle()

    def wait_idle(self) -> bool:
        feature = self.feature
        assert feature is not None
        return wait_for(lambda: not feature.busy)

    def sayings(self) -> list[str]:
        return list(self.fakes.synth.lines)

    def rows(self) -> list[tuple[object, ...]]:
        return [c.args for c in self.fakes.display.calls if c.op == "write_line"]

    def words(self, owner: str) -> list[Heard]:
        heard: list[Heard] = []
        self.registry.add_words(owner, 0, PHRASES, heard.append, alive=lambda: True)
        return heard

    def close(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(WAIT_S)
        self.router.close()
