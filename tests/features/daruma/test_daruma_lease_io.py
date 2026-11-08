"""借りた資源とカメラで遊びの口を動かす（LeaseGameIO）のテスト（2.5、2.7、3.3、4.6、4.7、5.1、6.2、6.3）。"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from daruma_fakes import FakeLease

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Arbiter
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.config import MotionSettings, VoiceSettings
from osekkai.core.display import DisplayService
from osekkai.core.lcd_text import center
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.types import DeviceUnavailable, LeaseRevoked, Priority, Resource
from osekkai.core.voice import VoiceService
from osekkai.features.daruma.flow import CameraLost
from osekkai.features.daruma.lease_io import OWNER, REST_STEP_S, LeaseGameIO
from osekkai.features.daruma.rules import EndReason
from osekkai.features.presence.api import (
    CameraBusy,
    CameraLoan,
    CameraNotAvailable,
    CameraNotBorrowed,
)

LOGGER = "osekkai.features.daruma.lease_io"
WAIT_S = 2.0  # 実際の時間で待つ上限（テストが止まらないように）
AMOUNT = 0.0371  # ログに出てはいけない動きの量


class FakeLoan:
    """偽の借りたカメラ。`motion` は決めた値を返すか、決めた例外を投げる。"""

    def __init__(self, value: float | Exception = AMOUNT) -> None:
        self.value = value
        self.watched: list[float] = []
        self.releases = 0
        self.release_error: Exception | None = None

    def motion(self, seconds: float) -> float:
        self.watched.append(seconds)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value

    def frame(self, max_width: int = 640, quality: int = 70) -> bytes:
        raise AssertionError("映像は求めない")

    def still(self) -> bytes:
        raise AssertionError("静止画は求めない")

    def release(self) -> None:
        self.releases += 1
        if self.release_error is not None:
            raise self.release_error

    def __enter__(self) -> FakeLoan:
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


class FakeBorrow:
    """偽のカメラを借りる口。借り手の名前と `alive` を覚える。"""

    def __init__(self, loan: FakeLoan | None = None, error: Exception | None = None) -> None:
        self.loan = loan if loan is not None else FakeLoan()
        self.error = error
        self.owners: list[str] = []
        self.alive: Callable[[], bool] | None = None

    def __call__(self, owner: str, alive: Callable[[], bool]) -> CameraLoan:
        self.owners.append(owner)
        self.alive = alive
        if self.error is not None:
            raise self.error
        return self.loan


class Kit:
    def __init__(self, borrow: FakeBorrow | None = None) -> None:
        self.lease = FakeLease()
        self.borrow = borrow if borrow is not None else FakeBorrow()
        self.clock = FakeClock()
        self.stop = threading.Event()
        self.reason: EndReason | None = None
        self.interrupt_at: int | None = None  # この回数目の確かめから中断にする
        self.checks = 0
        self.io = LeaseGameIO(
            self.lease,
            borrow=self.borrow,
            clock=self.clock,
            interrupted=self._interrupted,
            stop=self.stop,
        )

    def _interrupted(self) -> EndReason | None:
        self.checks += 1
        if self.interrupt_at is not None and self.checks >= self.interrupt_at:
            return EndReason.PREEMPTED
        return self.reason


@pytest.fixture
def kit() -> Kit:
    return Kit()


# 話す・出す・首・ボタン


def test_LCD_は_2_行とも中央に置いて出す(kit: Kit) -> None:
    kit.io.show("ﾐﾃﾙﾖ!", "(o_o)")

    assert kit.lease.events == [("show", center("ﾐﾃﾙﾖ!"), center("(o_o)"))]


def test_話す_首_ボタンはリースの口に渡す(kit: Kit) -> None:
    kit.lease.presses = 1

    kit.io.say("ころんだ")
    kit.io.look(-40.0, 10.0)
    kit.io.home()
    kit.io.nod()
    assert kit.io.pressed() is True
    assert kit.io.wait_press(1.2) is True
    assert kit.io.wait_press(0.5) is False
    kit.io.clear_presses()

    assert kit.lease.events == [
        ("say", "ころんだ"),
        ("said", "ころんだ"),
        ("look", -40.0, 10.0),
        ("home",),
        ("nod", 1),  # 1 回だけうなずく（2.7）
        ("has_press",),
        ("wait_press", 1.2),
        ("wait_press", 0.5),
        ("clear",),
    ]


# 踊りながら話す


def test_踊りは首だけで_走行の口を渡さない(kit: Kit) -> None:
    kit.io.celebrate("やったね")

    dances = [event for event in kit.lease.events if event[0] == "dance"]
    assert dances == [("dance", None)]  # 走行の口なし（5.1）


def test_踊りと話は並んで動き_話し終えたら踊りを待つ(kit: Kit) -> None:
    speaking = threading.Event()
    dancing = threading.Event()
    seen: dict[str, bool] = {}
    threads: list[str] = []

    def speak_hook() -> None:
        speaking.set()
        seen["dance_while_speaking"] = dancing.wait(WAIT_S)

    def dance_hook() -> None:
        threads.append(threading.current_thread().name)
        dancing.set()
        seen["speech_while_dancing"] = speaking.wait(WAIT_S)
        time.sleep(0.05)  # 話し終えた後も少し踊る

    kit.lease.speak_hook = speak_hook
    kit.lease.dance_hook = dance_hook

    kit.io.celebrate("やったね")

    assert seen == {"dance_while_speaking": True, "speech_while_dancing": True}
    assert threads == ["daruma-dance"]
    names = kit.lease.names
    # 戻ったときには踊りも終わっている（待たなければ "danced" はまだない）
    assert "said" in names
    assert "danced" in names


@pytest.mark.parametrize("error", [LeaseRevoked("daruma"), DeviceUnavailable("neck"), OSError()])
def test_踊りの誤りは型の名前だけ記録して捨て_話は続ける(
    kit: Kit, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    kit.lease.dance_error = error
    caplog.set_level(logging.INFO, logger=LOGGER)

    kit.io.celebrate("やったね")

    assert ("said", "やったね") in kit.lease.events
    messages = [record.getMessage() for record in caplog.records]
    assert any(type(error).__name__ in message for message in messages)


def test_踊りながら話す誤りは外に出し_踊りは待つ(kit: Kit) -> None:
    kit.lease.say_error = LeaseRevoked("daruma")
    started = threading.Event()

    def dance_hook() -> None:
        started.set()
        time.sleep(0.05)

    kit.lease.dance_hook = dance_hook

    with pytest.raises(LeaseRevoked):
        kit.io.celebrate("やったね")

    assert started.is_set()
    assert "danced" in kit.lease.names


# カメラ


def test_カメラを開くと遊びの名前で借りる(kit: Kit) -> None:
    assert kit.io.open_camera() is True
    assert kit.borrow.owners == [OWNER]
    assert OWNER == "daruma"

    # もう借りていれば借り直さない
    assert kit.io.open_camera() is True
    assert kit.borrow.owners == [OWNER]


def test_カメラの貸し出しは止める旗かリースの終わりで切れる(kit: Kit) -> None:
    kit.io.open_camera()
    alive = kit.borrow.alive
    assert alive is not None

    assert alive() is True
    kit.lease.active = False
    assert alive() is False
    kit.lease.active = True
    kit.stop.set()
    assert alive() is False


@pytest.mark.parametrize("error", [CameraNotAvailable(), CameraBusy()])
def test_見つけてあいさつの誤りはカメラを使えないにする(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    kit = Kit(FakeBorrow(error=error))

    assert kit.io.open_camera() is False
    assert any(type(error).__name__ in r.getMessage() for r in caplog.records)

    # 借りていないので、見ればカメラの誤り
    with pytest.raises(CameraLost):
        kit.io.motion(2.5)


def test_動きの量は借りたカメラから受け取り_ログに出さない(
    kit: Kit, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    kit.io.open_camera()

    assert kit.io.motion(2.5) == AMOUNT
    assert kit.borrow.loan.watched == [2.5]
    assert str(AMOUNT) not in caplog.text


@pytest.mark.parametrize(
    "error", [CameraNotBorrowed(), CameraNotAvailable(), TimeoutError("t"), RuntimeError("x")]
)
def test_見るときのどの誤りもカメラの誤りにする(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    kit = Kit(FakeBorrow(FakeLoan(error)))
    kit.io.open_camera()

    with pytest.raises(CameraLost):
        kit.io.motion(2.5)

    assert any(type(error).__name__ in r.getMessage() for r in caplog.records)


def test_カメラを開かずに見るとカメラの誤り(kit: Kit) -> None:
    with pytest.raises(CameraLost):
        kit.io.motion(2.5)
    assert kit.borrow.owners == []


# 休む


def test_休むは時計で少しずつ待つ(kit: Kit) -> None:
    kit.io.rest(0.5)

    assert kit.clock.sleeps == pytest.approx([REST_STEP_S] * 5)
    assert REST_STEP_S == 0.1


def test_休む間に中断があれば早く戻る(kit: Kit) -> None:
    kit.interrupt_at = 3  # 3 回目の確かめで中断に気づく

    kit.io.rest(5.0)

    assert len(kit.clock.sleeps) == 2


def test_始めから中断していれば休まずに戻る(kit: Kit) -> None:
    kit.reason = EndReason.QUIET

    kit.io.rest(5.0)

    assert kit.clock.sleeps == []
    assert kit.checks == 1


# 閉じる


def test_閉じるとカメラを返し_2_回呼んでもよい(kit: Kit) -> None:
    kit.io.open_camera()

    kit.io.close()
    kit.io.close()

    assert kit.borrow.loan.releases == 1
    assert kit.lease.events == []  # 首は動かさない（基盤と流れが戻す）
    with pytest.raises(CameraLost):
        kit.io.motion(2.5)


def test_カメラを開かずに閉じてもよい(kit: Kit) -> None:
    kit.io.close()

    assert kit.borrow.loan.releases == 0


def test_返すときの誤りは記録して捨てる(kit: Kit, caplog: pytest.LogCaptureFixture) -> None:
    kit.io.open_camera()
    kit.borrow.loan.release_error = CameraNotAvailable()
    caplog.set_level(logging.INFO, logger=LOGGER)

    kit.io.close()

    assert any("CameraNotAvailable" in r.getMessage() for r in caplog.records)


# 基盤のリースで動かす


@pytest.fixture
def arbiter(xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock) -> Iterator[Arbiter]:
    cache = CacheManager(Paths().cache_dir, max_bytes=10 * 1024 * 1024, clock=clock)
    voice = VoiceService(fakes.synth, fakes.player, cache, VoiceSettings(sink="speaker"))
    motion = MotionService(
        fakes.servos, fakes.motors, MotionSettings(allow_drive_in_dance=True), clock
    )
    router = ButtonRouter(fakes.buttons, clock)
    router.start()
    yield Arbiter(clock, motion, voice=voice, display=DisplayService(fakes.display), buttons=router)
    router.close()


def test_基盤のリースでは首だけで踊り_ステアリングとタイヤに触らない(
    arbiter: Arbiter, fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    lease = arbiter.acquire(
        "daruma",
        Priority.INTERACTIVE + 6,
        [Resource.VOICE, Resource.DISPLAY, Resource.BUTTON, Resource.NECK],
    )
    io = LeaseGameIO(
        lease, borrow=FakeBorrow(), clock=clock, interrupted=lambda: None, stop=threading.Event()
    )
    steering_before = [c for c in fakes.servos.calls if c.args and c.args[0] == "steering"]

    io.celebrate("やったね")

    channels = {c.args[0] for c in fakes.servos.calls if c.op == "set_angle"}
    assert "pan" in channels  # 首は踊った
    steering = [c for c in fakes.servos.calls if c.args and c.args[0] == "steering"]
    assert steering == steering_before  # ステアリングに触らない（5.1）
    assert fakes.motors.calls == []  # タイヤに触らない（5.1）
    assert fakes.player.calls  # 話した
    lease.release()
