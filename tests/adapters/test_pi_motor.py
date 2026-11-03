"""モーターのアダプタと、安全停止の実機の部品のテスト。

本物の HatMcu に偽の smbus を渡し、書いたレジスタと方向ピンを確かめる。
smbus2 と lgpio は読み込まない。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from osekkai.adapters.pi.hat import HatMcu
from osekkai.adapters.pi.i2c_bus import I2cBus
from osekkai.adapters.pi.picarx import (
    LEFT_DIR_PIN,
    MOTOR_PERIOD,
    MOTOR_PRESCALER,
    RIGHT_DIR_PIN,
    MotorMappingUnverified,
    PiCarXMotors,
    PiSafeStop,
    speed_to_duty,
)
from osekkai.core.ports import MotorPort

V5_UUID = "9daeea78-0000-076e-0032-582369ac3e02"
REG_P12 = 0x20 + 12
REG_P13 = 0x20 + 13
REG_PSC_T3 = 0x40 + 3
REG_ARR_T3 = 0x44 + 3


class Log:
    """I2C と GPIO の操作を 1 本の並びに記録する。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, int, int]] = []


class FakeMsg:
    def __init__(self, addr: int, data: list[int]) -> None:
        self.addr = addr
        self.data = list(data)


class FakeSMBus:
    """smbus2 と同じく、block や byte の書き込みは共有の相手（I2C_SLAVE）を覚えて変える。"""

    def __init__(self, log: Log) -> None:
        self.log = log
        self.fail_regs: set[int] = set()
        self.address: int | None = None
        self.messages: list[FakeMsg] = []

    def _set_address(self, addr: int) -> None:
        self.address = addr

    def write_quick(self, addr: int) -> None:
        self._set_address(addr)
        if addr != 0x14:
            raise OSError(121, "Remote I/O error")

    def write_byte(self, addr: int, value: int) -> None:
        self._set_address(addr)

    def write_i2c_block_data(self, addr: int, reg: int, data: list[int]) -> None:
        self._set_address(addr)
        if reg in self.fail_regs:
            raise OSError(5, "Input/output error")
        self.log.events.append(("i2c", reg, (data[0] << 8) | data[1]))

    def i2c_rdwr(self, *msgs: FakeMsg) -> None:
        # 1 通ずつ相手を持つ。共有の相手は変えない
        for msg in msgs:
            self.messages.append(msg)
            reg, hi, lo = msg.data
            if reg in self.fail_regs:
                raise OSError(5, "Input/output error")
            self.log.events.append(("rdwr", reg, (hi << 8) | lo))

    def close(self) -> None:
        pass


class FakePins:
    def __init__(self, log: Log) -> None:
        self.log = log
        self.claimed: dict[int, int] = {}
        self.fail = False

    def claim_output(self, pin: int, level: int) -> None:
        if self.fail:
            raise OSError("claim failed")
        self.claimed.setdefault(pin, level)
        self.log.events.append(("claim", pin, level))

    def write(self, pin: int, level: int) -> None:
        if self.fail:
            raise OSError("write failed")
        self.log.events.append(("gpio", pin, level))

    def close(self) -> None:
        self.log.events.append(("close", -1, -1))


def _hat(tmp_path: Path, log: Log, *, v5: bool = False) -> tuple[HatMcu, FakeSMBus, FakePins]:
    if v5:
        folder = tmp_path / "hat"
        folder.mkdir()
        (folder / "uuid").write_text(V5_UUID + "\x00")
    smbus = FakeSMBus(log)
    pins = FakePins(log)
    bus = I2cBus(opener=lambda n: smbus, message_writer=FakeMsg)
    hat = HatMcu(bus, pins, sleep=lambda s: None, device_tree=tmp_path)
    return hat, smbus, pins


def _motors(tmp_path: Path, driver: str = "auto") -> tuple[PiCarXMotors, Log]:
    log = Log()
    hat, _, pins = _hat(tmp_path, log)
    motors = PiCarXMotors(hat, pins, motor_driver=driver)  # type: ignore[arg-type]
    log.events.clear()
    return motors, log


# ---- 速さの変換 ----


@pytest.mark.parametrize(
    ("speed", "percent", "reverse"),
    [
        (0, 0, False),
        (1, 50, False),
        (-1, 50, True),
        (2, 51, False),
        (50, 75, False),
        (-50, 75, True),
        (100, 100, False),
        (-100, 100, True),
        (150, 100, False),
        (-150, 100, True),
    ],
)
def test_速さはデューティと向きに変わる(speed: int, percent: int, reverse: bool) -> None:
    assert speed_to_duty(speed) == (percent, reverse)


def test_周期とプリスケーラは実機で確かめた値() -> None:
    assert (MOTOR_PERIOD, MOTOR_PRESCALER) == (4095, 10)
    assert (LEFT_DIR_PIN, RIGHT_DIR_PIN) == (23, 24)


# ---- 作るとき ----


def test_モーターのアダプタはMotorPortを満たす(tmp_path: Path) -> None:
    motors, _ = _motors(tmp_path)
    port: MotorPort = motors
    assert port is motors


