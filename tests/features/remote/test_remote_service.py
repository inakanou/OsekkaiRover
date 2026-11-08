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
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

import pytest
from remote_fakes import (
    FakeClock,
    FakeContext,
    FakeDrive,
    FakeFrameSource,
    FakeNeck,
    ScriptedSensors,
)

from osekkai.adapters.fake.clock import JST
from osekkai.core.lcd_text import center
from osekkai.core.types import LeaseRevoked, Resource
from osekkai.features.remote import texts
from osekkai.features.remote.config import RemoteConfig
from osekkai.features.remote.drive_rules import Move
from osekkai.features.remote.leases import LeaseKeeper, Level
from osekkai.features.remote.media import Camera, FrameHub
from osekkai.features.remote.operator_slot import OperatorSlot
from osekkai.features.remote.photos import PhotoStore
from osekkai.features.remote.ports import SensorReading
from osekkai.features.remote.service import (
    MAX_FAILURES,
    PhotoResult,
    RemoteService,
    Reply,
    Status,
)

ME = "hidden1"  # 印の偽の値（ログに出ないことを確かめる）
OTHER = "hidden2"
WAIT_S = 5.0  # 別のスレッドを待つ上限（実際の秒）
NEAR = SensorReading(distance_cm=5.0, floor=(1000, 1000, 1000))
EDGE = SensorReading(distance_cm=100.0, floor=(100, 1000, 1000))


class _Rig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        threaded: bool = False,
        clock: FakeClock | None = None,
        ctx: FakeContext | None = None,
        hubs: Mapping[Camera, FrameHub] | None = None,
        **config: object,
    ) -> None:
        self.clock = clock if clock is not None else FakeClock()
        self.ctx = ctx if ctx is not None else FakeContext(self.clock)
        self.sensors = ScriptedSensors()
        self.lost = 0
        self.leases = LeaseKeeper(self.ctx.as_context(), self._on_lost)
        self.slot = OperatorSlot(self.clock)
        self.photo_dir = tmp_path
        self.service = RemoteService(
            self.ctx.as_context(),
            RemoteConfig.model_validate({"enabled": True, **config}),
            self.sensors,
            hubs if hubs is not None else {},
            PhotoStore(tmp_path, self.clock),
            self.slot,
            self.leases,
            random.Random(0),
        )
        self.service._threaded = threaded

    def _on_lost(self) -> None:
        # 機能と同じく、中断の知らせをサービスに渡す（12.2）
        self.lost += 1
        self.service.on_lost()

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
    assert rig.service.status(ME).message == texts.MSG_DANGER_OBSTACLE
    assert rig.service.status(OTHER).message != texts.MSG_DANGER_OBSTACLE  # 危険は操作する端末だけ
    _wait(lambda: rig.says() == [("said", texts.SAY_DANGER_OBSTACLE)])
    assert ("say", texts.SAY_DANGER_OBSTACLE, True) in rig.ctx.calls


def test_段差なら段差の知らせと声(rig: _Rig) -> None:
    rig.sensors.set(EDGE)
    rig.service.drive(ME, Move.FWD)
    rig.step()
    assert rig.moves() == [("stop",)]
    assert rig.service.status(ME).message == texts.MSG_DANGER_CLIFF
    _wait(lambda: rig.says() == [("said", texts.SAY_DANGER_CLIFF)])


def test_センサーが読めなければ前に進まない(rig: _Rig) -> None:
    rig.sensors.set(OSError("broken"))
    rig.service.drive(ME, Move.FWD)
    assert rig.step()
    assert rig.moves() == [("stop",)]
    assert rig.service.status(ME).message == texts.MSG_DANGER_OBSTACLE


def test_危険の声は10秒に1回で_知らせは毎回出す(rig: _Rig) -> None:
    rig.sensors.set(NEAR)
    for _ in range(5):
        rig.service.drive(ME, Move.FWD)
        rig.step()
        _wait(lambda: not rig.service._speaking)
        rig.clock.advance(1.0)
    assert len(rig.says()) == 1
    assert rig.service.status(ME).message == texts.MSG_DANGER_OBSTACLE
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
    assert rig.service.status(ME).message is None


# --- 受け付けの順 ---


