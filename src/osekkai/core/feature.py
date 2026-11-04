"""機能の契約（申告と型）と、機能に渡す道具。"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol, TypeVar

from osekkai.core.arbiter import Arbiter, Lease
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.clock import Clock
from osekkai.core.config import FeatureConfig
from osekkai.core.events import EventBus
from osekkai.core.idle import IdleScreen
from osekkai.core.paths import Paths
from osekkai.core.quiet import QuietMode
from osekkai.core.storage import StateStore
from osekkai.core.types import ButtonPress, Face, Priority, Resource

E = TypeVar("E")

NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]*")
RESERVED_NAMES = frozenset({"core"})
# 機能が使える帯（IDLE は基盤の内部だけ）。帯の値に 0〜9 を足してよい
FEATURE_BANDS = (Priority.BACKGROUND, Priority.REMOTE, Priority.INTERACTIVE, Priority.SCHEDULED)
BAND_STEP_MAX = 9
LOGGER_PREFIX = "osekkai.features"


@dataclass(frozen=True)
class FeatureSpec:
    """機能の申告。"""

    name: str  # 設定のトップレベルのキー、状態の保存先の名前
    priority: int  # Priority の帯の値に 0〜9 を足した値（10〜89）
    resources: frozenset[Resource]  # 使う可能性のある資源（acquire はこの中だけ）
    config_model: type[FeatureConfig] = FeatureConfig


class FeatureContext(Protocol):
    """機能に渡す道具。ほかの機能の保存先・キャッシュ・リースには触れない。"""

    name: str
    clock: Clock
    store: StateStore
    cache_dir: Path
    log: logging.Logger

    def acquire(
        self,
        resources: Iterable[Resource],
        *,
        wait: float = 0.0,
        on_preempt: Callable[[], None] | None = None,
    ) -> Lease: ...

    def want_button_presses(
        self, callback: Callable[[ButtonPress], None]
    ) -> Callable[[], None]: ...

    def set_idle_info(self, text: str | None) -> None: ...

    def set_idle_face(self, face: Face, gaze: float = 0.0) -> None: ...

    def request_quiet(self) -> None: ...

    def end_quiet(self) -> None: ...

    @property
    def is_quiet(self) -> bool: ...

    @property
    def was_quiet_before_restart(self) -> bool: ...

    def subscribe(self, event_type: type[E], callback: Callable[[E], None]) -> None: ...


class Feature(Protocol):
    """機能の型。`run()` は専用のスレッドで呼ばれ、`stop` が立ったら速やかに戻る。"""

    spec: ClassVar[FeatureSpec]

    def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None: ...

    def run(self, stop: threading.Event) -> None: ...


def _priority_ok(priority: object) -> bool:
    if not isinstance(priority, int) or isinstance(priority, bool):
        return False
    return any(0 <= priority - band <= BAND_STEP_MAX for band in FEATURE_BANDS)


def validate_spec(spec: FeatureSpec) -> list[str]:
    """申告の誤りの理由を返す。正しければ空。"""
    reasons: list[str] = []
    if not isinstance(spec.name, str) or NAME_PATTERN.fullmatch(spec.name) is None:
        reasons.append("名前は小文字で始まる英小文字・数字・下線にしてください")
    elif spec.name in RESERVED_NAMES:
        reasons.append(f"名前 {spec.name!r} は予約されています")
    if not _priority_ok(spec.priority):
        reasons.append("優先度は帯の値に 0〜9 を足した値（10〜89）にしてください")
    resources = spec.resources
    if not isinstance(resources, frozenset) or not resources:
        reasons.append("使う資源を 1 つ以上申告してください")
    elif not all(isinstance(r, Resource) for r in resources):
        reasons.append("使う資源は Resource で申告してください")
    model: object = spec.config_model  # 型の注釈に反する申告も確かめる
    if not (isinstance(model, type) and issubclass(model, FeatureConfig)):
        reasons.append("設定のモデルは FeatureConfig を継いでください")
    return reasons


def feature_models(specs: Iterable[FeatureSpec]) -> dict[str, type[FeatureConfig]]:
    """機能の名前と設定のモデルの対応（設定の読み込みに渡す）。"""
    return {spec.name: spec.config_model for spec in specs}


class CoreFeatureContext:
    """FeatureContext の実体。名前と優先度を埋めて基盤の部品に任せる。"""

    def __init__(
        self,
        spec: FeatureSpec,
        *,
        arbiter: Arbiter,
        buttons: ButtonRouter,
        idle: IdleScreen,
        quiet: QuietMode,
        bus: EventBus,
        clock: Clock,
        paths: Paths,
        cache: CacheManager,
    ) -> None:
        reasons = validate_spec(spec)
        if reasons:
            raise ValueError("; ".join(reasons))
        self._spec = spec
        self._arbiter = arbiter
        self._buttons = buttons
        self._idle = idle
        self._quiet = quiet
        self._bus = bus
        self.name = spec.name
        self.clock = clock
        self.store = StateStore.for_feature(paths, spec.name, clock)
        self.cache_dir = cache.dir_for(spec.name)
        self.log = logging.getLogger(f"{LOGGER_PREFIX}.{spec.name}")

    def acquire(
        self,
        resources: Iterable[Resource],
        *,
        wait: float = 0.0,
        on_preempt: Callable[[], None] | None = None,
    ) -> Lease:
        """申告した資源の中だけを、自分の名前と優先度で借りる。"""
        wanted = frozenset(resources)
        outside = wanted - self._spec.resources
        if outside:
            names = ", ".join(sorted(r.value for r in outside))
            raise ValueError(f"申告していない資源です: {names}")
        return self._arbiter.acquire(
            self._spec.name, self._spec.priority, wanted, wait=wait, on_preempt=on_preempt
        )

    def want_button_presses(self, callback: Callable[[ButtonPress], None]) -> Callable[[], None]:
        """ボタンの待ち手になる（5.2）。戻り値を呼ぶと解除する。"""
        return self._buttons.add_waiter(self._spec.name, self._spec.priority, callback)

    def set_idle_info(self, text: str | None) -> None:
        self._idle.set_info(text)

    def set_idle_face(self, face: Face, gaze: float = 0.0) -> None:
        self._idle.set_face(face, gaze)

    def request_quiet(self) -> None:
        self._quiet.request_quiet()

    def end_quiet(self) -> None:
        self._quiet.end_quiet()

    @property
    def is_quiet(self) -> bool:
        return self._quiet.is_quiet

    @property
    def was_quiet_before_restart(self) -> bool:
        return self._quiet.was_quiet_before_restart

    def subscribe(self, event_type: type[E], callback: Callable[[E], None]) -> None:
        """基盤の知らせを受ける。機能が終わったら外れる。"""
        self._bus.subscribe(event_type, callback, owner=self._spec.name)
