"""停止の要求と基盤の異常（Runtime.run）のテスト（2.1〜2.4）。"""

import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core import FeatureConfig, FeatureContext, FeatureSpec, Priority, Resource
from osekkai.core.paths import Paths
from osekkai.core.registry import FeatureRegistry
from osekkai.core.runtime import DEFAULT_STOP_BUDGET_S, ExitCode, Runtime

WAIT_S = 3.0
SHORT_BUDGET_S = 0.2


def _wait_for(condition: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


def make_feature(name: str, started: threading.Event, release: threading.Event | None) -> type:
    """開始を知らせて停止を待つ機能。`release` を渡すと、停止に応えずそれを待つ。"""

    class _Feature:
        spec: ClassVar[FeatureSpec] = FeatureSpec(
            name=name, priority=Priority.BACKGROUND, resources=frozenset({Resource.VOICE})
        )

        def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
            pass

        def run(self, stop: threading.Event) -> None:
            started.set()
            if release is None:
                stop.wait()
            else:
                release.wait()

    _Feature.__name__ = f"Feature_{name}"
    return _Feature


@pytest.fixture
def paths(xdg_dirs: dict[str, Path]) -> Paths:
    return Paths()


@pytest.fixture
def release() -> Iterator[threading.Event]:
    """応えない機能を、テストの終わりに必ず終わらせる。"""
    event = threading.Event()
    yield event
    event.set()


@pytest.fixture
def make_runtime(paths: Paths, clock: FakeClock) -> Callable[..., Runtime]:
    def build(fakes: FakeDeviceSet, features: list[type], **kwargs: Any) -> Runtime:
        kwargs.setdefault("stop_budget", SHORT_BUDGET_S)
        return Runtime(fakes.to_devices(), paths, clock, FeatureRegistry(features), **kwargs)

    return build


class _Runner:
    """`run()` をほかのスレッドで動かす（シグナルのハンドラは置かれない）。"""

    def __init__(self, runtime: Runtime) -> None:
        self.code: ExitCode | None = None
        self.thread = threading.Thread(target=self._run, args=(runtime,), daemon=True)
        self.thread.start()

    def _run(self, runtime: Runtime) -> None:
        self.code = runtime.run()

    def join(self, timeout: float = WAIT_S) -> ExitCode | None:
        self.thread.join(timeout)
        assert not self.thread.is_alive()
        return self.code


def _stop_count(fakes: FakeDeviceSet) -> int:
    return sum(1 for c in fakes.motors.calls if c.op == "stop_now")


def assert_cleaned_up(fakes: FakeDeviceSet) -> None:
    assert fakes.motors.speeds == (0, 0)
    assert fakes.servos.angles == {"pan": 0.0, "tilt": 0.0, "steering": 0.0}
    assert fakes.led.on is False
    assert fakes.display.backlight is False
    assert fakes.display.ops()[-2:] == ["clear", "set_backlight"]


# --- 停止の要求 ---


def test_停止の要求でモーターを直ちに止め片付けて終了コード0で終える(
    fakes: FakeDeviceSet, make_runtime: Callable[..., Runtime]
) -> None:
    started = threading.Event()
    runtime = make_runtime(fakes, [make_feature("alpha", started, None)])
    runner = _Runner(runtime)
    assert started.wait(WAIT_S)
    before = _stop_count(fakes)
    runtime.request_stop()
    # 要求の呼び出しの中で、モーターが止まる（2.1）
    assert _stop_count(fakes) > before
    assert runner.join() is ExitCode.OK
    assert_cleaned_up(fakes)
    assert runtime.abandoned == []


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_シグナルから1秒以内にモーターが止まり終了コード0で終える(
    signum: int,
    fakes: FakeDeviceSet,
    make_runtime: Callable[..., Runtime],
    release: threading.Event,
) -> None:
    # 応えない機能で停止の待ちを長くし、待ちの前にモーターが止まることを見る
    started = threading.Event()
    runtime = make_runtime(fakes, [make_feature("slow", started, release)], stop_budget=1.1)
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    observed: dict[str, Any] = {}

    def send() -> None:
        if not started.wait(WAIT_S):
            runtime.request_stop()  # 止まらないままにしない
            return
        before = _stop_count(fakes)
        sent = time.monotonic()
        os.kill(os.getpid(), signum)
        observed["stopped"] = _wait_for(lambda: _stop_count(fakes) > before, 1.0)
        observed["elapsed"] = time.monotonic() - sent

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    code = runtime.run()  # メインのスレッドで動かす
    sender.join(WAIT_S)
    assert code is ExitCode.OK
    assert observed["stopped"] is True
    assert observed["elapsed"] < 1.0
    assert_cleaned_up(fakes)
    # 前のハンドラに戻す
    assert {s: signal.getsignal(s) for s in previous} == previous


def test_応えない機能は上限で待つのをやめて片付けて終える(
    fakes: FakeDeviceSet, make_runtime: Callable[..., Runtime], release: threading.Event
) -> None:
    started = threading.Event()
    runtime = make_runtime(fakes, [make_feature("stubborn", started, release)])
    runner = _Runner(runtime)
    assert started.wait(WAIT_S)
    requested = time.monotonic()
    runtime.request_stop()
    assert runner.join() is ExitCode.OK
    elapsed = time.monotonic() - requested
    cleanup = elapsed - SHORT_BUDGET_S
    assert elapsed >= SHORT_BUDGET_S
    # 既定の上限でも、上限と片付けの時間の和が 10 秒以内に収まる（2.2、2.3）
    assert DEFAULT_STOP_BUDGET_S + cleanup < 10.0
    assert cleanup < 1.0
    assert runtime.abandoned == ["stubborn"]
    assert_cleaned_up(fakes)


def test_停止の待ちの上限の既定は8秒(paths: Paths, clock: FakeClock, fakes: FakeDeviceSet) -> None:
    runtime = Runtime(fakes.to_devices(), paths, clock, FeatureRegistry([]))
    assert runtime._stop_budget == DEFAULT_STOP_BUDGET_S == 8.0


# --- 基盤の異常 ---


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_基盤のスレッドの異常で片付けて終了コード1で終える(
    fakes: FakeDeviceSet, make_runtime: Callable[..., Runtime]
) -> None:
    started = threading.Event()
    runtime = make_runtime(fakes, [make_feature("alpha", started, None)])
    runner = _Runner(runtime)
    assert started.wait(WAIT_S)

    def broken() -> None:
        raise RuntimeError("boom")

    failing = threading.Thread(target=broken, name="osekkai-broken", daemon=True)
    failing.start()
    failing.join(WAIT_S)
    assert runner.join() is ExitCode.FATAL
    assert_cleaned_up(fakes)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_機能のスレッドや基盤の外のスレッドの例外では止めない(
    fakes: FakeDeviceSet, make_runtime: Callable[..., Runtime]
) -> None:
    started = threading.Event()
    runtime = make_runtime(fakes, [make_feature("alpha", started, None)])
    runner = _Runner(runtime)
    assert started.wait(WAIT_S)

    def broken() -> None:
        raise RuntimeError("boom")

    for name in ("osekkai-feature-x", "other"):
        thread = threading.Thread(target=broken, name=name, daemon=True)
        thread.start()
        thread.join(WAIT_S)
    time.sleep(0.1)
    assert runner.thread.is_alive()
    runtime.request_stop()
    assert runner.join() is ExitCode.OK


def test_起動の途中の異常でも片付けて終了コード1で終える(
    fakes: FakeDeviceSet, make_runtime: Callable[..., Runtime], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_plan(self: FeatureRegistry, report: object) -> list[object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(FeatureRegistry, "plan", broken_plan)
    runtime = make_runtime(fakes, [])
    assert runtime.run() is ExitCode.FATAL
    assert_cleaned_up(fakes)
    # ロックが返されて、次の起動ができる
    assert runtime._lock.acquire()
    runtime._lock.release()


def test_基盤の異常の知らせでモーターを直ちに止める(
    fakes: FakeDeviceSet, make_runtime: Callable[..., Runtime]
) -> None:
    started = threading.Event()
    runtime = make_runtime(fakes, [make_feature("alpha", started, None)])
    runner = _Runner(runtime)
    assert started.wait(WAIT_S)
    before = _stop_count(fakes)
    runtime.report_fatal(RuntimeError("boom"))
    assert _stop_count(fakes) > before
    assert runner.join() is ExitCode.FATAL


def test_runの後は前のexcepthookに戻す(
    fakes: FakeDeviceSet, make_runtime: Callable[..., Runtime]
) -> None:
    before = threading.excepthook
    runtime = make_runtime(fakes, [])
    runner = _Runner(runtime)
    assert _wait_for(lambda: runtime.supervisor is not None)
    runtime.request_stop()
    assert runner.join() is ExitCode.OK
    assert threading.excepthook is before