def test_ほかの端末が操作する端末なら断る(rig: _Rig) -> None:
    rig.service.drive(ME, Move.FWD)
    assert rig.service.drive(OTHER, Move.STOP) is Reply.NOT_NOW
    assert rig.service.set_fast(OTHER, True) is Reply.NOT_NOW
    assert rig.service.status(OTHER).message == texts.MSG_OTHER_OPERATOR
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
    assert rig.service.status(ME).message == message
    assert rig.ctx.calls == []
    assert rig.slot.holder() is None


def test_使えるようになれば断りの知らせが消える(rig: _Rig) -> None:
    rig.ctx.request_quiet()
    rig.service.drive(ME, Move.FWD)
    rig.ctx.end_quiet()
    assert rig.service.drive(ME, Move.FWD) is Reply.OK
    assert rig.service.status(ME).message is None


def test_操作の段を借りられなければ断る(rig: _Rig) -> None:
    rig.ctx.busy = {Resource.DRIVE}
    assert rig.service.drive(ME, Move.FWD) is Reply.NOT_NOW
    assert rig.service.status(ME).message == texts.MSG_BUSY
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


# --- 首（7.1、7.2） ---


def _looks(rig: _Rig) -> list[tuple[object, ...]]:
    return [c for c in rig.ctx.calls if c[0] in ("look", "home")]


def test_首は上下左右に15度ずつ動く(rig: _Rig) -> None:
    for way in ("right", "up", "left", "down"):
        assert rig.service.look(ME, way) is Reply.OK
    assert _looks(rig) == [
        ("look", 15.0, 0.0),
        ("look", 15.0, 15.0),
        ("look", 0.0, 15.0),
        ("look", 0.0, 0.0),
    ]


def test_首は範囲の端でそれ以上動かない(rig: _Rig) -> None:
    for _ in range(7):
        rig.service.look(ME, "right")
    for _ in range(3):
        rig.service.look(ME, "down")
    looks = _looks(rig)
    assert looks[4:7] == [("look", 75.0, 0.0), ("look", 80.0, 0.0), ("look", 80.0, 0.0)]
    assert looks[7:] == [("look", 80.0, -15.0), ("look", 80.0, -30.0), ("look", 80.0, -30.0)]
    # 端から戻るときは 1 回で戻る（押した分を貯めない）
    rig.service.look(ME, "left")
    assert _looks(rig)[-1] == ("look", 65.0, -30.0)


def test_首は左と上の端でも止まる(rig: _Rig) -> None:
    for _ in range(7):
        rig.service.look(ME, "left")
        rig.service.look(ME, "up")
    assert _looks(rig)[-1] == ("look", -80.0, 60.0)


def test_まんなかで正面に戻り_そこから数え直す(rig: _Rig) -> None:
    rig.service.look(ME, "right")
    assert rig.service.look(ME, "center") is Reply.OK
    rig.service.look(ME, "up")
    assert _looks(rig) == [("look", 15.0, 0.0), ("home",), ("look", 0.0, 15.0)]


def test_借り直したら首は正面から数える(rig: _Rig) -> None:
    rig.service.look(ME, "right")
    rig.ctx.preempt_latest()
    rig.service.look(ME, "right")
    assert _looks(rig) == [("look", 15.0, 0.0), ("look", 15.0, 0.0)]


# --- ブッブー（7.3） ---


def test_ブッブーは4つから選んで話す(rig: _Rig) -> None:
    for _ in range(20):
        assert rig.service.horn(ME) is Reply.OK
        _wait(lambda: not rig.service._speaking)
    said = {c[1] for c in rig.says()}
    assert said <= set(texts.SAY_HORNS)
    assert len(said) > 1
    assert all(c[2] is True for c in rig.ctx.calls if c[0] == "say")


def test_話している途中のブッブーは話さない(rig: _Rig) -> None:
    rig.ctx.speech.hold()
    rig.service.horn(ME)
    _wait(rig.ctx.speech.speaking.is_set)
    assert rig.service.horn(ME) is Reply.IGNORED
    rig.ctx.speech.release()
    _wait(lambda: not rig.service._speaking)
    assert len(rig.says()) == 1


# --- ダンス（7.4〜7.6） ---


