"""首と走行の操作（MotionService）のテスト。"""

import logging
import threading

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.adapters.fake.devices import FakeMotors, FakeServos
from osekkai.core.config import Calibration, MotionSettings
from osekkai.core.motion import MotionService
from osekkai.core.ports import Unavailable
from osekkai.core.types import DeviceUnavailable, MotionAborted


def _always() -> bool:
    return True


def _service(
    fakes: FakeDeviceSet, clock: FakeClock, settings: MotionSettings | None = None
) -> MotionService:
    return MotionService(fakes.servos, fakes.motors, settings or MotionSettings(), clock)


def _angles(servos: FakeServos) -> list[tuple[object, ...]]:
    return [c.args for c in servos.calls if c.op == "set_angle"]


def _speeds(motors: FakeMotors) -> list[tuple[object, ...]]:
    return [c.args for c in motors.calls if c.op == "set_speeds"]


# --- 範囲と調整値（8.1、8.4、8.5） ---


def test_範囲の外の首の向きは端に収まる(fakes: FakeDeviceSet, clock: FakeClock) -> None:
    motion = _service(fakes, clock)
    motion.look(120, -50)
    assert fakes.servos.angles == {"pan": 80.0, "tilt": -30.0}
    motion.look(-200, 100)
    assert fakes.servos.angles == {"pan": -80.0, "tilt": 60.0}


def test_首の範囲は設定から読む(fakes: FakeDeviceSet, clock: FakeClock) -> None:
    settings = MotionSettings(pan_range=(-40.0, 40.0), tilt_range=(-10.0, 20.0))
    motion = _service(fakes, clock, settings)
    motion.look(50, 50)
    assert fakes.servos.angles == {"pan": 40.0, "tilt": 20.0}


def test_走行のステアリングと速さは範囲に収まる(fakes: FakeDeviceSet, clock: FakeClock) -> None:
    motion = _service(fakes, clock)
    motion.drive(45, 150)
    assert fakes.servos.angles["steering"] == 30.0
    assert fakes.motors.speeds == (100, 100)
    motion.drive(-45, -150)
    assert fakes.servos.angles["steering"] == -30.0
    assert fakes.motors.speeds == (-100, -100)


