"""機能の監督（FeatureSupervisor）のテスト（2.2、2.3、3.1、3.4、3.5）。"""

import logging
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import ClassVar, cast

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core import Feature, FeatureConfig, FeatureContext, FeatureSpec, Priority, Resource
from osekkai.core.arbiter import Arbiter, Lease
from osekkai.core.config import MotionSettings
from osekkai.core.motion import MotionService
from osekkai.core.registry import FeaturePlan
from osekkai.core.supervisor import FeatureState, FeatureSupervisor

SECRET = "たろうくんの ひみつ"


class _Ctx:
    """テスト用の小さな道具。借りるのは Arbiter に任せる。"""

    def __init__(self, spec: FeatureSpec, arbiter: Arbiter) -> None:
        self.name = spec.name
        self.spec = spec
        self._arbiter = arbiter
        self.log = logging.getLogger(f"osekkai.features.{spec.name}")
        self.cache_dir = Path("unused")

    def acquire(self, resources: Iterable[Resource], *, wait: float = 0.0) -> Lease:
        return self._arbiter.acquire(self.name, self.spec.priority, resources, wait=wait)


class _Probe:
    """機能の中で起きたことを記録する。"""

    def __init__(self) -> None:
        self.started: dict[str, tuple[object, FeatureConfig]] = {}
        self.runs: dict[str, int] = {}
        self.running = threading.Event()
        self.lock = threading.Lock()


def make_feature(
    name: str,
    probe: _Probe,
    *,
    behavior: Callable[[FeatureContext, threading.Event], object] | None = None,
    init_error: Exception | None = None,
    resources: frozenset[Resource] = frozenset({Resource.VOICE}),
) -> type[Feature]:
    class _Feature:
        spec: ClassVar[FeatureSpec] = FeatureSpec(
            name=name, priority=Priority.SCHEDULED, resources=resources
        )

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            if init_error is not None:
                raise init_error
            self.ctx = ctx
            with probe.lock:
                probe.started[name] = (ctx, config)

        def run(self, stop: threading.Event) -> None:
            with probe.lock:
                probe.runs[name] = probe.runs.get(name, 0) + 1
            if behavior is None:
                stop.wait()
            else:
                behavior(self.ctx, stop)

    return _Feature


def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "条件が満たされませんでした"
        time.sleep(0.005)


@pytest.fixture
def arbiter(fakes: FakeDeviceSet, clock: FakeClock) -> Arbiter:
    motion = MotionService(fakes.servos, fakes.motors, MotionSettings(), clock)
    return Arbiter(clock, motion)


@pytest.fixture
def probe() -> _Probe:
    return _Probe()


@pytest.fixture
def made_contexts() -> list[FeatureSpec]:
    return []


@pytest.fixture
def supervisor(arbiter: Arbiter, made_contexts: list[FeatureSpec]) -> Iterable[FeatureSupervisor]:
    def factory(spec: FeatureSpec) -> FeatureContext:
        made_contexts.append(spec)
        return cast(FeatureContext, _Ctx(spec, arbiter))

    sup = FeatureSupervisor(arbiter, factory)
    yield sup
    sup.stop_all(2.0)


def plan(cls: type[Feature], config: FeatureConfig | None = None) -> FeaturePlan:
    return FeaturePlan(cls, config if config is not None else FeatureConfig())


# --- 開始（3.1） ---


def test_機能ごとに道具と設定を渡して動かす(
    supervisor: FeatureSupervisor, probe: _Probe, made_contexts: list[FeatureSpec]
) -> None:
    config_a = FeatureConfig()
    supervisor.start([plan(make_feature("a", probe), config_a), plan(make_feature("b", probe))])
    wait_until(
        lambda: supervisor.states() == {"a": FeatureState.RUNNING, "b": FeatureState.RUNNING}
    )
    ctx_a, got_config = probe.started["a"]
    assert isinstance(ctx_a, _Ctx)
    assert ctx_a.name == "a"
    assert got_config is config_a
    assert sorted(spec.name for spec in made_contexts) == ["a", "b"]


def test_機能はデーモンでない専用のスレッドで動く(
    supervisor: FeatureSupervisor, probe: _Probe
) -> None:
    seen: dict[str, threading.Thread] = {}

    def behavior(ctx: FeatureContext, stop: threading.Event) -> None:
        seen["thread"] = threading.current_thread()
        stop.wait()

    supervisor.start([plan(make_feature("a", probe, behavior=behavior))])
    wait_until(lambda: "thread" in seen)
    thread = seen["thread"]
    assert thread is not threading.main_thread()
    assert not thread.daemon
    assert thread.name == "osekkai-feature-a"


def test_開始の直後は開始中か動作中(supervisor: FeatureSupervisor, probe: _Probe) -> None:
    supervisor.start([plan(make_feature("a", probe))])
    assert supervisor.states()["a"] in {FeatureState.STARTING, FeatureState.RUNNING}


