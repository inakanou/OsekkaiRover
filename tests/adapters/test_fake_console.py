"""ハードなしの実行のアダプタ（端末への出力と、Enter キーのボタン）。"""

import ast
import io
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from osekkai.adapters.fake.clock import FakeClock
from osekkai.adapters.fake.console import (
    ConsoleButtons,
    _reset_typed_input_for_tests,
    build_console_devices,
    build_console_set,
    codes_to_text,
    take_typed_line,
    typed_input_active,
)
from osekkai.core.config import VoiceSettings
from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.ports import Devices, Unavailable
from osekkai.core.types import Glyph


@pytest.fixture(autouse=True)
def _reset_typed_input() -> Iterator[None]:
    _reset_typed_input_for_tests()
    yield
    _reset_typed_input_for_tests()


def _lines(out: io.StringIO) -> list[str]:
    return out.getvalue().splitlines()


def test_LCD_のコードを読める文字に戻す() -> None:
    codes = to_lcd_codes("Hi ガッコウ")
    assert codes_to_text(codes) == "Hi ｶﾞｯｺｳ"
    assert codes_to_text([Glyph.STAR, Glyph.HEART, 0x5C, 0x01F]) == "★♥¥?"


def test_LCD_の行が変わると_2_行とも出す(clock: FakeClock) -> None:
    out = io.StringIO()
    fakes = build_console_set(clock, out=out)
    fakes.display.write_line(0, to_lcd_codes("ｺﾝﾆﾁﾊ".ljust(16)))
    fakes.display.write_line(0, to_lcd_codes("ｺﾝﾆﾁﾊ".ljust(16)))  # 同じなら出さない
    fakes.display.write_line(1, to_lcd_codes("ABC"))
    assert _lines(out) == [
        "[LCD] |ｺﾝﾆﾁﾊ           |                |",
        "[LCD] |ｺﾝﾆﾁﾊ           |ABC             |",
    ]
    fakes.display.clear()
    assert _lines(out)[-1] == "[LCD] |                |                |"


def test_バックライトは変わったときだけ出す(clock: FakeClock) -> None:
    out = io.StringIO()
    fakes = build_console_set(clock, out=out)
    fakes.display.set_backlight(True)  # 初めから点灯
    fakes.display.set_backlight(False)
    fakes.display.set_backlight(False)
    fakes.display.set_backlight(True)
    assert _lines(out) == ["[LCD] バックライト: 消灯", "[LCD] バックライト: 点灯"]


def test_LED_と角度と速さは変わるたびに_1_行ずつ出す(clock: FakeClock) -> None:
    out = io.StringIO()
    fakes = build_console_set(clock, out=out)
    fakes.led.set(True)
    fakes.led.set(True)
    fakes.led.set(False)
    fakes.servos.set_angle("pan", 10.0)
    fakes.servos.set_angle("pan", 10.0)
    fakes.servos.set_angle("tilt", -5.5)
    fakes.servos.set_angle("steering", 20.0)
    fakes.motors.set_speeds(40, -40)
    fakes.motors.set_speeds(40, -40)
    fakes.motors.stop_now()
    fakes.motors.stop_now()
    assert _lines(out) == [
        "[LED] 点灯",
        "[LED] 消灯",
        "[首] pan=10.0 tilt=-",
        "[首] pan=10.0 tilt=-5.5",
        "[ステアリング] 20.0",
        "[モーター] 左=40 右=-40",
        "[モーター] 左=0 右=0",
    ]
    # 記録は偽物と同じに残る
    assert fakes.motors.ops() == ["set_speeds", "set_speeds", "stop_now", "stop_now"]


def test_話す文章を声として出し_WAV_も書く(clock: FakeClock, tmp_path: Path) -> None:
    out = io.StringIO()
    fakes = build_console_set(clock, out=out)
    wav = tmp_path / "a.wav"
    fakes.synth.synthesize("こんにちは", VoiceSettings(), wav)
    fakes.player.play(wav, "default")
    assert _lines(out) == ["[声] こんにちは"]
    assert wav.stat().st_size > 0


