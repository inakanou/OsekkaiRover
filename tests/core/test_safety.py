"""中立の姿勢への復帰の手順（SafetyStop）のテスト。"""

import logging

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.config import MotionSettings
from osekkai.core.display import DisplayService, LedService
from osekkai.core.motion import MotionService
from osekkai.core.ports import Devices
from osekkai.core.safety import SafetyStop


class TickingClock:
    """呼ばれるたびに 1 ずつ進む時計。偽物をまたいだ呼び出しの順を記録に残す。"""

    def __init__(self) -> None:
        self._now = 0.0

    def monotonic(self) -> float:
        self._now += 1.0
        return self._now


@pytest.fixture
def ordered() -> FakeDeviceSet:
    return FakeDeviceSet.create(TickingClock())


def _safety(devices: Devices) -> SafetyStop:
    motion = MotionService(devices.servos, devices.motors, MotionSettings(), FakeClock())
    return SafetyStop(motion, DisplayService(devices.display), LedService(devices.led))


def _timeline(fakes: FakeDeviceSet) -> list[tuple[str, str, tuple[object, ...]]]:
    """表示・LED・サーボ・モーターの記録を、呼ばれた順に 1 本に並べる。"""
    named = {
        "display": fakes.display,
        "led": fakes.led,
        "servos": fakes.servos,
        "motors": fakes.motors,
    }
    rows = [(c.at, name, c.op, c.args) for name, fake in named.items() for c in fake.calls]
    return [(name, op, args) for _, name, op, args in sorted(rows, key=lambda r: r[0])]


NEUTRAL = [
    ("motors", "stop_now", ()),
    ("motors", "set_speeds", (0, 0)),
    ("servos", "set_angle", ("pan", 0.0)),
    ("servos", "set_angle", ("tilt", 0.0)),
    ("servos", "set_angle", ("steering", 0.0)),
    ("led", "set", (False,)),
]


# --- 中立（1.1） ---


def test_中立はモーター停止_首とステアリングを正面_LEDを消すの順(
    ordered: FakeDeviceSet,
) -> None:
    failed = _safety(ordered.to_devices()).neutral()
    assert _timeline(ordered) == NEUTRAL
    assert failed == []


def test_中立では画面に触らない(ordered: FakeDeviceSet) -> None:
    _safety(ordered.to_devices()).neutral()
    assert ordered.display.calls == []


# --- 停止の片付け（2.2、2.4） ---


def test_停止の片付けは中立の後にLCDを消してバックライトを消す(
    ordered: FakeDeviceSet,
) -> None:
    failed = _safety(ordered.to_devices()).halt()
    assert _timeline(ordered) == [
        *NEUTRAL,
        ("display", "clear", ()),
        ("display", "set_backlight", (False,)),
    ]
    assert failed == []


# --- 失敗しても次の段階に進む ---


def test_首の段階が失敗しても残りの段階が行われる(
    ordered: FakeDeviceSet, caplog: pytest.LogCaptureFixture
) -> None:
    ordered.servos.fail_on.add("set_angle")
    with caplog.at_level(logging.WARNING, logger="osekkai.core.safety"):
        failed = _safety(ordered.to_devices()).halt()
    assert failed == ["home"]
    timeline = _timeline(ordered)
    assert timeline[-3:] == [
        ("led", "set", (False,)),
        ("display", "clear", ()),
        ("display", "set_backlight", (False,)),
    ]
    assert ordered.motors.speeds == (0, 0)
    messages = [r.getMessage() for r in caplog.records if r.name == "osekkai.core.safety"]
    assert any("home" in m and "DeviceUnavailable" in m for m in messages)
    # 例外のメッセージは出さない
    assert not any("fake:" in m for m in messages)


def test_モーターの段階が失敗しても首とLEDと画面は戻る(ordered: FakeDeviceSet) -> None:
    ordered.motors.fail_on.update({"stop_now", "set_speeds"})
    failed = _safety(ordered.to_devices()).halt()
    assert failed == ["stop"]
    assert ordered.servos.angles == {"pan": 0.0, "tilt": 0.0, "steering": 0.0}
    assert ordered.led.calls[-1].args == (False,)
    assert ordered.display.ops() == ["clear", "set_backlight"]


def test_LEDの段階が失敗しても画面は消える(ordered: FakeDeviceSet) -> None:
    ordered.led.fail_on.add("set")
    failed = _safety(ordered.to_devices()).halt()
    assert failed == ["led"]
    assert ordered.display.ops() == ["clear", "set_backlight"]


def test_LCDの消去が失敗してもバックライトは消える(ordered: FakeDeviceSet) -> None:
    ordered.display.fail_on.add("clear")
    failed = _safety(ordered.to_devices()).halt()
    assert failed == ["display_clear"]
    assert ordered.display.ops() == ["clear", "set_backlight"]
    assert ordered.display.calls[-1].args == (False,)


def test_使えないデバイスがあっても残りの段階が行われる(ordered: FakeDeviceSet) -> None:
    failed = _safety(ordered.to_devices(fail=["servos", "led"])).halt()
    assert failed == ["home", "led"]
    assert _timeline(ordered) == [
        ("motors", "stop_now", ()),
        ("motors", "set_speeds", (0, 0)),
        ("display", "clear", ()),
        ("display", "set_backlight", (False,)),
    ]


def test_すべてのデバイスが使えなくても例外を出さない(ordered: FakeDeviceSet) -> None:
    devices = ordered.to_devices(fail=["display", "led", "servos", "motors"])
    failed = _safety(devices).halt()
    assert failed == ["stop", "home", "led", "display_clear", "display_backlight"]