def test_作るときに周期を先に書きプリスケーラを書いて止めておく(tmp_path: Path) -> None:
    log = Log()
    hat, _, pins = _hat(tmp_path, log)
    log.events.clear()
    PiCarXMotors(hat, pins, motor_driver="pwm_dir")
    i2c = [e for e in log.events if e[0] == "i2c"]
    # P12 と P13 は同じタイマーなので 1 回だけ。プリスケーラは「値 − 1」
    assert i2c[:2] == [("i2c", REG_ARR_T3, 4095), ("i2c", REG_PSC_T3, 9)]
    assert set(i2c[2:]) == {("i2c", REG_P12, 0), ("i2c", REG_P13, 0)}
    assert pins.claimed == {23: 0, 24: 0}


def test_自動はv4ならPWMと方向ピンにする(tmp_path: Path) -> None:
    motors, _ = _motors(tmp_path, "auto")
    assert motors.mode == "pwm_dir"


def test_PWM2本を選ぶと未確認なので断る(tmp_path: Path) -> None:
    log = Log()
    hat, _, pins = _hat(tmp_path, log)
    with pytest.raises(MotorMappingUnverified, match="unverified"):
        PiCarXMotors(hat, pins, motor_driver="dual_pwm")
    assert not [e for e in log.events if e[1] in (REG_P12, REG_P13)]


def test_自動でv5と判定すると未確認なので断る(tmp_path: Path) -> None:
    log = Log()
    hat, _, pins = _hat(tmp_path, log, v5=True)
    assert hat.hat_version() == "v5"
    with pytest.raises(MotorMappingUnverified, match="v5 motor mapping is unverified"):
        PiCarXMotors(hat, pins, motor_driver="auto")


def test_知らない方式は断る(tmp_path: Path) -> None:
    log = Log()
    hat, _, pins = _hat(tmp_path, log)
    with pytest.raises(ValueError):
        PiCarXMotors(hat, pins, motor_driver="turbo")  # type: ignore[arg-type]


# ---- 走行 ----


def test_前進は左の方向ピンをLowに右をHighにする(tmp_path: Path) -> None:
    motors, log = _motors(tmp_path)
    motors.set_speeds(50, 50)
    assert log.events == [
        ("gpio", 23, 0),
        ("i2c", REG_P13, 3071),  # 75 % × 4095 = 3071.25 → 切り捨て
        ("gpio", 24, 1),
        ("i2c", REG_P12, 3071),
    ]


def test_後退は左をHighに右をLowにする(tmp_path: Path) -> None:
    motors, log = _motors(tmp_path)
    motors.set_speeds(-100, -100)
    assert log.events == [
        ("gpio", 23, 1),
        ("i2c", REG_P13, 4095),
        ("gpio", 24, 0),
        ("i2c", REG_P12, 4095),
    ]


def test_速さ0はデューティ0で方向ピンはLow(tmp_path: Path) -> None:
    motors, log = _motors(tmp_path)
    motors.set_speeds(0, 0)
    assert ("i2c", REG_P13, 0) in log.events
    assert ("i2c", REG_P12, 0) in log.events
    assert ("gpio", 23, 0) in log.events
    # 右は反転するが、0 は 0 のまま（picar-x は 0 で方向ピンを Low にする）
    assert ("gpio", 24, 0) in log.events


def test_速さ1はデューティ50パーセント(tmp_path: Path) -> None:
    motors, log = _motors(tmp_path)
    motors.set_speeds(1, -1)
    assert ("i2c", REG_P13, 2047) in log.events
    assert ("i2c", REG_P12, 2047) in log.events
    # 右に −1 は、反転して +1 → 方向ピン Low
    assert ("gpio", 24, 0) in log.events


def test_範囲の外の速さは端に収める(tmp_path: Path) -> None:
    motors, log = _motors(tmp_path)
    motors.set_speeds(250, -250)
    assert ("i2c", REG_P13, 4095) in log.events
    assert ("i2c", REG_P12, 4095) in log.events


def test_書き込みの失敗はOSErrorのまま投げる(tmp_path: Path) -> None:
    log = Log()
    hat, smbus, pins = _hat(tmp_path, log)
    motors = PiCarXMotors(hat, pins, motor_driver="pwm_dir")
    smbus.fail_regs.add(REG_P13)
    with pytest.raises(OSError):
        motors.set_speeds(10, 10)


# ---- 最速の停止 ----


def test_止めるとPWMの両方に0を書く(tmp_path: Path) -> None:
    motors, log = _motors(tmp_path)
    motors.set_speeds(80, 80)
    log.events.clear()
    motors.stop_now()
    assert set(log.events) == {("rdwr", REG_P12, 0), ("rdwr", REG_P13, 0)}