def test_Enter_を_1_回押すと押下を_1_回知らせる(clock: FakeClock) -> None:
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "r") as inp, os.fdopen(write_fd, "w") as feed:
        buttons = ConsoleButtons(clock, inp=inp)
        got: list[tuple[str, float]] = []
        pressed = threading.Event()

        def on_falling(source: str, at: float) -> None:
            got.append((source, at))
            pressed.set()

        buttons.start(on_falling)
        feed.write("\n")
        feed.flush()
        assert pressed.wait(2.0)
        feed.close()  # EOF で読み手は静かに終わる
        assert buttons.join_reader(2.0)
        buttons.close()
    assert got == [("usr", clock.monotonic())]


def test_入力の終わりでも落ちない(clock: FakeClock) -> None:
    buttons = ConsoleButtons(clock, inp=io.StringIO(""))
    got: list[str] = []
    buttons.start(lambda source, at: got.append(source))
    assert buttons.join_reader(2.0)
    buttons.close()
    assert got == []


def test_押下の前に打った行を取っておき_取り出すと消える(clock: FakeClock) -> None:
    buttons = ConsoleButtons(clock, inp=io.StringIO("だるまさん しよう\n\n"))
    got: list[tuple[str | None, str | None, bool]] = []

    def on_falling(source: str, at: float) -> None:
        # 押下と一緒に、その行の文字が取り出せる。取ったら消える
        got.append((take_typed_line(), take_typed_line(), typed_input_active()))

    assert not typed_input_active()
    buttons.start(on_falling)
    assert buttons.join_reader(2.0)
    assert got == [("だるまさん しよう", None, True), ("", None, True)]
    assert not typed_input_active()  # EOF で消える
    buttons.close()


def test_行の改行だけを除き_ほかの空白は残す(clock: FakeClock) -> None:
    buttons = ConsoleButtons(clock, inp=io.StringIO("  あそぼ \r\n"))
    got: list[str | None] = []
    buttons.start(lambda source, at: got.append(take_typed_line()))
    assert buttons.join_reader(2.0)
    buttons.close()
    assert got == ["  あそぼ "]


def test_端末の偽物が動いているかは_start_で立ち_close_で消える(clock: FakeClock) -> None:
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "r") as inp, os.fdopen(write_fd, "w") as feed:
        buttons = ConsoleButtons(clock, inp=inp)
        assert not typed_input_active()
        buttons.start(lambda source, at: None)
        assert typed_input_active()
        buttons.close()
        assert not typed_input_active()
        feed.write("あとの行\n")  # close の後の行は取っておかない
        feed.close()
        assert buttons.join_reader(2.0)
    assert take_typed_line() is None
    assert not typed_input_active()


def test_テスト用に打った行の口を戻せる(clock: FakeClock) -> None:
    buttons = ConsoleButtons(clock, inp=io.StringIO("のこり\n"))
    buttons.start(lambda source, at: None)
    assert buttons.join_reader(2.0)
    _reset_typed_input_for_tests()
    assert take_typed_line() is None
    assert not typed_input_active()
    buttons.close()


def test_組み立てた一式は端末の版でできている(clock: FakeClock) -> None:
    out = io.StringIO()
    devices = build_console_devices(clock, out=out, inp=io.StringIO(""), fail=["motors"])
    assert isinstance(devices, Devices)
    assert isinstance(devices.buttons, ConsoleButtons)
    assert isinstance(devices.motors, Unavailable)
    with pytest.raises(ValueError):
        build_console_devices(clock, out=out, fail=["nope"])


def test_偽物の記録は同じスレッドから入り直しても止まらない(clock: FakeClock) -> None:
    fakes = build_console_set(clock, out=io.StringIO())
    lock = fakes.motors._lock
    with lock:  # シグナルの受け手が記録の途中に割り込んだ形
        fakes.motors.stop_now()
    assert fakes.motors.ops() == ["stop_now"]


def test_偽物のアダプタは基盤のポートと型だけを読み込む() -> None:
    fake_dir = Path(__file__).resolve().parents[2] / "src" / "osekkai" / "adapters" / "fake"
    allowed = {"osekkai.core.ports", "osekkai.core.types"}
    for path in sorted(fake_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        type_only = {
            id(n)
            for block in ast.walk(tree)
            if isinstance(block, ast.If) and ast.unparse(block.test) == "TYPE_CHECKING"
            for stmt in block.body
            for n in ast.walk(stmt)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and id(node) not in type_only
                and (node.module or "").startswith("osekkai.core")
            ):
                assert node.module in allowed, f"{path.name}: {node.module}"
