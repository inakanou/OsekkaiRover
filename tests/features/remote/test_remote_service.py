"""操作の受け付けの順と、0.1 秒ごとの走行の繰り返し（要件 3.2、4.2〜4.8、5.1〜5.6、6.1〜6.3、
11.1〜11.3、12.3、12.6）。

多くのテストは繰り返しを始めずに `_drive_step()` で 1 歩ずつ進める。
本物のスレッドのテストは、偽の時計（sleep は待たない）と短い待ちで確かめる。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from remote_fakes import FakeClock, FakeContext, FakeDrive, ScriptedSensors

from osekkai.adapters.fake.clock import JST
from osekkai.core.lcd_text import center
from osekkai.core.types import LeaseRevoked, Resource
from osekkai.features.remote import texts
from osekkai.features.remote.config import RemoteConfig
from osekkai.features.remote.drive_rules import Move
from osekkai.features.remote.leases import LeaseKeeper, Level
from osekkai.features.remote.operator_slot import OperatorSlot
from osekkai.features.remote.photos import PhotoStore
from osekkai.features.remote.ports import SensorReading
from osekkai.features.remote.service import MAX_FAILURES, RemoteService, Reply

ME = "hidden1"  # 印の偽の値（ログに出ないことを確かめる）
OTHER = "hidden2"
WAIT_S = 5.0  # 別のスレッドを待つ上限（実際の秒）
NEAR = SensorReading(distance_cm=5.0, floor=(1000, 1000, 1000))
EDGE = SensorReading(distance_cm=100.0, floor=(100, 1000, 1000))


class _Rig:
    def __init__(self, tmp_path: Path, *, threaded: bool = False, **config: object) -> None:
        self.clock = FakeClock()
        self.ctx = FakeContext(self.clock)
        self.sensors = ScriptedSensors()
        self.lost = 0
        self.leases = LeaseKeeper(self.ctx.as_context(), self._on_lost)
        self.slot = OperatorSlot(self.clock)
        self.service = RemoteService(
            self.ctx.as_context(),
            RemoteConfig.model_validate({"enabled": True, **config}),
            self.sensors,
            {},
            PhotoStore(tmp_path, self.clock),
            self.slot,
            self.leases,
            random.Random(0),
        )
        self.service._threaded = threaded

    def _on_lost(self) -> None:
        self.lost += 1

    def step(self) -> bool:
        return self.service._drive_step()

    def moves(self) -> list[tuple[object, ...]]:
        """走行の口に渡った操作だけ。"""
        return [c for c in self.ctx.calls if c[0] in ("drive", "stop")]

    def shows(self) -> list[tuple[object, ...]]:
        return [c for c in self.ctx.calls if c[0] == "show"]

    def says(self) -> list[tuple[object, ...]]:
        return [c for c in self.ctx.calls if c[0] == "said"]


@pytest.fixture
def rig(tmp_path: Path) -> _Rig:
    return _Rig(tmp_path)


def _wait(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + WAIT_S
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("待ちが終わりませんでした")
        time.sleep(0.01)


def _show(line1: str, line2: str) -> tuple[object, ...]:
    return ("show", center(line1), center(line2))


# --- 走行 ---


def test_前を押すと決まった速さで走り_離すと止まる(rig: _Rig) -> None:
    assert rig.service.drive(ME, Move.FWD) is Reply.OK
    assert rig.step()
    rig.clock.advance(0.1)
    assert rig.service.drive(ME, Move.STOP) is Reply.OK
    assert rig.step()
    assert rig.moves() == [("drive", 0.0, 20), ("stop",)]


def test_左と右はステアリングを30度切る(rig: _Rig) -> None:
    rig.service.drive(ME, Move.LEFT)
    rig.step()
    rig.service.drive(ME, Move.RIGHT)
    rig.step()
    assert rig.moves() == [("drive", -30.0, 20), ("drive", 30.0, 20)]


def test_命令が05秒届かなければ止まる(rig: _Rig) -> None:
    rig.service.drive(ME, Move.FWD)
    rig.step()
    rig.clock.advance(0.5)
    rig.step()
    rig.clock.advance(0.01)
    rig.step()
    assert rig.moves() == [("drive", 0.0, 20), ("drive", 0.0, 20), ("stop",)]


def test_はやいを選ぶと次の命令から速く走る(rig: _Rig) -> None:
    rig.service.drive(ME, Move.FWD)
    rig.step()
    assert rig.service.set_fast(ME, True) is Reply.OK
    rig.step()
    assert rig.moves() == [("drive", 0.0, 20), ("drive", 0.0, 35)]


def test_後退はいつもゆっくりで2秒で止まり_離すまで下がらない(rig: _Rig) -> None:
    rig.service.set_fast(ME, True)
    for _ in range(20):
        rig.service.drive(ME, Move.BACK)
        rig.step()
        rig.clock.advance(0.1)
    rig.service.drive(ME, Move.BACK)
    rig.step()
    moves = rig.moves()
    assert moves[:20] == [("drive", 0.0, -20)] * 20
    assert moves[20] == ("stop",)
    rig.service.drive(ME, Move.STOP)
    rig.step()
    rig.service.drive(ME, Move.BACK_LEFT)
    rig.step()
    assert rig.moves()[-1] == ("drive", -30.0, -20)


def test_後退ではセンサーを読まない(rig: _Rig) -> None:
    rig.sensors.set(NEAR)
    rig.service.drive(ME, Move.BACK)
    rig.step()
    assert rig.sensors.reads == 0
    assert rig.moves() == [("drive", 0.0, -20)]


def test_走行のLCDは変わったときだけ出す(rig: _Rig) -> None:
    rig.service.drive(ME, Move.FWD)
    rig.step()
    rig.step()
    rig.service.drive(ME, Move.BACK)
    rig.step()
    rig.service.drive(ME, Move.STOP)
    rig.step()
    rig.step()
    face = texts.FACE_DRIVE.text()
    assert rig.shows() == [
        _show(texts.LCD_FORWARD, face),
        _show(texts.LCD_BACK, face),
        _show(texts.LCD_IDLE, face),
    ]


# --- 前の安全 ---


def test_前に物があれば1回の読みで止まり_LCDと知らせと声を出す(rig: _Rig) -> None:
    rig.sensors.set(NEAR)
    rig.service.drive(ME, Move.FWD)
    rig.step()
    assert rig.moves() == [("stop",)]
    assert rig.sensors.reads == 1
    assert rig.shows() == [_show(texts.LCD_STOP, texts.FACE_STOP.text())]
    assert rig.service._message_for(ME) == texts.MSG_DANGER_OBSTACLE
    assert rig.service._message_for(OTHER) is None
    _wait(lambda: rig.says() == [("said", texts.SAY_DANGER_OBSTACLE)])
    assert ("say", texts.SAY_DANGER_OBSTACLE, True) in rig.ctx.calls


def test_段差なら段差の知らせと声(rig: _Rig) -> None:
    rig.sensors.set(EDGE)
    rig.service.drive(ME, Move.FWD)
    rig.step()
    assert rig.moves() == [("stop",)]
    assert rig.service._message_for(ME) == texts.MSG_DANGER_CLIFF
    _wait(lambda: rig.says() == [("said", texts.SAY_DANGER_CLIFF)])


def test_センサーが読めなければ前に進まない(rig: _Rig) -> None:
    rig.sensors.set(OSError("broken"))
    rig.service.drive(ME, Move.FWD)
    assert rig.step()
    assert rig.moves() == [("stop",)]
    assert rig.service._message_for(ME) == texts.MSG_DANGER_OBSTACLE


def test_危険の声は10秒に1回で_知らせは毎回出す(rig: _Rig) -> None:
    rig.sensors.set(NEAR)
    for _ in range(5):
        rig.service.drive(ME, Move.FWD)
        rig.step()
        _wait(lambda: not rig.service._speaking)
        rig.clock.advance(1.0)
    assert len(rig.says()) == 1
    assert rig.service._message_for(ME) == texts.MSG_DANGER_OBSTACLE
    rig.clock.advance(6.0)
    rig.service.drive(ME, Move.FWD)
    rig.step()
    _wait(lambda: len(rig.says()) == 2)


def test_話している途中は危険の声を重ねない(rig: _Rig) -> None:
    rig.ctx.speech.hold()
    rig.sensors.set(NEAR)
    rig.service.drive(ME, Move.FWD)
    rig.step()
    _wait(rig.ctx.speech.speaking.is_set)
    rig.clock.advance(11.0)
    rig.service.drive(ME, Move.FWD)
    rig.step()
    rig.ctx.speech.release()
    _wait(lambda: not rig.service._speaking)
    assert len(rig.says()) == 1


def test_危険が続く間も後退は受け付け_前に戻れば知らせが消える(rig: _Rig) -> None:
    rig.sensors.set(NEAR)
    rig.service.drive(ME, Move.FWD)
    rig.step()
    rig.service.drive(ME, Move.BACK)
    rig.step()
    assert rig.moves()[-1] == ("drive", 0.0, -20)
    assert rig.service._message_for(ME) is None


# --- 受け付けの順 ---


def test_ほかの端末が操作する端末なら断る(rig: _Rig) -> None:
    rig.service.drive(ME, Move.FWD)
    assert rig.service.drive(OTHER, Move.STOP) is Reply.NOT_NOW
    assert rig.service.set_fast(OTHER, True) is Reply.NOT_NOW
    assert rig.service._message_for(OTHER) == texts.MSG_OTHER_OPERATOR
    rig.step()
    assert rig.moves() == [("drive", 0.0, 20)]


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (
            lambda r: r.clock.set_now(datetime(2026, 1, 1, 20, 0, tzinfo=JST)),
            texts.MSG_OUT_OF_HOURS,
        ),
        (lambda r: r.ctx.request_quiet(), texts.MSG_QUIET),
        (lambda r: r.clock.set_synchronized(False), texts.MSG_NO_CLOCK),
    ],
    ids=["時間帯の外", "おやすみ", "時刻が確かでない"],
)
def test_使えないときは断り_リースを借りない(
    rig: _Rig, setup: Callable[[_Rig], None], message: str
) -> None:
    setup(rig)
    assert rig.service.drive(ME, Move.FWD) is Reply.NOT_NOW
    assert rig.service._message_for(ME) == message
    assert rig.ctx.calls == []
    assert rig.slot.holder() is None


def test_使えるようになれば断りの知らせが消える(rig: _Rig) -> None:
    rig.ctx.request_quiet()
    rig.service.drive(ME, Move.FWD)
    rig.ctx.end_quiet()
    assert rig.service.drive(ME, Move.FWD) is Reply.OK
    assert rig.service._message_for(ME) is None


def test_操作の段を借りられなければ断る(rig: _Rig) -> None:
    rig.ctx.busy = {Resource.DRIVE}
    assert rig.service.drive(ME, Move.FWD) is Reply.NOT_NOW
    assert rig.service._message_for(ME) == texts.MSG_BUSY
    assert not rig.step()
    assert rig.moves() == []


def test_借り直したら止まる前の命令を続けない(rig: _Rig) -> None:
    rig.service.drive(ME, Move.FWD)
    rig.step()
    rig.ctx.preempt_latest()
    assert not rig.step()
    assert rig.service.set_fast(ME, False) is Reply.OK
    assert len(rig.ctx.leases) == 2
    rig.step()
    assert rig.moves() == [("drive", 0.0, 20), ("stop",)]


# --- 繰り返しの例外 ---


def test_走行の途中の失敗では止めて次の命令を受け付ける(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = FakeDrive.drive
    failures = [RuntimeError("boom")]

    def drive(self: FakeDrive, steering: float, speed: int) -> None:
        if failures:
            raise failures.pop()
        original(self, steering, speed)

    monkeypatch.setattr(FakeDrive, "drive", drive)
    rig.service.drive(ME, Move.FWD)
    assert rig.step()
    assert rig.moves() == [("stop",)]
    rig.service.drive(ME, Move.FWD)
    assert rig.step()
    assert rig.moves()[-1] == ("drive", 0.0, 20)


def test_走行の口がLeaseRevokedなら繰り返しを終える(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def drive(self: FakeDrive, steering: float, speed: int) -> None:
        raise LeaseRevoked("remote")

    monkeypatch.setattr(FakeDrive, "drive", drive)
    rig.service.drive(ME, Move.FWD)
    assert not rig.step()


def test_リースを取り上げられたら繰り返しを終える(rig: _Rig) -> None:
    rig.service.drive(ME, Move.FWD)
    rig.ctx.leases[-1].revoke()
    assert not rig.step()
    assert rig.moves() == []


# --- 本物のスレッド ---


def _drive_thread() -> threading.Thread | None:
    for thread in threading.enumerate():
        if thread.name == "remote-drive":
            return thread
    return None


def test_繰り返しはremote_driveのスレッドで動き_中断の知らせで終わる(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, threaded=True)
    try:
        assert rig.service.drive(ME, Move.FWD) is Reply.OK
        thread = _drive_thread()
        assert thread is not None
        assert thread.daemon
        _wait(lambda: ("drive", 0.0, 20) in rig.ctx.calls)
        rig.ctx.preempt_latest()
        thread.join(WAIT_S)
        assert not thread.is_alive()
        assert rig.service._thread is None
    finally:
        rig.service.close()


def test_閉じると繰り返しが止まる(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, threaded=True)
    rig.service.drive(ME, Move.STOP)
    thread = _drive_thread()
    assert thread is not None
    rig.service.close()
    assert not thread.is_alive()
    assert rig.service.drive(ME, Move.FWD) is Reply.NOT_NOW


def test_続けて10回失敗したらリースを返す(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: FakeDrive, *args: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(FakeDrive, "drive", broken)
    monkeypatch.setattr(FakeDrive, "stop", broken)
    rig = _Rig(tmp_path, threaded=True)
    try:
        rig.service.drive(ME, Move.FWD)
        _wait(lambda: rig.service._thread is None)
        assert ("release",) in rig.ctx.calls
        assert rig.leases.lease is None
        assert rig.service._failures >= MAX_FAILURES
    finally:
        rig.service.close()


# --- ログ ---


def test_ログに印と言葉を出さない(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig.sensors.set(OSError("broken"))
    rig.service.drive(ME, Move.FWD)
    rig.step()
    rig.sensors.set(NEAR)
    rig.service.drive(ME, Move.FWD)
    rig.step()
    _wait(lambda: len(rig.says()) == 1)
    rig.service.drive(OTHER, Move.FWD)

    def drive(self: FakeDrive, steering: float, speed: int) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(FakeDrive, "drive", drive)
    rig.sensors.set(SensorReading(distance_cm=100.0, floor=(1000, 1000, 1000)))
    rig.service.drive(ME, Move.FWD)
    rig.step()
    assert caplog.records
    for word in (ME, OTHER, texts.SAY_DANGER_OBSTACLE, texts.MSG_DANGER_OBSTACLE, "boom", "broken"):
        assert word not in caplog.text


def test_受け付ける前に借り直したリースでは前の命令で走らない(rig: _Rig) -> None:
    rig.service.drive(ME, Move.FWD)
    rig.step()
    rig.ctx.preempt_latest()
    # 受け付けの前に、繰り返しが新しいリースを見た
    rig.leases.want(Level.OPERATE)
    assert rig.step()
    assert rig.moves() == [("drive", 0.0, 20), ("stop",)]


def test_繰り返しが思わぬ形で止まっても走行を止めてから終える(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _Rig(tmp_path, threaded=True)

    def broken() -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(rig.service, "_drive_step", broken)
    try:
        rig.service.drive(ME, Move.FWD)
        _wait(lambda: rig.service._thread is None)
        assert rig.moves() == [("stop",)]
    finally:
        rig.service.close()
