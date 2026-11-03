"""画面と LED の操作（DisplayService、LedService）のテスト。"""

import logging

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.devices import FakeDisplay
from osekkai.core.display import DisplayService, LedService
from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.ports import Unavailable
from osekkai.core.types import DeviceUnavailable


def _writes(display: FakeDisplay) -> list[tuple[object, ...]]:
    return [c.args for c in display.calls if c.op == "write_line"]


def _codes(text: str) -> tuple[int, ...]:
    return tuple(to_lcd_codes(text))


# --- 2 行の表示（7.1） ---


def test_17セル以上の行は16セルに切り詰める(fakes: FakeDeviceSet) -> None:
    display = DisplayService(fakes.display)
    display.show("ABCDEFGHIJKLMNOPQRST", "0123456789abcdefg")
    assert fakes.display.rows == [_codes("ABCDEFGHIJKLMNOP"), _codes("0123456789abcdef")]


def test_濁点も1セルとして数えて切り詰める(fakes: FakeDeviceSet) -> None:
    display = DisplayService(fakes.display)
    # 「が」は 2 セル（カ＋濁点）。8 文字で 16 セル、9 文字目は入らない
    display.show("がぎぐげござじずぜ")
    row = fakes.display.rows[0]
    assert len(row) == 16
    assert row == _codes("がぎぐげござじず")


def test_短い行は空白で16セルに埋める(fakes: FakeDeviceSet) -> None:
    display = DisplayService(fakes.display)
    display.show("hi")
    assert fakes.display.rows[0] == _codes("hi") + (0x20,) * 14
    assert fakes.display.rows[1] == (0x20,) * 16


def test_同じ行の再表示ではデバイスに書き込まない(fakes: FakeDeviceSet) -> None:
    display = DisplayService(fakes.display)
    display.show("おはよう", "げんき?")
    assert len(_writes(fakes.display)) == 2
    display.show("おはよう", "げんき?")
    assert len(_writes(fakes.display)) == 2


def test_変わった行だけを送る(fakes: FakeDeviceSet) -> None:
    display = DisplayService(fakes.display)
    display.show("12:00", "abc")
    display.show("12:01", "abc")
    writes = _writes(fakes.display)
    assert len(writes) == 3
    assert writes[-1][0] == 0
    assert fakes.display.rows[0][:5] == _codes("12:01")


def test_切り詰めた後が同じなら書き込まない(fakes: FakeDeviceSet) -> None:
    display = DisplayService(fakes.display)
    display.show("ABCDEFGHIJKLMNOPQ")
    display.show("ABCDEFGHIJKLMNOPZ")
    assert len(_writes(fakes.display)) == 2


def test_消去の後は両方の行を書き直す(fakes: FakeDeviceSet) -> None:
    display = DisplayService(fakes.display)
    display.show("a", "b")
    display.clear()
    assert fakes.display.calls[-1].op == "clear"
    display.show("a", "b")
    assert len(_writes(fakes.display)) == 4
    assert fakes.display.rows == [_codes("a") + (0x20,) * 15, _codes("b") + (0x20,) * 15]


# --- バックライト（7.5）と LED（7.6） ---


def test_バックライトは指示どおりになる(fakes: FakeDeviceSet) -> None:
    display = DisplayService(fakes.display)
    display.set_backlight(False)
    assert fakes.display.backlight is False
    display.set_backlight(True)
    assert fakes.display.backlight is True


def test_LEDは指示どおりになる(fakes: FakeDeviceSet) -> None:
    led = LedService(fakes.led)
    led.set(True)
    assert fakes.led.on is True
    led.set(False)
    assert fakes.led.on is False


# --- 使えないデバイスと失敗 ---


def test_使えない画面はDeviceUnavailableを投げる() -> None:
    display = DisplayService(Unavailable("display", "i2c"))
    with pytest.raises(DeviceUnavailable):
        display.show("a")
    with pytest.raises(DeviceUnavailable):
        display.set_backlight(True)
    with pytest.raises(DeviceUnavailable):
        display.clear()


def test_使えないLEDはDeviceUnavailableを投げる() -> None:
    led = LedService(Unavailable("led", "gpio"))
    with pytest.raises(DeviceUnavailable):
        led.set(True)


def test_画面の書き込みの失敗はDeviceUnavailableになり型だけを記録する(
    fakes: FakeDeviceSet, caplog: pytest.LogCaptureFixture
) -> None:
    display = DisplayService(fakes.display)
    fakes.display.fail_on.add("write_line")
    with caplog.at_level(logging.WARNING, logger="osekkai"), pytest.raises(DeviceUnavailable):
        display.show("ひみつ")
    assert "FakeDeviceError" in caplog.text
    assert "ひみつ" not in caplog.text
    assert "ﾋﾐﾂ" not in caplog.text


def test_書き込みに失敗した行は次の表示で送り直す(fakes: FakeDeviceSet) -> None:
    display = DisplayService(fakes.display)
    fakes.display.fail_on.add("write_line")
    with pytest.raises(DeviceUnavailable):
        display.show("a", "b")
    fakes.display.fail_on.clear()
    display.show("a", "b")
    assert fakes.display.rows == [_codes("a") + (0x20,) * 15, _codes("b") + (0x20,) * 15]


def test_バックライトと消去とLEDの失敗はDeviceUnavailableになる(fakes: FakeDeviceSet) -> None:
    display = DisplayService(fakes.display)
    led = LedService(fakes.led)
    fakes.display.fail_on.update({"set_backlight", "clear"})
    fakes.led.fail_on.add("set")
    with pytest.raises(DeviceUnavailable):
        display.set_backlight(False)
    with pytest.raises(DeviceUnavailable):
        display.clear()
    with pytest.raises(DeviceUnavailable):
        led.set(True)