def test_非常停止の書き込みは共有の相手のアドレスを変えずI2C_RDWRで送る(tmp_path: Path) -> None:
    log = Log()
    hat, smbus, pins = _hat(tmp_path, log)
    motors = PiCarXMotors(hat, pins, motor_driver="pwm_dir")
    smbus.address = 0x27  # LCD のスレッドが相手を 0x27 にした直後
    motors.stop_now()
    assert smbus.address == 0x27
    assert [(m.addr, m.data) for m in smbus.messages] == [
        (0x14, [REG_P13, 0, 0]),
        (0x14, [REG_P12, 0, 0]),
    ]


def test_非常停止のバイト列はブロック書き込みと同じ(tmp_path: Path) -> None:
    log = Log()
    hat, smbus, _ = _hat(tmp_path, log)
    log.events.clear()
    hat.set_channel_value(REG_P12 - 0x20, 0x1234)
    hat.set_channel_value_emergency(REG_P12 - 0x20, 0x1234)
    # write_i2c_block_data(0x14, 0x2C, [0x12, 0x34]) と i2c_msg.write(0x14, [0x2C, 0x12, 0x34])
    assert log.events == [("i2c", REG_P12, 0x1234), ("rdwr", REG_P12, 0x1234)]
    assert smbus.messages[0].data == [REG_P12, 0x12, 0x34]


def test_非常停止の書き込みはロックを取らない(tmp_path: Path) -> None:
    log = Log()
    hat, smbus, _ = _hat(tmp_path, log)
    done = threading.Event()
    with hat.bus.lock:

        def other() -> None:
            hat.bus.write_block_emergency(0x14, REG_P12, [0, 0])
            done.set()

        t = threading.Thread(target=other, daemon=True)
        t.start()
        assert done.wait(1)
        t.join(1)


def test_ロックをほかが持っていても50ミリ秒ほどで諦めて0を書く(tmp_path: Path) -> None:
    log = Log()
    hat, _, pins = _hat(tmp_path, log)
    motors = PiCarXMotors(hat, pins, motor_driver="pwm_dir")
    log.events.clear()
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with hat.bus.lock:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(1)
    try:
        start = time.monotonic()
        motors.stop_now()
        elapsed = time.monotonic() - start
    finally:
        release.set()
        holder.join(1)
    assert 0.04 <= elapsed < 0.5
    assert set(log.events) == {("rdwr", REG_P12, 0), ("rdwr", REG_P13, 0)}


def test_止める途中の失敗でも残りのチャンネルに0を書く(tmp_path: Path) -> None:
    log = Log()
    hat, smbus, pins = _hat(tmp_path, log)
    motors = PiCarXMotors(hat, pins, motor_driver="pwm_dir")
    log.events.clear()
    smbus.fail_regs.add(REG_P13)
    with pytest.raises(OSError):
        motors.stop_now()
    assert ("rdwr", REG_P12, 0) in log.events


# ---- 安全停止の実機の部品 ----


def _safe_stop(tmp_path: Path) -> tuple[PiSafeStop, Log, FakeSMBus, FakePins]:
    log = Log()
    hat, smbus, pins = _hat(tmp_path, log)
    log.events.clear()
    return PiSafeStop(lambda: pins, lambda p: hat), log, smbus, pins


def test_安全停止はP12からP15を0にする(tmp_path: Path) -> None:
    stop, log, _, _ = _safe_stop(tmp_path)
    stop.zero_motor_pwm()
    assert log.events == [("i2c", 0x20 + ch, 0) for ch in (12, 13, 14, 15)]


def test_安全停止は1つのチャンネルが失敗しても残りを0にしてから投げる(tmp_path: Path) -> None:
    stop, log, smbus, _ = _safe_stop(tmp_path)
    smbus.fail_regs.add(0x20 + 13)
    with pytest.raises(OSError):
        stop.zero_motor_pwm()
    assert [e[1] for e in log.events] == [0x2C, 0x2E, 0x2F]


def test_安全停止は方向ピンD4とD5をLowにする(tmp_path: Path) -> None:
    stop, log, _, pins = _safe_stop(tmp_path)
    stop.direction_pins_low()
    assert ("gpio", 23, 0) in log.events
    assert ("gpio", 24, 0) in log.events
    assert pins.claimed == {23: 0, 24: 0}


def test_安全停止はマイコンをリセットする(tmp_path: Path) -> None:
    stop, log, _, _ = _safe_stop(tmp_path)
    stop.reset_mcu()
    assert [e for e in log.events if e[0] == "gpio"] == [("gpio", 5, 0), ("gpio", 5, 1)]


def test_GPIOが開けなくてもPWMは0にできる(tmp_path: Path) -> None:
    log = Log()
    hat, _, _ = _hat(tmp_path, log)
    log.events.clear()

    def no_gpio() -> FakePins:
        raise OSError("gpiochip busy")

    stop = PiSafeStop(no_gpio, lambda p: hat)
    stop.zero_motor_pwm()
    assert len(log.events) == 4
    with pytest.raises(OSError):
        stop.direction_pins_low()


def test_安全停止は閉じるときにGPIOを閉じる(tmp_path: Path) -> None:
    stop, log, _, _ = _safe_stop(tmp_path)
    stop.direction_pins_low()
    stop.close()
    assert log.events[-1] == ("close", -1, -1)