class _Gate:
    """最初の sleep で止める。`open()` まで、ダンスは 1 段目のまま待つ。"""

    def __init__(self, rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
        self.waiting = threading.Event()
        self._open = threading.Event()
        original = rig.clock.sleep

        def sleep(seconds: float) -> None:
            self.waiting.set()
            if not self._open.wait(WAIT_S):
                raise AssertionError("門が開きませんでした")
            original(seconds)

        monkeypatch.setattr(rig.clock, "sleep", sleep)

    def open(self) -> None:
        self._open.set()


def _dance_looks(rig: _Rig) -> list[tuple[object, ...]]:
    return [c for c in rig.ctx.calls if c[0] == "look"]


def test_ダンスは走行を止めLCDを出し_首だけで踊って元の向きに戻る(rig: _Rig) -> None:
    rig.service.look(ME, "right")
    rig.service.drive(ME, Move.FWD)
    rig.step()
    assert rig.service.dance(ME) is Reply.OK
    _wait(lambda: not rig.service._dancing)
    calls = rig.ctx.calls
    start = calls.index(_show(texts.LCD_DANCE, texts.FACE_DANCE.text()))
    looks = [c for c in calls[start:] if c[0] == "look"]
    assert looks == [("look", 30.0, 15.0), ("look", -30.0, 15.0)] * 3 + [("look", 15.0, 0.0)]
    assert not [c for c in calls[start:] if c[0] in ("drive", "dance")]
    # 走る命令は STOP になっている
    rig.step()
    assert rig.moves()[-1] == ("stop",)


def test_ダンスはremote_danceのスレッドで踊る(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _Gate(rig, monkeypatch)
    rig.service.dance(ME)
    assert gate.waiting.wait(WAIT_S)
    names = {t.name: t for t in threading.enumerate()}
    assert "remote-dance" in names
    assert names["remote-dance"].daemon
    gate.open()
    _wait(lambda: not rig.service._dancing)


def test_踊っている間は走る命令とダンスの命令と首を受け付けない(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = _Gate(rig, monkeypatch)
    rig.service.dance(ME)
    assert gate.waiting.wait(WAIT_S)
    assert rig.service.drive(ME, Move.FWD) is Reply.IGNORED
    assert rig.service.drive(ME, Move.BACK) is Reply.IGNORED
    assert rig.service.dance(ME) is Reply.IGNORED
    assert rig.service.look(ME, "up") is Reply.IGNORED
    rig.step()
    assert rig.moves() == [("stop",)]
    gate.open()
    _wait(lambda: not rig.service._dancing)


@pytest.mark.parametrize(
    "interrupt",
    [
        lambda r: r.service.drive(ME, Move.STOP),
        lambda r: r.slot.drop(ME),
        lambda r: r.ctx.preempt_latest(),
        lambda r: r.service.close(),
    ],
    ids=["止める命令", "操作する端末がいない", "中断の知らせ", "閉じる"],
)
def test_ダンスは次の段に進まずに止まる(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, interrupt: Callable[[_Rig], object]
) -> None:
    gate = _Gate(rig, monkeypatch)
    rig.service.dance(ME)
    assert gate.waiting.wait(WAIT_S)
    lease = rig.leases.lease
    assert lease is not None
    assert rig.service._dance_go(lease, ME)
    interrupt(rig)
    assert not rig.service._dance_go(lease, ME)
    gate.open()
    _wait(lambda: not rig.service._dancing)
    # 1 段目のあと進まず、元の向きにも戻さない
    assert _dance_looks(rig) == [("look", 30.0, 15.0)]


def test_止める命令で止めたダンスの後は_また踊れる(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = _Gate(rig, monkeypatch)
    rig.service.dance(ME)
    assert gate.waiting.wait(WAIT_S)
    assert rig.service.drive(ME, Move.STOP) is Reply.OK
    gate.open()
    _wait(lambda: not rig.service._dancing)
    assert rig.service.dance(ME) is Reply.OK
    _wait(lambda: not rig.service._dancing)
    assert len(_dance_looks(rig)) == 1 + 7


def test_止めたダンスの後の首は_止めたときの向きから数える(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = _Gate(rig, monkeypatch)
    rig.service.dance(ME)
    assert gate.waiting.wait(WAIT_S)
    rig.service.drive(ME, Move.STOP)
    gate.open()
    _wait(lambda: not rig.service._dancing)
    assert rig.service.look(ME, "right") is Reply.OK
    assert _dance_looks(rig) == [("look", 30.0, 15.0), ("look", 45.0, 15.0)]


def test_ダンスの途中で取り上げられたら静かに終わる(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def look(self: FakeNeck, pan: float, tilt: float) -> None:
        raise LeaseRevoked("remote")

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(FakeNeck, "look", look)
    rig.service.dance(ME)
    _wait(lambda: not rig.service._dancing)
    assert not caplog.records


def test_ダンスが終われば走行の表示に戻る(rig: _Rig) -> None:
    rig.service.drive(ME, Move.STOP)
    rig.step()
    rig.service.dance(ME)
    _wait(lambda: not rig.service._dancing)
    rig.step()
    face = texts.FACE_DRIVE.text()
    assert rig.shows() == [
        _show(texts.LCD_IDLE, face),
        _show(texts.LCD_DANCE, texts.FACE_DANCE.text()),
        _show(texts.LCD_IDLE, face),
    ]


# --- 話させる（10.1〜10.5） ---


@pytest.mark.parametrize(
    ("given", "spoken"),
    [
        ("  こんにちは  ", "こんにちは"),
        ("a\nb\x7fc\x00d", "a b c d"),
        ("a\x85b\x9fc", "a b c"),
        ("\t\x1fやあ\r\n", "やあ"),
        ("あ" * 130, "あ" * 120),
        ("   " + "い" * 125, "い" * 120),
    ],
    ids=[
        "前後の空白",
        "制御文字",
        "C1の制御文字",
        "前後の制御文字",
        "120文字に切る",
        "空白を除いてから切る",
    ],
)
def test_話させる文字は整えてから話す(rig: _Rig, given: str, spoken: str) -> None:
    assert rig.service.say_text(ME, given) is Reply.OK
    _wait(lambda: not rig.service._speaking)
    assert ("say", spoken, False) in rig.ctx.calls


@pytest.mark.parametrize("given", ["", "   ", "\n\t\x00"], ids=["空", "空白", "制御文字"])
def test_空の文字は話さずに断る(rig: _Rig, given: str) -> None:
    assert rig.service.say_text(ME, given) is Reply.EMPTY
    assert not [c for c in rig.ctx.calls if c[0] in ("say", "show")]


def test_話している途中なら話させるを断る(rig: _Rig) -> None:
    rig.ctx.speech.hold()
    rig.service.horn(ME)
    _wait(rig.ctx.speech.speaking.is_set)
    assert rig.service.say_text(ME, "やあ") is Reply.BUSY_SPEAKING
    rig.ctx.speech.release()
    _wait(lambda: not rig.service._speaking)
    rig.ctx.speech.hold()
    rig.service.say_text(ME, "やあ")
    _wait(rig.ctx.speech.speaking.is_set)
    assert rig.service.say_text(ME, "やあ") is Reply.BUSY_SPEAKING
    assert rig.service.horn(ME) is Reply.IGNORED
    rig.ctx.speech.release()
    _wait(lambda: not rig.service._speaking)
    assert len(rig.says()) == 2


def test_話す間はLCDを出し_話し終えたら走行の表示に戻る(rig: _Rig) -> None:
    rig.ctx.speech.hold()
    assert rig.service.say_text(ME, "やあ") is Reply.OK
    _wait(rig.ctx.speech.speaking.is_set)
    talk = _show(texts.LCD_TALK, texts.FACE_TALK.text())
    assert rig.shows() == [talk]
    # 話している間も走れるが、LCD は話す表示のまま
    rig.service.drive(ME, Move.FWD)
    rig.step()
    assert rig.moves() == [("drive", 0.0, 20)]
    assert rig.shows() == [talk]
    rig.ctx.speech.release()
    _wait(lambda: not rig.service._speaking)
    rig.step()
    assert rig.shows() == [talk, _show(texts.LCD_FORWARD, texts.FACE_DRIVE.text())]


def test_話している間も危険のLCDを先に出し_危険が消えれば話す表示に戻る(rig: _Rig) -> None:
    rig.ctx.speech.hold()
    rig.service.say_text(ME, "やあ")
    _wait(rig.ctx.speech.speaking.is_set)
    talk = _show(texts.LCD_TALK, texts.FACE_TALK.text())
    stop = _show(texts.LCD_STOP, texts.FACE_STOP.text())
    rig.sensors.set(NEAR)
    rig.service.drive(ME, Move.FWD)
    rig.step()
    assert rig.moves() == [("stop",)]
    assert rig.shows() == [talk, stop]
    rig.step()
    assert rig.shows() == [talk, stop]  # 変わらなければ出し直さない
    rig.sensors.set(SensorReading(distance_cm=100.0, floor=(1000, 1000, 1000)))
    rig.service.drive(ME, Move.FWD)
    rig.step()
    assert rig.moves()[-1] == ("drive", 0.0, 20)
    assert rig.shows() == [talk, stop, talk]
    rig.ctx.speech.release()
    _wait(lambda: not rig.service._speaking)
    rig.step()
    assert rig.shows() == [talk, stop, talk, _show(texts.LCD_FORWARD, texts.FACE_DRIVE.text())]


def test_話させた文字をログにも記録にも残さない(
    rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    word = "ないしょの"
    rig.service.say_text(ME, word)
    _wait(lambda: not rig.service._speaking)
    rig.ctx.speech.error = RuntimeError("boom")
    rig.service.say_text(ME, word)
    _wait(lambda: not rig.service._speaking)
    assert caplog.records
    for text in (word, ME, "boom"):
        assert text not in caplog.text
    for value in vars(rig.service).values():
        assert word not in repr(value)


# --- 受け付けの順 ---


@pytest.mark.parametrize(
    "act",
    [
        lambda s: s.look(OTHER, "up"),
        lambda s: s.horn(OTHER),
        lambda s: s.dance(OTHER),
        lambda s: s.say_text(OTHER, "やあ"),
    ],
    ids=["首", "ブッブー", "ダンス", "話させる"],
)
def test_ほかの端末の首とブッブーとダンスと話させるは断る(
    rig: _Rig, act: Callable[[RemoteService], Reply]
) -> None:
    rig.service.drive(ME, Move.STOP)
    assert act(rig.service) is Reply.NOT_NOW
    assert rig.service.status(OTHER).message == texts.MSG_OTHER_OPERATOR
    assert not [c for c in rig.ctx.calls if c[0] in ("look", "say", "show")]


def test_おやすみの間は首とブッブーとダンスと話させるを断る(rig: _Rig) -> None:
    rig.ctx.request_quiet()
    assert rig.service.look(ME, "up") is Reply.NOT_NOW
    assert rig.service.horn(ME) is Reply.NOT_NOW
    assert rig.service.dance(ME) is Reply.NOT_NOW
    assert rig.service.say_text(ME, "やあ") is Reply.NOT_NOW
    assert rig.ctx.calls == []


# --- 写真（9.1〜9.4） ---

NECK_JPEG = b"neck-jpg"
USB_JPEG = b"usb-jpg"
NECK_NAME = "20260101-090000-00-neck.jpg"
USB_NAME = "20260101-090000-00-usb.jpg"


class _StillSource(FakeFrameSource):
    """撮った順を文脈の calls に覚える偽の映像の元。"""

    def __init__(self, ctx: FakeContext, name: str, still: bytes | None) -> None:
        super().__init__(still=still)
        self._ctx = ctx
        self._name = name

    def still(self) -> bytes | None:
        self._ctx._recorder.record("still", self._name)
        return super().still()


class _CamRig(_Rig):
    """首と USB の偽のカメラのハブを持つ。"""

    def __init__(
        self,
        tmp_path: Path,
        *,
        neck: bytes | None = NECK_JPEG,
        usb: bytes | None = USB_JPEG,
    ) -> None:
        clock = FakeClock()
        ctx = FakeContext(clock)
        self.neck_source = _StillSource(ctx, "neck", neck)
        self.usb_source = _StillSource(ctx, "usb", usb)
        self.neck_hub = FrameHub(Camera.NECK, self.neck_source, clock, run_in_thread=False)
        self.usb_hub = FrameHub(Camera.USB, self.usb_source, clock, run_in_thread=False)
        super().__init__(
            tmp_path,
            clock=clock,
            ctx=ctx,
            hubs={Camera.NECK: self.neck_hub, Camera.USB: self.usb_hub},
        )

    def level(self) -> frozenset[Resource] | None:
        lease = self.leases.lease
        return None if lease is None else lease.resources


@pytest.fixture
def cams(tmp_path: Path) -> _CamRig:
    return _CamRig(tmp_path)


def _photo_in_thread(rig: _Rig) -> tuple[threading.Thread, list[PhotoResult]]:
    results: list[PhotoResult] = []
    thread = threading.Thread(target=lambda: results.append(rig.service.take_photo(ME)))
    thread.start()
    return thread, results


def test_写真は走行を止めてLCDを出し_話し終えてから首とUSBを撮って保存する(
    cams: _CamRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    cams.service.drive(ME, Move.FWD)
    cams.step()
    before = len(cams.ctx.calls)
    result = cams.service.take_photo(ME)
    assert result == PhotoResult((NECK_NAME, USB_NAME), Reply.OK)
    calls = [c for c in cams.ctx.calls[before:] if c[0] in ("stop", "show", "say", "said", "still")]
    assert calls == [
        ("stop",),
        _show(texts.LCD_PHOTO, texts.FACE_DRIVE.text()),
        ("say", texts.SAY_PHOTO_CHEESE, True),
        ("said", texts.SAY_PHOTO_CHEESE),
        ("still", "neck"),
        ("still", "usb"),
        ("say", texts.SAY_PHOTO_DONE, True),
        ("said", texts.SAY_PHOTO_DONE),
    ]
    assert (cams.photo_dir / NECK_NAME).read_bytes() == NECK_JPEG
    assert (cams.photo_dir / USB_NAME).read_bytes() == USB_JPEG
    # 走る命令は STOP になっている（9.1、4.3）
    cams.step()
    assert cams.moves()[-1] == ("stop",)
    assert NECK_NAME not in caplog.text
    assert USB_NAME not in caplog.text


def test_写真は撮れたカメラの分だけ返す(tmp_path: Path) -> None:
    rig = _CamRig(tmp_path, neck=None)
    assert rig.service.take_photo(ME) == PhotoResult((USB_NAME,), Reply.OK)
    assert rig.says()[-1] == ("said", texts.SAY_PHOTO_DONE)


def test_1枚も撮れなければ謝ってFAILEDを返す(tmp_path: Path) -> None:
    rig = _CamRig(tmp_path, neck=None, usb=None)
    assert rig.service.take_photo(ME) == PhotoResult((), Reply.FAILED)
    assert rig.says()[-1] == ("said", texts.SAY_PHOTO_FAILED)
    assert list(tmp_path.glob("*.jpg")) == []


def test_撮っている途中の次の写真の要求は無視する(cams: _CamRig) -> None:
    cams.ctx.speech.hold()
    thread, results = _photo_in_thread(cams)
    assert cams.ctx.speech.speaking.wait(WAIT_S)
    assert cams.service.take_photo(ME) == PhotoResult((), Reply.IGNORED)
    cams.ctx.speech.release()
    thread.join(WAIT_S)
    assert results == [PhotoResult((NECK_NAME, USB_NAME), Reply.OK)]
    # 終われば、また撮れる
    assert cams.service.take_photo(ME).reply is Reply.OK


@pytest.mark.parametrize(
    "interrupt",
    [lambda r: r.ctx.preempt_latest(), lambda r: r.service.halt()],
    ids=["中断の知らせ", "止め方"],
)
def test_写真の途中で止められたら撮らずに話さない(
    cams: _CamRig, interrupt: Callable[[_Rig], object]
) -> None:
    cams.ctx.speech.hold()
    thread, results = _photo_in_thread(cams)
    assert cams.ctx.speech.speaking.wait(WAIT_S)
    interrupt(cams)
    cams.ctx.speech.release()
    thread.join(WAIT_S)
    assert results == [PhotoResult((), Reply.FAILED)]
    assert [c for c in cams.ctx.calls if c[0] == "still"] == []
    assert cams.says() == [("said", texts.SAY_PHOTO_CHEESE)]


def test_ほかの端末の写真は断る(cams: _CamRig) -> None:
    cams.service.drive(OTHER, Move.STOP)
    assert cams.service.take_photo(ME) == PhotoResult((), Reply.NOT_NOW)
    assert [c for c in cams.ctx.calls if c[0] == "still"] == []


# --- 定期の確かめと止め方（3.3、3.4、11.4、12.2、12.4、12.6） ---


@pytest.mark.parametrize(
    "setup",
    [
        lambda r: r.clock.set_now(datetime(2026, 1, 1, 20, 0, tzinfo=JST)),
        lambda r: r.ctx.request_quiet(),
        lambda r: r.clock.set_synchronized(False),
    ],
    ids=["時間帯の終わり", "おやすみ", "時刻が確かでない"],
)
def test_使えなくなれば止めてリースと首のカメラを返す(
    cams: _CamRig, setup: Callable[[_Rig], None]
) -> None:
    cams.service.drive(ME, Move.FWD)
    cams.step()
    setup(cams)
    cams.service.tick()
    assert cams.moves() == [("drive", 0.0, 20), ("stop",)]
    assert cams.leases.lease is None
    assert ("release",) in cams.ctx.calls
    assert not cams.neck_hub.available()
    assert cams.neck_source.closes >= 1
    assert not cams.service.may_stream(ME)
    assert not cams.step()


def test_時間帯の終わりの知らせを端末に出す(cams: _CamRig) -> None:
    cams.service.drive(ME, Move.FWD)
    cams.clock.set_now(datetime(2026, 1, 1, 20, 0, tzinfo=JST))
    cams.service.tick()
    assert cams.service.status(ME).message == texts.MSG_OUT_OF_HOURS


@pytest.mark.parametrize(
    "interrupt",
    [
        lambda r: r.service.halt(),
        lambda r: r.service.on_lost(),
        lambda r: (r.ctx.request_quiet(), r.service.tick()),
    ],
    ids=["止め方", "中断の後片付け", "おやすみの確かめ"],
)
def test_止め方でダンスは次の段に進まない(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, interrupt: Callable[[_Rig], object]
) -> None:
    gate = _Gate(rig, monkeypatch)
    rig.service.dance(ME)
    assert gate.waiting.wait(WAIT_S)
    lease = rig.leases.lease
    assert lease is not None
    interrupt(rig)
    assert not rig.service._dance_go(lease, ME)
    gate.open()
    _wait(lambda: not rig.service._dancing)
    assert _dance_looks(rig) == [("look", 30.0, 15.0)]


def test_止め方は走行を止めて首のカメラを返す(cams: _CamRig) -> None:
    cams.service.drive(ME, Move.FWD)
    cams.step()
    cams.service.halt()
    assert cams.moves()[-1] == ("stop",)
    assert not cams.neck_hub.available()
    cams.step()
    assert cams.moves()[-1] == ("stop",)


def test_中断の後片付けは話さず待たず借りない(cams: _CamRig) -> None:
    cams.service.drive(ME, Move.FWD)
    cams.step()
    cams.ctx.preempt_latest()
    after = cams.ctx.calls[cams.ctx.calls.index(("preempt",)) :]
    assert [c for c in after if c[0] in ("say", "acquire")] == []
    assert not cams.neck_hub.available()
    assert not cams.service.neck_allowed()


def test_10秒の交代で走行を止め_首を見る端末がいなければリースを返す(cams: _CamRig) -> None:
    cams.service.drive(ME, Move.FWD)
    cams.step()
    cams.clock.advance(OperatorSlot.IDLE_SECONDS)
    cams.service.tick()
    assert cams.slot.holder() is None
    assert ("stop",) in cams.moves()
    assert cams.level() is None
    assert not cams.neck_hub.available()


def test_操作する端末のログインが切れたら席を空けて走行を止める(cams: _CamRig) -> None:
    cams.service.drive(ME, Move.FWD)
    cams.step()
    cams.service.drop_operator(ME)
    assert cams.slot.holder() is None
    assert cams.moves()[-1] == ("stop",)
    cams.step()
    assert cams.moves()[-1] == ("stop",)


def test_操作する端末でない印のログインが切れても走行は止めない(cams: _CamRig) -> None:
    cams.service.drive(ME, Move.FWD)
    cams.step()
    cams.service.drop_operator(OTHER)
    assert cams.slot.holder() == ME
    assert cams.moves() == [("drive", 0.0, 20)]


def test_10秒の交代で首を見る端末がいれば見るだけの段に下げる(cams: _CamRig) -> None:
    cams.service.drive(ME, Move.FWD)
    cams.step()
    with cams.neck_hub.viewer():
        cams.clock.advance(OperatorSlot.IDLE_SECONDS)
        cams.service.tick()
        assert ("stop",) in cams.moves()
        assert cams.level() == frozenset({Resource.NECK})
        assert cams.service.neck_allowed()
    assert not cams.step()


def test_交代の前は段を下げない(cams: _CamRig) -> None:
    cams.service.drive(ME, Move.FWD)
    cams.clock.advance(OperatorSlot.IDLE_SECONDS - 0.5)
    cams.service.tick()
    assert cams.slot.holder() == ME
    level = cams.level()
    assert level is not None
    assert Resource.DRIVE in level


def test_首を見る端末が来れば見るだけの段を借り_いなくなれば返す(cams: _CamRig) -> None:
    with cams.neck_hub.viewer():
        cams.service.tick()
        assert cams.level() == frozenset({Resource.NECK})
        assert cams.service.neck_allowed()
    cams.service.tick()
    assert cams.level() is None
    assert not cams.service.neck_allowed()
    assert not cams.neck_hub.available()


def test_見るだけの段を借りられなければ5秒おいて借り直す(cams: _CamRig) -> None:
    cams.ctx.busy = {Resource.NECK}
    with cams.neck_hub.viewer():
        cams.service.tick()
        assert cams.level() is None
        cams.ctx.busy = set()
        cams.clock.advance(LeaseKeeper.RETRY_SECONDS - 0.5)
        cams.service.tick()
        assert cams.level() is None
        cams.clock.advance(0.5)
        cams.service.tick()
        assert cams.level() == frozenset({Resource.NECK})


def test_確かめでカメラのハブも進める(cams: _CamRig, monkeypatch: pytest.MonkeyPatch) -> None:
    ticked: list[Camera] = []
    for hub in (cams.neck_hub, cams.usb_hub):
        monkeypatch.setattr(hub, "tick", lambda c=hub.camera: ticked.append(c))
    cams.service.tick()
    assert ticked == [Camera.NECK, Camera.USB]


def test_確かめの思わぬ失敗は型の名前だけ残して走行を止める(
    cams: _CamRig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cams.service.drive(ME, Move.FWD)
    cams.step()

    def broken() -> bool:
        raise RuntimeError("hidden3")

    monkeypatch.setattr(cams.slot, "expire", broken)
    cams.service.tick()
    assert cams.moves()[-1] == ("stop",)
    assert "RuntimeError" in caplog.text
    assert "hidden3" not in caplog.text
    cams.step()
    assert cams.moves()[-1] == ("stop",)


# --- 状態の知らせ（3.2、8.3、8.6、12.2） ---


def test_状態は操作する端末と速さとカメラと踊りを返す(cams: _CamRig) -> None:
    cams.service.set_fast(ME, True)
    cams.usb_hub._available = False
    assert cams.service.status(ME) == Status(
        message=None,
        operator="me",
        fast=True,
        cameras={Camera.NECK: True, Camera.USB: False},
        dancing=False,
    )
    other = cams.service.status(OTHER)
    assert other.operator == "other"
    assert other.message == texts.MSG_OTHER_OPERATOR


def test_操作する端末がいなければnone(cams: _CamRig) -> None:
    status = cams.service.status(ME)
    assert status.operator == "none"
    assert status.message is None
    assert not status.fast


def test_中断の後はほかのことをしている知らせ(cams: _CamRig) -> None:
    cams.service.drive(ME, Move.FWD)
    cams.ctx.preempt_latest()
    assert cams.service.status(ME).message == texts.MSG_BUSY


def test_危険で止めた知らせは操作する端末に出す(cams: _CamRig) -> None:
    cams.sensors.set(NEAR)
    cams.service.drive(ME, Move.FWD)
    cams.step()
    assert cams.service.status(ME).message == texts.MSG_DANGER_OBSTACLE
    assert cams.service.status(OTHER).message == texts.MSG_OTHER_OPERATOR
    _wait(lambda: not cams.service._speaking)


def test_踊っている間は状態に出す(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _Gate(rig, monkeypatch)
    rig.service.dance(ME)
    assert gate.waiting.wait(WAIT_S)
    assert rig.service.status(ME).dancing
    gate.open()
    _wait(lambda: not rig.service._dancing)
    assert not rig.service.status(ME).dancing


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (
            lambda r: r.clock.set_now(datetime(2026, 1, 1, 6, 59, tzinfo=JST)),
            texts.MSG_OUT_OF_HOURS,
        ),
        (lambda r: r.ctx.request_quiet(), texts.MSG_QUIET),
        (lambda r: r.clock.set_synchronized(False), texts.MSG_NO_CLOCK),
    ],
    ids=["時間帯の外", "おやすみ", "時刻が確かでない"],
)
def test_使えないときは映像を止め_知らせを出す(
    cams: _CamRig, setup: Callable[[_Rig], None], message: str
) -> None:
    assert cams.service.may_stream(ME)
    setup(cams)
    assert not cams.service.may_stream(ME)
    assert cams.service.status(ME).message == message
