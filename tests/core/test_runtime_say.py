"""「話して終わる」（Runtime.say_and_exit）のテスト（13.5、1.7）。"""

import logging
from pathlib import Path

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.paths import Paths
from osekkai.core.registry import FeatureRegistry
from osekkai.core.runtime import ALREADY_RUNNING_MESSAGE, ExitCode, Runtime


@pytest.fixture
def paths(xdg_dirs: dict[str, Path]) -> Paths:
    return Paths()


def _runtime(
    fakes: FakeDeviceSet, paths: Paths, clock: FakeClock, fail: tuple[str, ...] = ()
) -> Runtime:
    return Runtime(fakes.to_devices(fail), paths, clock, FeatureRegistry([]))


def _untouched(fakes: FakeDeviceSet) -> None:
    for device in (
        fakes.display,
        fakes.led,
        fakes.buttons,
        fakes.servos,
        fakes.motors,
        fakes.hat,
    ):
        assert device.calls == []


def test_文章を話して終了コード_0_で終わり_ほかのデバイスに触らない(
    fakes: FakeDeviceSet, paths: Paths, clock: FakeClock
) -> None:
    runtime = _runtime(fakes, paths, clock)
    assert runtime.say_and_exit("こんにちは") is ExitCode.OK
    assert fakes.synth.lines == ["こんにちは"]
    assert len(fakes.player.played) == 1
    _untouched(fakes)


def test_話し終えたらロックを放す(fakes: FakeDeviceSet, paths: Paths, clock: FakeClock) -> None:
    runtime = _runtime(fakes, paths, clock)
    runtime.say_and_exit("こんにちは")
    lock = InstanceLock(paths.lock_file)
    assert lock.acquire()
    lock.release()


def test_すでに動いていれば_75_で終わり話さない(
    fakes: FakeDeviceSet,
    paths: Paths,
    clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = InstanceLock(paths.lock_file)
    assert other.acquire()
    try:
        code = _runtime(fakes, paths, clock).say_and_exit("こんにちは")
    finally:
        other.release()
    assert code is ExitCode.ALREADY_RUNNING
    assert ALREADY_RUNNING_MESSAGE in capsys.readouterr().err
    assert fakes.synth.calls == []
    assert fakes.player.calls == []


def test_合成に失敗したら_1_で終わり_ログに文章を出さない(
    fakes: FakeDeviceSet,
    paths: Paths,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fakes.synth.fail_on.add("synthesize")
    with caplog.at_level(logging.DEBUG, logger="osekkai"):
        code = _runtime(fakes, paths, clock).say_and_exit("ひみつのことば")
    assert code is ExitCode.FATAL
    assert "SpeechFailed" in caplog.text
    assert "ひみつのことば" not in caplog.text


def test_声のデバイスが使えなければ_1_で終わる(
    fakes: FakeDeviceSet, paths: Paths, clock: FakeClock
) -> None:
    runtime = _runtime(fakes, paths, clock, ("player",))
    assert runtime.say_and_exit("こんにちは") is ExitCode.FATAL
    lock = InstanceLock(paths.lock_file)
    assert lock.acquire()  # 失敗でもロックを放す
    lock.release()