def test_同じ名前の機能を2度は開始しない(supervisor: FeatureSupervisor, probe: _Probe) -> None:
    feature = make_feature("a", probe)
    supervisor.start([plan(feature)])
    with pytest.raises(ValueError):
        supervisor.start([plan(feature)])


# --- 開始の失敗の隔離（3.4） ---


def test_生成で例外が出た機能だけが失敗になる(
    supervisor: FeatureSupervisor, probe: _Probe, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="osekkai")
    broken = make_feature("broken", probe, init_error=ValueError(SECRET))
    supervisor.start([plan(broken), plan(make_feature("ok", probe))])
    wait_until(lambda: supervisor.states().get("broken") is FeatureState.FAILED)
    wait_until(lambda: supervisor.states().get("ok") is FeatureState.RUNNING)
    assert "broken" not in probe.runs
    text = caplog.text
    assert "broken" in text
    assert "ValueError" in text
    assert SECRET not in text


def test_道具の生成で例外が出た機能も失敗になる(arbiter: Arbiter, probe: _Probe) -> None:
    def factory(spec: FeatureSpec) -> FeatureContext:
        if spec.name == "broken":
            raise OSError(SECRET)
        return cast(FeatureContext, _Ctx(spec, arbiter))

    sup = FeatureSupervisor(arbiter, factory)
    try:
        sup.start([plan(make_feature("broken", probe)), plan(make_feature("ok", probe))])
        wait_until(lambda: sup.states().get("broken") is FeatureState.FAILED)
        wait_until(lambda: sup.states().get("ok") is FeatureState.RUNNING)
        assert "broken" not in probe.started
    finally:
        assert sup.stop_all(2.0) == []


# --- 動作中の例外の隔離（3.5） ---


def test_動作中の例外で資源が戻りモーターが止まりほかの機能は続く(
    supervisor: FeatureSupervisor,
    probe: _Probe,
    arbiter: Arbiter,
    fakes: FakeDeviceSet,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="osekkai")
    crash = threading.Event()

    def driver(ctx: FeatureContext, stop: threading.Event) -> None:
        lease = ctx.acquire([Resource.DRIVE, Resource.VOICE])
        lease.drive.drive(0.0, 50)
        probe.running.set()
        crash.wait()
        raise RuntimeError(SECRET)

    crasher = make_feature(
        "crasher", probe, behavior=driver, resources=frozenset({Resource.DRIVE, Resource.VOICE})
    )
    survivor = make_feature("survivor", probe)
    supervisor.start([plan(crasher), plan(survivor)])
    probe.running.wait(2.0)
    assert fakes.motors.speeds == (50, 50)
    assert arbiter.holder(Resource.DRIVE) == "crasher"

    crash.set()
    wait_until(lambda: supervisor.states().get("crasher") is FeatureState.FAILED)
    assert arbiter.holder(Resource.DRIVE) is None
    assert arbiter.holder(Resource.VOICE) is None
    assert fakes.motors.speeds == (0, 0)
    assert supervisor.states()["survivor"] is FeatureState.RUNNING
    text = caplog.text
    assert "crasher" in text
    assert "RuntimeError" in text
    assert SECRET not in text


def test_失敗した機能は再起動しない(supervisor: FeatureSupervisor, probe: _Probe) -> None:
    def boom(ctx: FeatureContext, stop: threading.Event) -> None:
        raise RuntimeError("boom")

    supervisor.start([plan(make_feature("a", probe, behavior=boom))])
    wait_until(lambda: supervisor.states().get("a") is FeatureState.FAILED)
    time.sleep(0.05)
    assert probe.runs["a"] == 1
    assert supervisor.states()["a"] is FeatureState.FAILED


def test_例外の詳細はDEBUGのときだけ残す(
    supervisor: FeatureSupervisor, probe: _Probe, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="osekkai")

    def boom(ctx: FeatureContext, stop: threading.Event) -> None:
        raise RuntimeError(SECRET)

    supervisor.start([plan(make_feature("a", probe, behavior=boom))])
    wait_until(lambda: supervisor.states().get("a") is FeatureState.FAILED)
    above_debug = [r for r in caplog.records if r.levelno > logging.DEBUG]
    assert all(r.exc_info is None for r in above_debug)
    assert all(SECRET not in r.getMessage() for r in above_debug)
    assert any(r.levelno == logging.DEBUG and r.exc_info for r in caplog.records)


# --- 停止（2.2、2.3） ---


def test_停止を知らせると機能が終わり停止になる(
    supervisor: FeatureSupervisor, probe: _Probe, arbiter: Arbiter
) -> None:
    def holder(ctx: FeatureContext, stop: threading.Event) -> None:
        ctx.acquire([Resource.VOICE])  # 返し忘れ
        probe.running.set()
        stop.wait()

    supervisor.start(
        [plan(make_feature("a", probe, behavior=holder)), plan(make_feature("b", probe))]
    )
    probe.running.wait(2.0)
    assert supervisor.stop_all(2.0) == []
    assert supervisor.states() == {"a": FeatureState.STOPPED, "b": FeatureState.STOPPED}
    assert arbiter.holder(Resource.VOICE) is None


