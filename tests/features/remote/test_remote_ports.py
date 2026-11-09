"""ラジコンのセンサーとカメラの口、開発 PC の偽物、テストの偽物の形（15.1）。

偽物が口の形どおりに読めることと、偽のリースが基盤のリースと同じ決まりで断ることを確かめる。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import FrozenInstanceError

import pytest
from remote_fakes import (
    FakeClock,
    FakeContext,
    FakeFrameSource,
    FakeUsbCamera,
    ScriptedSensors,
)

from osekkai.core.arbiter import LeaseState
from osekkai.core.clock import Clock
from osekkai.core.types import LeaseRevoked, Resource, ResourceBusy
from osekkai.features.remote.adapters.fake import ClearSensors, NoUsbCamera
from osekkai.features.remote.ports import (
    FrameSource,
    SensorPort,
    SensorReading,
    UsbCameraPort,
)

OPERATE = frozenset({Resource.DRIVE, Resource.NECK, Resource.VOICE, Resource.DISPLAY})
WAIT_S = 5.0  # 別のスレッドを待つ上限（実際の秒）

# --- 口の形 ---


def test_センサーの読みは物がない印を既定で偽にする() -> None:
    reading = SensorReading(distance_cm=12.5, floor=(1, None, 3))
    assert reading.far is False
    assert reading.distance_cm == 12.5
    assert reading.floor == (1, None, 3)


def test_センサーの読みは変えられない() -> None:
    reading = SensorReading(distance_cm=None, floor=(None, None, None), far=True)
    name = "far"
    with pytest.raises(FrozenInstanceError):
        setattr(reading, name, False)


# --- 開発 PC の偽物 ---


def test_いつも安全なセンサーは前に物がなく床が明るい読みを返す() -> None:
    sensors = ClearSensors()
    assert isinstance(sensors, SensorPort)
    for _ in range(3):
        assert sensors.read() == SensorReading(distance_cm=100.0, floor=(1000, 1000, 1000))
    sensors.close()
    sensors.close()  # 何度閉じてもよい


def test_ない_USB_カメラは開けず_1_枚も返さない() -> None:
    camera = NoUsbCamera()
    assert isinstance(camera, UsbCameraPort)
    assert camera.open() is False
    assert camera.read_jpeg() is None
    camera.close()


# --- テストの偽物: 時計 ---


def test_偽の時計は時計の口を満たし_待つと時刻が進む() -> None:
    clock: Clock = FakeClock(monotonic=10.0)
    start = clock.now()
    clock.sleep(1.5)
    assert clock.monotonic() == 11.5
    assert (clock.now() - start).total_seconds() == 1.5


# --- テストの偽物: センサーとカメラ ---


def test_台本のセンサーは順に返し_尽きたら最後の読みを返し続ける() -> None:
    near = SensorReading(distance_cm=5.0, floor=(1000, 1000, 1000))
    clear = SensorReading(distance_cm=100.0, floor=(1000, 1000, 1000))
    sensors = ScriptedSensors([near, clear])
    assert isinstance(sensors, SensorPort)
    assert [sensors.read() for _ in range(4)] == [near, clear, clear, clear]
    assert sensors.reads == 4
    sensors.set(near)
    assert sensors.read() == near
    sensors.close()
    assert sensors.closed


def test_台本のセンサーは例外を入れるとその読みで投げる() -> None:
    clear = SensorReading(distance_cm=100.0, floor=(1000, 1000, 1000))
    sensors = ScriptedSensors([OSError("hidden"), clear])
    with pytest.raises(OSError):
        sensors.read()
    assert sensors.read() == clear


def test_偽の映像の元は開いた後だけ順に返す() -> None:
    source = FakeFrameSource([b"a", None, b"c"], still=b"s")
    assert isinstance(source, FrameSource)
    assert source.read() is None  # 開く前は読めない
    assert source.open() is True
    assert [source.read() for _ in range(4)] == [b"a", None, b"c", b"c"]
    assert source.still() == b"s"
    source.close()
    assert source.read() is None
    assert source.opens == 1
    assert source.closes == 1


def test_偽の映像の元は開けないようにできる() -> None:
    source = FakeFrameSource([b"a"], can_open=False)
    assert source.open() is False
    assert source.read() is None
    assert source.still() is None


def test_偽の_USB_カメラは開いた後だけ_JPEG_を返す() -> None:
    camera = FakeUsbCamera([b"j1", None])
    assert isinstance(camera, UsbCameraPort)
    assert camera.read_jpeg() is None
    assert camera.open() is True
    assert [camera.read_jpeg() for _ in range(3)] == [b"j1", None, None]
    camera.close()
    assert camera.closed
    assert FakeUsbCamera([b"j1"], can_open=False).open() is False


# --- テストの偽物: 機能の文脈とリース ---


def test_偽の文脈は求めた資源のリースを返し_呼び出しを覚える() -> None:
    ctx = FakeContext()
    lease = ctx.as_context().acquire(OPERATE, wait=0.0)
    assert lease.resources == OPERATE
    assert lease.active
    assert lease.owner == "remote"
    lease.drive.drive(30.0, 50)
    lease.drive.stop()
    lease.neck.look(15.0, -15.0)
    lease.neck.home()
    lease.display.show("ラジコン", "")
    lease.voice.say("ブッブー", keep=False)
    assert ctx.calls == [
        ("acquire", OPERATE, 0.0),
        ("drive", 30.0, 50),
        ("stop",),
        ("look", 15.0, -15.0),
        ("home",),
        ("show", "ラジコン", ""),
        ("say", "ブッブー", False),
        ("said", "ブッブー"),
    ]


def test_偽の文脈は申告にない資源と空の資源を断る() -> None:
    ctx = FakeContext()
    with pytest.raises(ValueError):
        ctx.acquire({Resource.BUTTON})
    with pytest.raises(ValueError):
        ctx.acquire(())


def test_偽の文脈は求めに応じて借りられないことにできる() -> None:
    ctx = FakeContext()
    ctx.busy = {Resource.DRIVE}
    with pytest.raises(ResourceBusy):
        ctx.acquire(OPERATE)
    lease = ctx.acquire({Resource.NECK})
    assert lease.active
    ctx.busy = set()
    assert ctx.acquire({Resource.DRIVE}).active
    assert len(ctx.leases) == 2


def test_借りていない資源の口を求めると断る() -> None:
    lease = FakeContext().acquire({Resource.NECK})
    with pytest.raises(ValueError):
        _ = lease.drive


def test_返したリースの口はリースの取り上げを投げる() -> None:
    ctx = FakeContext()
    lease = ctx.acquire(OPERATE)
    drive, neck, voice, display = lease.drive, lease.neck, lease.voice, lease.display
    lease.release()
    lease.release()  # 何度返してもよい
    assert lease.state is LeaseState.RELEASED
    assert not lease.active
    actions: list[Callable[[], None]] = [
        lambda: drive.drive(0.0, 10),
        drive.stop,
        lambda: neck.look(0.0, 0.0),
        neck.home,
        lambda: voice.say("x"),
        lambda: display.show("a", "b"),
    ]
    for action in actions:
        with pytest.raises(LeaseRevoked):
            action()
    assert ctx.calls.count(("release",)) == 1


def test_取り上げたリースの口はリースの取り上げを投げる() -> None:
    lease = FakeContext().acquire(OPERATE)
    lease.revoke()
    assert lease.state is LeaseState.REVOKED
    with pytest.raises(LeaseRevoked):
        lease.drive.stop()


def test_中断の知らせは旗を立て_受け手を_1_回だけ呼ぶ() -> None:
    ctx = FakeContext()
    got: list[str] = []
    lease = ctx.acquire(OPERATE, on_preempt=lambda: got.append("preempted"))
    lease.preempt()
    lease.preempt()
    assert got == ["preempted"]
    assert lease.preempt_requested.is_set()
    assert lease.state is LeaseState.PREEMPT_REQUESTED
    assert lease.active
    # 基盤と同じく、中断の後は走る・首の段のある動きを断り、止めると首を向けるはできる
    with pytest.raises(LeaseRevoked):
        lease.drive.drive(0.0, 10)
    with pytest.raises(LeaseRevoked):
        lease.neck.nod()
    lease.drive.stop()
    lease.neck.look(0.0, 0.0)


def test_中断の知らせは文脈の最後のリースにも出せる() -> None:
    ctx = FakeContext()
    got: list[str] = []
    ctx.acquire({Resource.NECK}, on_preempt=lambda: got.append("neck"))
    ctx.preempt_latest()
    assert got == ["neck"]


def test_偽の声は話している途中にできる() -> None:
    ctx = FakeContext()
    voice = ctx.acquire({Resource.VOICE}).voice
    ctx.speech.hold()
    thread = threading.Thread(target=voice.say, args=("はい、チーズ！",), daemon=True)
    thread.start()
    assert ctx.speech.speaking.wait(WAIT_S)
    assert ("said", "はい、チーズ！") not in ctx.calls
    ctx.speech.release()
    thread.join(WAIT_S)
    assert not thread.is_alive()
    assert not ctx.speech.speaking.is_set()
    assert ctx.calls[-1] == ("said", "はい、チーズ！")


def test_偽の文脈は知らせを受け手に届ける() -> None:
    ctx = FakeContext()
    got: list[int] = []
    ctx.subscribe(int, got.append)
    ctx.publish(7)
    ctx.publish("ignored")
    assert got == [7]