def test_走行はステアリングとモーターをその向きと速さにする(
    fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    motion = _service(fakes, clock)
    motion.drive(10, 40)
    assert fakes.servos.angles["steering"] == 10.0
    assert fakes.motors.speeds == (40, 40)
    motion.stop()
    assert fakes.motors.speeds == (0, 0)


def test_調整値がすべての指令に加わる(fakes: FakeDeviceSet, clock: FakeClock) -> None:
    calibration = Calibration(steering=3, pan=-5, tilt=2, motor_direction=(1, -1))
    motion = _service(fakes, clock, MotionSettings(calibration=calibration))

    motion.look(10, 10)
    assert fakes.servos.angles == {"pan": 5.0, "tilt": 12.0}

    # 範囲に収めてから、正面のずれを足す
    motion.look(100, -100)
    assert fakes.servos.angles == {"pan": 75.0, "tilt": -28.0}

    motion.home()
    assert fakes.servos.angles == {"pan": -5.0, "tilt": 2.0, "steering": 3.0}

    motion.drive(40, 30)
    assert fakes.servos.angles["steering"] == 33.0
    assert fakes.motors.speeds == (30, -30)


# --- 基本の動き（8.2） ---


def test_正面に戻すと首とステアリングが0になる(fakes: FakeDeviceSet, clock: FakeClock) -> None:
    motion = _service(fakes, clock)
    motion.look(30, 30)
    motion.home()
    assert fakes.servos.angles == {"pan": 0.0, "tilt": 0.0, "steering": 0.0}


def test_うなずくはチルトを0_25秒ずつ動かして正面に戻る(
    fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    motion = _service(fakes, clock)
    motion.nod(2, _always)
    tilts = [a[1] for a in _angles(fakes.servos) if a[0] == "tilt"]
    assert tilts[:4] == [-20.0, 10.0, -20.0, 10.0]
    assert clock.sleeps[:4] == [0.25, 0.25, 0.25, 0.25]
    # 首だけを戻し、走行の側のステアリングには触らない
    assert fakes.servos.angles == {"pan": 0.0, "tilt": 0.0}


def test_見回すはパンを0_35秒ずつ動かす(fakes: FakeDeviceSet, clock: FakeClock) -> None:
    motion = _service(fakes, clock)
    motion.look_around(_always)
    assert _angles(fakes.servos) == [("pan", -30.0), ("pan", 30.0), ("pan", 0.0)]
    assert clock.sleeps == [0.35, 0.35, 0.35]


def test_ねむそうはパン0とチルトマイナス30(fakes: FakeDeviceSet, clock: FakeClock) -> None:
    motion = _service(fakes, clock)
    motion.sleepy()
    assert fakes.servos.angles == {"pan": 0.0, "tilt": -30.0}


def test_ダンスは6拍してモーターを止めて正面に戻る(fakes: FakeDeviceSet, clock: FakeClock) -> None:
    motion = _service(fakes, clock)
    motion.dance(wheels=False, should_continue=_always)
    pans = [a[1] for a in _angles(fakes.servos) if a[0] == "pan"]
    steerings = [a[1] for a in _angles(fakes.servos) if a[0] == "steering"]
    tilts = [a[1] for a in _angles(fakes.servos) if a[0] == "tilt"]
    assert pans[:6] == [30.0, -30.0, 30.0, -30.0, 30.0, -30.0]
    assert steerings[:6] == [-25.0, 25.0, -25.0, 25.0, -25.0, 25.0]
    assert tilts[:6] == [15.0] * 6
    assert clock.sleeps == [0.25] * 6
    assert fakes.motors.speeds == (0, 0)
    assert fakes.servos.angles == {"pan": 0.0, "tilt": 0.0, "steering": 0.0}


# --- ダンスのタイヤ（8.3） ---


@pytest.mark.parametrize(
    ("allow", "wheels"),
    [(False, False), (False, True), (True, False)],
)
def test_許可がなければダンスでモーターに速さが出ない(
    fakes: FakeDeviceSet, clock: FakeClock, allow: bool, wheels: bool
) -> None:
    motion = _service(fakes, clock, MotionSettings(allow_drive_in_dance=allow))
    motion.dance(wheels=wheels, should_continue=_always)
    assert all(s == (0, 0) for s in _speeds(fakes.motors))


def test_許可があり走行を使えるときだけダンスで前後に動く(
    fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    motion = _service(fakes, clock, MotionSettings(allow_drive_in_dance=True))
    motion.dance(wheels=True, should_continue=_always)
    speeds = _speeds(fakes.motors)
    assert speeds[:6] == [(20, 20), (-20, -20)] * 3
    assert speeds[-1] == (0, 0)


def test_タイヤを使わないダンスはほかの持ち主の走行を止めない(
    fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    motion = _service(fakes, clock, MotionSettings(allow_drive_in_dance=True))
    motion.drive(0, 50)
    motion.dance(wheels=False, should_continue=_always)
    assert _speeds(fakes.motors) == [(50, 50)]
    assert "stop_now" not in fakes.motors.ops()
    assert fakes.motors.speeds == (50, 50)


# --- 途中の中止（4.4、4.9） ---


def test_途中で中止すると残りの段階を行わずモーターを止める(
    fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    motion = _service(fakes, clock, MotionSettings(allow_drive_in_dance=True))
    checks = iter([True, True, False])
    with pytest.raises(MotionAborted):
        motion.dance(wheels=True, should_continue=lambda: next(checks))
    # 2 拍だけ動き、正面には戻さない
    assert [a[1] for a in _angles(fakes.servos) if a[0] == "pan"] == [30.0, -30.0]
    assert fakes.servos.angles["pan"] == -30.0
    assert fakes.motors.ops()[-1] == "stop_now"
    assert fakes.motors.speeds == (0, 0)


def test_うなずきの途中で中止すると正面に戻さない(fakes: FakeDeviceSet, clock: FakeClock) -> None:
    motion = _service(fakes, clock)
    checks = iter([True, False])
    with pytest.raises(MotionAborted):
        motion.nod(3, lambda: next(checks))
    assert _angles(fakes.servos) == [("tilt", -20.0)]
    assert "stop_now" in fakes.motors.ops()


def test_見回しの途中で中止すると残りの段階を行わない(
    fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    motion = _service(fakes, clock)
    with pytest.raises(MotionAborted):
        motion.look_around(lambda: False)
    assert _angles(fakes.servos) == []


# --- 非常停止（2.1） ---


def test_非常停止は動きの途中でもロックを待たずに止める(
    fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    motion = _service(fakes, clock)
    finished: list[bool] = []

    def stop_from_other_thread() -> None:
        motion.emergency_stop()
        finished.append(True)

    def check() -> bool:
        # 動きがロックを持っている間に、別のスレッドから非常停止する
        thread = threading.Thread(target=stop_from_other_thread)
        thread.start()
        thread.join(timeout=2.0)
        finished.append(not thread.is_alive())
        return True

    motion.look_around(check)
    assert finished == [True, True] * 3
    assert fakes.motors.ops().count("stop_now") == 3


class _BlockingClock(FakeClock):
    """`sleep()` の中で、テストが `release` を立てるまで止まる時計。"""

    def __init__(self) -> None:
        super().__init__()
        self.sleeping = threading.Event()
        self.release = threading.Event()

    def sleep(self, seconds: float) -> None:
        self.sleeping.set()
        self.release.wait(timeout=5.0)
        super().sleep(seconds)


@pytest.mark.parametrize("move", ["nod", "look_around", "dance"])
def test_段階のある動きが待つ間もほかの停止と走行は待たされない(move: str) -> None:
    clock = _BlockingClock()
    fakes = FakeDeviceSet.create(clock)
    motion = MotionService(fakes.servos, fakes.motors, MotionSettings(), clock)

    def run() -> None:
        if move == "nod":
            motion.nod(100, _always)
        elif move == "look_around":
            motion.look_around(_always)
        else:
            motion.dance(wheels=False, should_continue=_always)

    mover = threading.Thread(target=run)
    mover.start()
    try:
        assert clock.sleeping.wait(timeout=2.0)

        def drive_then_stop() -> None:
            motion.drive(0, 50)
            motion.stop()

        other = threading.Thread(target=drive_then_stop)
        other.start()
        other.join(timeout=2.0)
        assert not other.is_alive()
        assert mover.is_alive()  # 動きはまだ終わっていない
        assert _speeds(fakes.motors) == [(50, 50), (0, 0)]
    finally:
        clock.release.set()
        mover.join(timeout=5.0)
    assert not mover.is_alive()


def test_非常停止は何度呼んでもよい(fakes: FakeDeviceSet, clock: FakeClock) -> None:
    motion = _service(fakes, clock)
    motion.emergency_stop()
    motion.emergency_stop()
    assert fakes.motors.ops() == ["stop_now", "stop_now"]
    assert fakes.motors.speeds == (0, 0)


def test_非常停止は失敗を外に出さず2回まで再試行する(
    fakes: FakeDeviceSet, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    motion = _service(fakes, clock)
    fakes.motors.fail_on.add("stop_now")
    with caplog.at_level(logging.WARNING, logger="osekkai"):
        motion.emergency_stop()
    assert fakes.motors.ops() == ["stop_now"] * 3
    assert "FakeDeviceError" in caplog.text


def test_非常停止はモーターが使えなくても例外を出さない(clock: FakeClock) -> None:
    motion = MotionService(FakeServos(clock), Unavailable("motors", "x"), MotionSettings(), clock)
    motion.emergency_stop()


# --- 使えないデバイスと書き込みの失敗 ---


def test_使えないデバイスへの操作はデバイスが使えないになる(
    fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    motion = MotionService(
        Unavailable("servos", "x"), Unavailable("motors", "x"), MotionSettings(), clock
    )
    with pytest.raises(DeviceUnavailable):
        motion.look(0, 0)
    with pytest.raises(DeviceUnavailable):
        motion.drive(0, 10)
    with pytest.raises(DeviceUnavailable):
        motion.stop()


def test_書き込みの失敗はデバイスが使えないになる(fakes: FakeDeviceSet, clock: FakeClock) -> None:
    motion = _service(fakes, clock)
    fakes.servos.fail_on.add("set_angle")
    with pytest.raises(DeviceUnavailable):
        motion.look(0, 0)
    fakes.servos.fail_on.clear()
    fakes.motors.fail_on.add("set_speeds")
    with pytest.raises(DeviceUnavailable):
        motion.drive(0, 10)
    # 失敗の後も次の操作は動く
    fakes.motors.fail_on.clear()
    motion.drive(0, 10)
    assert fakes.motors.speeds == (10, 10)


def test_首だけを正面に戻してもステアリングは変えない(
    fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    motion = _service(fakes, clock)
    motion.drive(20, 50)
    motion.look(40, 20)
    motion.home_neck()
    assert fakes.servos.angles == {"pan": 0.0, "tilt": 0.0, "steering": 20.0}
    assert fakes.motors.speeds == (50, 50)
