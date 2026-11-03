"""機能の契約と、機能に渡す道具（FeatureContext）のテスト（3.1、3.2、5.2、9.2〜9.6、10.1、11.7、12.2）。"""

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import JST, FakeClock
from osekkai.core import (
    ButtonPress,
    Face,
    Feature,
    FeatureConfig,
    FeatureContext,
    FeatureSpec,
    Lease,
    Priority,
    QuietChanged,
    Resource,
    ResourceBusy,
    TimeSynced,
)
from osekkai.core.arbiter import Arbiter
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.config import MotionSettings
from osekkai.core.display import DisplayService
from osekkai.core.events import EventBus
from osekkai.core.feature import CoreFeatureContext, feature_models, validate_spec
from osekkai.core.idle import IdleScreen
from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.quiet import QuietMode
from osekkai.core.storage import StateStore

START = datetime(2026, 3, 7, 8, 5, 30, tzinfo=JST)
V = Resource.VOICE
D = Resource.DISPLAY
B = Resource.BUTTON


def _row(text: str) -> tuple[int, ...]:
    codes = to_lcd_codes(text)[:16]
    return tuple(codes) + (0x20,) * (16 - len(codes))


def _face_row(face: Face, offset: int) -> tuple[int, ...]:
    text = face.text()
    cells = len(to_lcd_codes(text))
    return _row(" " * ((16 - cells) // 2 + offset) + text)


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "条件が満たされませんでした"
        time.sleep(0.005)


class Kit:
    """道具を作るための基盤の部品一式。"""

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


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def kit(xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock) -> Iterator[Kit]:
    k = Kit(fakes, clock)
    k.router.start()
    k.bus.start()
    k.idle.start(run_thread=False)
    yield k
    k.idle.stop()
    k.bus.close()
    k.router.close()


TALK = FeatureSpec("talk", Priority.INTERACTIVE, frozenset({V, D, B}))
STARS = FeatureSpec("stars", Priority.BACKGROUND + 1, frozenset({V}))


# --- 申告 ---


def test_申告は変えられない() -> None:
    with pytest.raises(FrozenInstanceError):
        TALK.name = "other"  # type: ignore[misc]


def test_申告の設定のモデルは既定で基本の型() -> None:
    assert TALK.config_model is FeatureConfig


class StarsConfig(FeatureConfig):
    goal: int = 5


def test_申告から名前と設定のモデルの対応を作れる() -> None:
    spec = FeatureSpec("stars", Priority.BACKGROUND, frozenset({V}), StarsConfig)
    assert feature_models([TALK, spec]) == {"talk": FeatureConfig, "stars": StarsConfig}


@pytest.mark.parametrize(
    "priority",
    [Priority.BACKGROUND, Priority.BACKGROUND + 9, Priority.REMOTE + 3, Priority.SCHEDULED + 9],
)
def test_帯の値に0から9を足した優先度は正しい(priority: int) -> None:
    assert validate_spec(FeatureSpec("ok_1", priority, frozenset({V}))) == []


@pytest.mark.parametrize("priority", [0, 5, 9, 20, 29, 40, 60, 90, 95, -1])
def test_帯の外の優先度は誤り(priority: int) -> None:
    assert validate_spec(FeatureSpec("ok", priority, frozenset({V}))) != []


@pytest.mark.parametrize("name", ["", "Talk", "1talk", "_talk", "talk-time", "とけい", "talk time"])
def test_形の誤った名前は誤り(name: str) -> None:
    assert validate_spec(FeatureSpec(name, Priority.INTERACTIVE, frozenset({V}))) != []


def test_core_は予約の名前() -> None:
    assert validate_spec(FeatureSpec("core", Priority.INTERACTIVE, frozenset({V}))) != []


def test_資源のない申告は誤り() -> None:
    assert validate_spec(FeatureSpec("talk", Priority.INTERACTIVE, frozenset())) != []


def test_資源でないものを含む申告は誤り() -> None:
    spec = FeatureSpec("talk", Priority.INTERACTIVE, frozenset({"voice"}))  # type: ignore[arg-type]
    assert validate_spec(spec) != []


def test_設定のモデルが基本の型を継いでいなければ誤り() -> None:
    spec = FeatureSpec("talk", Priority.INTERACTIVE, frozenset({V}), dict)  # type: ignore[arg-type]
    assert validate_spec(spec) != []


def test_理由は誤りごとに並ぶ() -> None:
    assert len(validate_spec(FeatureSpec("core", 0, frozenset()))) == 3


def test_誤った申告では道具を作らない(kit: Kit) -> None:
    with pytest.raises(ValueError):
        kit.context(FeatureSpec("core", Priority.INTERACTIVE, frozenset({V})))


# --- 機能の型 ---


class SampleFeature:
    """契約を満たす最小の機能。"""

    spec: ClassVar[FeatureSpec] = TALK

    def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
        self.ctx = ctx
        self.config = config

    def run(self, stop: threading.Event) -> None:
        stop.wait()


def test_最小の機能が契約を満たす(kit: Kit) -> None:
    cls: type[Feature] = SampleFeature
    ctx: FeatureContext = kit.context(cls.spec)
    feature = cls(ctx, FeatureConfig())
    stop = threading.Event()
    stop.set()
    feature.run(stop)


# --- 道具の値（10.1、12.2） ---


def test_道具は名前と時計とロガーを持つ(kit: Kit) -> None:
    ctx = kit.context(TALK)
    assert ctx.name == "talk"
    assert ctx.clock is kit.clock
    assert ctx.clock.is_synchronized() == kit.clock.is_synchronized()
    assert ctx.log.name == "osekkai.features.talk"


def test_保存先はその機能の名前の下(kit: Kit, xdg_dirs: dict[str, Path]) -> None:
    ctx = kit.context(TALK)
    assert ctx.store.directory == xdg_dirs["XDG_STATE_HOME"] / "osekkai" / "talk"
    ctx.store.save("record", {"n": 1})
    assert (xdg_dirs["XDG_STATE_HOME"] / "osekkai" / "talk" / "record.json").is_file()


def test_キャッシュの置き場所はその機能の名前の下(kit: Kit, xdg_dirs: dict[str, Path]) -> None:
    ctx = kit.context(TALK)
    assert ctx.cache_dir == xdg_dirs["XDG_CACHE_HOME"] / "osekkai" / "talk"
    assert ctx.cache_dir.is_dir()


def test_2つの機能の保存先は分かれている(kit: Kit) -> None:
    a = kit.context(TALK)
    b = kit.context(STARS)
    assert a.store.directory != b.store.directory
    assert a.cache_dir != b.cache_dir
    a.store.save("record", {"n": 1})
    assert b.store.load("record") is None


# --- 資源を借りる口 ---


def test_自分の名前と優先度でリースを借りる(kit: Kit) -> None:
    ctx = kit.context(TALK)
    lease = ctx.acquire([V])
    assert isinstance(lease, Lease)
    assert lease.owner == "talk"
    assert lease.priority == Priority.INTERACTIVE
    assert kit.arbiter.holder(V) == "talk"
    lease.release()


def test_申告の外の資源は断る(kit: Kit) -> None:
    ctx = kit.context(STARS)
    with pytest.raises(ValueError):
        ctx.acquire([V, Resource.DRIVE])
    assert kit.arbiter.holder(V) is None


def test_待ちと中断の知らせを渡す(kit: Kit) -> None:
    low = kit.context(STARS)
    high = kit.context(TALK)
    called: list[bool] = []
    lease = low.acquire([V], on_preempt=lambda: called.append(True))
    with pytest.raises(ResourceBusy):
        low.acquire([V], wait=0.01)

    def give_back() -> None:
        _wait_until(lambda: called == [True])
        lease.release()

    t = threading.Thread(target=give_back)
    t.start()
    taken = high.acquire([V])
    t.join()
    assert taken.owner == "talk"


# --- ボタン（5.2） ---


def test_ボタンの待ち手は解除すると届かなくなる(kit: Kit) -> None:
    ctx = kit.context(TALK)
    got: list[ButtonPress] = []
    unregister = ctx.want_button_presses(got.append)
    kit.clock.advance(0.2)
    kit.fakes.buttons.press("usr")
    _wait_until(lambda: len(got) == 1)
    unregister()
    kit.clock.advance(0.2)
    kit.fakes.buttons.press("usr")
    time.sleep(0.05)
    assert len(got) == 1


def test_ボタンの待ち手は優先度の高い機能に届く(kit: Kit) -> None:
    low = kit.context(STARS)
    high = kit.context(TALK)
    got_low: list[ButtonPress] = []
    got_high: list[ButtonPress] = []
    low.want_button_presses(got_low.append)
    high.want_button_presses(got_high.append)
    kit.clock.advance(0.2)
    kit.fakes.buttons.press("usr")
    _wait_until(lambda: len(got_high) == 1)
    assert got_low == []


# --- 待機画面（9.2、9.3） ---


def test_短い情報が待機画面に出る(kit: Kit) -> None:
    ctx = kit.context(TALK)
    ctx.set_idle_info("ｱﾒ")
    assert kit.fakes.display.rows[0] == _row("03/07 08:05   ｱﾒ")
    ctx.set_idle_info(None)
    assert kit.fakes.display.rows[0] == _row("03/07 08:05")


def test_顔と向きが待機画面に出る(kit: Kit) -> None:
    ctx = kit.context(TALK)
    ctx.set_idle_face(Face.HAPPY, gaze=1.0)
    assert kit.fakes.display.rows[1] == _face_row(Face.HAPPY, 4)


# --- 静かな状態とイベント（9.4〜9.6、10.1） ---


def _is_quiet(ctx: FeatureContext) -> bool:
    # 性質を直に比べると、mypy が 1 回目の確かめで型を狭めてしまう
    return ctx.is_quiet


def test_静かな状態の切り替えと知らせ(kit: Kit) -> None:
    a = kit.context(TALK)
    b = kit.context(STARS)
    got: list[bool] = []
    b.subscribe(QuietChanged, lambda e: got.append(e.quiet))
    assert not a.was_quiet_before_restart
    a.request_quiet()
    assert _is_quiet(a) and _is_quiet(b)
    assert kit.quiet.is_quiet
    b.end_quiet()
    assert not _is_quiet(a)
    assert kit.bus.wait_idle(2.0)
    assert got == [True, False]


def test_再起動の前の静かな状態を確かめられる(
    kit: Kit, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    kit.context(TALK).request_quiet()
    again = Kit(fakes, clock)
    assert again.context(TALK).was_quiet_before_restart


def test_時刻が確かになった知らせを受け取れる(kit: Kit) -> None:
    ctx = kit.context(TALK)
    got: list[TimeSynced] = []
    ctx.subscribe(TimeSynced, got.append)
    event = TimeSynced()
    kit.bus.publish(event)
    assert kit.bus.wait_idle(2.0)
    assert got == [event]


# --- ほかの機能に触れない ---


def test_道具にはほかの機能に触れる口がない(kit: Kit) -> None:
    ctx = kit.context(TALK)
    public = {n for n in dir(ctx) if not n.startswith("_")}
    assert public == {
        "name",
        "clock",
        "store",
        "cache_dir",
        "log",
        "acquire",
        "want_button_presses",
        "set_idle_info",
        "set_idle_face",
        "request_quiet",
        "end_quiet",
        "is_quiet",
        "was_quiet_before_restart",
        "subscribe",
    }


def test_基盤のパッケージから機能に公開する型を読み込める() -> None:
    import osekkai.core as core

    for name in (
        "Feature",
        "FeatureSpec",
        "FeatureContext",
        "FeatureConfig",
        "Resource",
        "Priority",
        "Lease",
        "LeaseRevoked",
        "ResourceBusy",
        "DeviceUnavailable",
        "SpeechFailed",
        "Face",
        "Glyph",
        "ButtonPress",
        "QuietChanged",
        "TimeSynced",
    ):
        assert name in core.__all__
        assert hasattr(core, name)