def test_応えない機能がいても上限で待つのをやめて名前を返す(
    supervisor: FeatureSupervisor, probe: _Probe, arbiter: Arbiter
) -> None:
    release = threading.Event()

    def stubborn(ctx: FeatureContext, stop: threading.Event) -> None:
        ctx.acquire([Resource.VOICE])
        probe.running.set()
        release.wait(5.0)  # stop を見ない

    supervisor.start(
        [
            plan(make_feature("stubborn", probe, behavior=stubborn)),
            plan(make_feature("stubborn2", probe, behavior=lambda c, s: release.wait(5.0))),
            plan(make_feature("polite", probe)),
        ]
    )
    probe.running.wait(2.0)
    wait_until(lambda: supervisor.states().get("polite") is FeatureState.RUNNING)
    began = time.monotonic()
    left = supervisor.stop_all(0.2)
    elapsed = time.monotonic() - began
    try:
        assert sorted(left) == ["stubborn", "stubborn2"]
        assert 0.15 <= elapsed < 0.5
        assert supervisor.states()["polite"] is FeatureState.STOPPED
        assert arbiter.holder(Resource.VOICE) is None
    finally:
        release.set()
    wait_until(lambda: supervisor.states().get("stubborn") is FeatureState.STOPPED)


# --- 終わった機能の待ち手と受け手の片付け（5.2、5.3、9.4） ---


@pytest.fixture
def ended() -> list[str]:
    return []


@pytest.fixture
def tidy_supervisor(arbiter: Arbiter, ended: list[str]) -> Iterable[FeatureSupervisor]:
    def factory(spec: FeatureSpec) -> FeatureContext:
        return cast(FeatureContext, _Ctx(spec, arbiter))

    sup = FeatureSupervisor(arbiter, factory, on_finished=ended.append)
    yield sup
    sup.stop_all(2.0)


def test_失敗した機能は終わりの片付けに名前が渡る(
    tidy_supervisor: FeatureSupervisor, probe: _Probe, ended: list[str]
) -> None:
    def boom(ctx: FeatureContext, stop: threading.Event) -> None:
        raise RuntimeError(SECRET)

    broken = make_feature("broken", probe, init_error=ValueError(SECRET))
    tidy_supervisor.start(
        [
            plan(make_feature("a", probe, behavior=boom)),
            plan(broken),
            plan(make_feature("ok", probe)),
        ]
    )
    wait_until(lambda: sorted(ended) == ["a", "broken"])
    assert tidy_supervisor.states()["ok"] is FeatureState.RUNNING


def test_正常に終わった機能も終わりの片付けに名前が渡る(
    tidy_supervisor: FeatureSupervisor, probe: _Probe, ended: list[str]
) -> None:
    tidy_supervisor.start([plan(make_feature("a", probe, behavior=lambda c, s: None))])
    wait_until(lambda: ended == ["a"])
    assert tidy_supervisor.states()["a"] is FeatureState.STOPPED


def test_見捨てた機能は待つのをやめた時点で片付けに名前が渡る(
    tidy_supervisor: FeatureSupervisor, probe: _Probe, ended: list[str]
) -> None:
    release = threading.Event()

    def stubborn(ctx: FeatureContext, stop: threading.Event) -> None:
        probe.running.set()
        release.wait(5.0)  # stop を見ない

    tidy_supervisor.start([plan(make_feature("stubborn", probe, behavior=stubborn))])
    probe.running.wait(2.0)
    try:
        assert tidy_supervisor.stop_all(0.1) == ["stubborn"]
        assert ended == ["stubborn"]
    finally:
        release.set()
    wait_until(lambda: tidy_supervisor.states().get("stubborn") is FeatureState.STOPPED)


def test_片付けの失敗は型だけを残して終わりを妨げない(
    arbiter: Arbiter, probe: _Probe, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="osekkai")

    def broken_cleanup(name: str) -> None:
        raise RuntimeError(SECRET)

    def factory(spec: FeatureSpec) -> FeatureContext:
        return cast(FeatureContext, _Ctx(spec, arbiter))

    sup = FeatureSupervisor(arbiter, factory, on_finished=broken_cleanup)
    sup.start([plan(make_feature("a", probe, behavior=lambda c, s: None))])
    wait_until(lambda: sup.states().get("a") is FeatureState.STOPPED)
    assert sup.stop_all(1.0) == []
    assert "RuntimeError" in caplog.text
    assert SECRET not in caplog.text


def test_機能がなければ停止はすぐ終わる(supervisor: FeatureSupervisor) -> None:
    supervisor.start([])
    assert supervisor.stop_all(1.0) == []
    assert supervisor.states() == {}
