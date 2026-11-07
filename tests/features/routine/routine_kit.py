"""日課の機能を、偽のデバイスで組んだ基盤の部品で動かすための部品一式（型も確かめる）。

`Kit` は `Arbiter`・`IdleScreen`・`QuietMode`・`EventBus` を偽のデバイスで組み、
`CoreFeatureContext` を作る。入出力は `io_factory` で台本の入出力（`ScriptedIO`）にする。
準備と片付けは conftest の `kit` が行う。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from scripted_io import ScriptedIO

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Arbiter, Lease
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.config import MotionSettings
from osekkai.core.display import DisplayService
from osekkai.core.events import EventBus
from osekkai.core.feature import CoreFeatureContext, FeatureSpec
from osekkai.core.idle import IdleScreen
from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.quiet import QuietMode
from osekkai.core.storage import StateStore
from osekkai.features.routine.config import RoutineConfig
from osekkai.features.routine.feature import RoutineFeature
from osekkai.features.routine.records import RecordBook


class Kit:
    """日課の機能を動かすための基盤の部品一式。"""

    def __init__(self, fakes: FakeDeviceSet, clock: FakeClock) -> None:
        self.fakes = fakes
        self.clock = clock
        self.paths = Paths()
        self.router = ButtonRouter(fakes.buttons, clock)
        motion = MotionService(fakes.servos, fakes.motors, MotionSettings(), clock)
        self.arbiter = Arbiter(
            clock, motion, display=DisplayService(fakes.display), buttons=self.router
        )
        self.idle = IdleScreen(self.arbiter, clock)
        self.bus = EventBus()
        self.quiet = QuietMode(
            self.idle, self.bus, StateStore.for_feature(self.paths, "core", clock), clock
        )
        self.cache = CacheManager.from_paths(self.paths, 10, clock)
        self.ios: list[ScriptedIO] = []
        self.prepare: Callable[[ScriptedIO], None] = lambda io: None

    def context(self, spec: FeatureSpec) -> CoreFeatureContext:
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

    def io_factory(self, lease: Lease) -> ScriptedIO:
        assert lease.active
        io = ScriptedIO(self.clock)
        self.prepare(io)
        self.ios.append(io)
        return io

    def feature(self, config: RoutineConfig, **kwargs: Any) -> RoutineFeature:
        kwargs.setdefault("io_factory", self.io_factory)
        return RoutineFeature(self.context(RoutineFeature.spec), config, **kwargs)

    def book(self, config: RoutineConfig) -> RecordBook:
        """保存先から記録を読み直す（機能が書いたものを確かめる）。"""
        store = StateStore.for_feature(self.paths, "routine", self.clock)
        return RecordBook.load(store, config.keep_days, self.clock)

    def idle_row(self) -> str:
        """画面の 1 行目（LCD の文字のコードを 1 文字ずつにしたもの。右の空白は除く）。"""
        return bytes(self.fakes.display.rows[0]).decode("latin-1").rstrip()

    def shows(self, text: str) -> bool:
        """待機画面の 1 行目の右に、その短い情報が出ているか。"""
        codes = bytes(to_lcd_codes(text)).decode("latin-1")
        return self.idle_row().endswith(" " + codes)


def move_to(clock: FakeClock, moment: datetime) -> None:
    """偽の時計をその時刻まで進める。"""
    clock.advance((moment - clock.now()).total_seconds())


def is_quiet(kit: Kit) -> bool:
    """静かな状態か（関数にして、mypy が真偽を決めつけないようにする）。"""
    return kit.quiet.is_quiet


def screen_blank(kit: Kit) -> bool:
    """画面が消え、バックライトも消えているか。"""
    kit.idle.tick()
    rows = [bytes(r).decode("latin-1").strip() for r in kit.fakes.display.rows]
    return not kit.fakes.display.backlight and rows == ["", ""]


def backlight_ons(kit: Kit, since: int) -> list[object]:
    """`since` 番目より後の画面の操作のうち、バックライトを点けたもの。"""
    calls = kit.fakes.display.calls[since:]
    return [c.args for c in calls if c.op == "set_backlight" and c.args == (True,)]


def restart_quiet(kit: Kit, *, was_quiet: bool) -> None:
    """再起動の後の静かな状態の部品にする（前回の値だけを残し、今は静かでない）。"""
    if was_quiet:
        kit.quiet.request_quiet()
    else:
        kit.quiet.end_quiet()
    kit.quiet = QuietMode(
        kit.idle, kit.bus, StateStore.for_feature(kit.paths, "core", kit.clock), kit.clock
    )
    kit.idle.set_quiet(False)
    assert kit.quiet.was_quiet_before_restart is was_quiet
