"""実機の部品のうち、ハードのライブラリなしで確かめられる部分のテスト。

smbus2 と lgpio は、偽のオブジェクトを渡して読み込まない。
"""

from __future__ import annotations

import ast
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from osekkai.adapters.pi.gpio import (
    BUTTON_DEBOUNCE_MICROS,
    PIN_MAP,
    Gpio,
    HatButtons,
    HatLed,
    enable_speaker,
    resolve_pin,
    select_gpiochip,
    speaker_pin,
)
from osekkai.adapters.pi.hat import (
    V5_UUID,
    HatMcu,
    channel_register,
    encode_word,
    period_register,
    prescaler_register,
    read_hat_version,
    timer_index,
)
from osekkai.adapters.pi.i2c_bus import I2cBus

PI_DIR = Path(__file__).resolve().parents[2] / "src" / "osekkai" / "adapters" / "pi"


# ---- 偽の smbus2 と lgpio ----


class FakeSMBus:
    def __init__(self, *, answer: set[int] | None = None, fail_write: bool = False) -> None:
        self.answer = answer if answer is not None else {0x14}
        self.fail_write = fail_write
        self.writes: list[tuple[int, int, list[int]]] = []
        self.closed = False

    def write_quick(self, addr: int) -> None:
        if addr not in self.answer:
            raise OSError(121, "Remote I/O error")

    def write_byte(self, addr: int, value: int) -> None:
        if self.fail_write:
            raise OSError(5, "Input/output error")
        self.writes.append((addr, value, []))

    def write_i2c_block_data(self, addr: int, reg: int, data: list[int]) -> None:
        if self.fail_write:
            raise OSError(5, "Input/output error")
        self.writes.append((addr, reg, list(data)))

    def close(self) -> None:
        self.closed = True


class FakeCallback:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeLgpio:
    FALLING_EDGE = 2
    SET_PULL_UP = 32

    def __init__(self, labels: dict[int, str]) -> None:
        self.labels = labels
        self.opened: list[int] = []
        self.closed: list[int] = []
        self.calls: list[tuple[Any, ...]] = []
        self.callbacks: dict[int, Callable[[int, int, int, int], None]] = {}
        self.handles: list[FakeCallback] = []

    def gpiochip_open(self, n: int) -> int:
        if n not in self.labels:
            raise RuntimeError("no chip")
        self.opened.append(n)
        return 100 + n

    def gpiochip_close(self, handle: int) -> None:
        self.closed.append(handle - 100)

    def gpio_get_chip_info(self, handle: int) -> tuple[int, int, str, str]:
        return (0, 54, f"gpiochip{handle - 100}", self.labels[handle - 100])

    def gpio_claim_output(self, handle: int, pin: int, level: int = 0, flags: int = 0) -> int:
        self.calls.append(("out", pin, level))
        return 0

    def gpio_claim_alert(self, handle: int, pin: int, edge: int, flags: int = 0) -> int:
        self.calls.append(("alert", pin, edge, flags))
        return 0

    def gpio_set_debounce_micros(self, handle: int, pin: int, micros: int) -> int:
        self.calls.append(("debounce", handle, pin, micros))
        return 0

    def gpio_write(self, handle: int, pin: int, level: int) -> int:
        self.calls.append(("write", pin, level))
        return 0

    def gpio_free(self, handle: int, pin: int) -> int:
        self.calls.append(("free", pin))
        return 0

    def callback(
        self, handle: int, pin: int, edge: int, func: Callable[[int, int, int, int], None]
    ) -> FakeCallback:
        self.calls.append(("callback", pin, edge))
        self.callbacks[pin] = func
        cb = FakeCallback()
        self.handles.append(cb)
        return cb


def _bus(fake: FakeSMBus) -> tuple[I2cBus, list[int]]:
    opened: list[int] = []

    def opener(n: int) -> FakeSMBus:
        opened.append(n)
        return fake

    return I2cBus(opener=opener), opened


# ---- ハードのライブラリの読み込みの場所 ----


def test_ハードのライブラリはモジュールの最上位で読み込まない() -> None:
    for path in sorted(PI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert name.split(".")[0] not in {"smbus2", "lgpio", "numpy"}, path.name


# ---- I2C のバス ----


def test_バスは最初の書き込みで1回だけ開く() -> None:
    fake = FakeSMBus()
    bus, opened = _bus(fake)
    assert opened == []
    bus.write_block(0x14, 0x20, [0x01, 0x02])
    bus.write_byte(0x27, 0x08)
    assert opened == [1]
    assert fake.writes == [(0x14, 0x20, [1, 2]), (0x27, 0x08, [])]


def test_書き込みの失敗はOSErrorのまま投げる() -> None:
    bus, _ = _bus(FakeSMBus(fail_write=True))
    with pytest.raises(OSError):
        bus.write_block(0x14, 0x20, [0, 0])
    with pytest.raises(OSError):
        bus.write_byte(0x27, 0)


def test_応えるアドレスだけが見つかる() -> None:
    bus, _ = _bus(FakeSMBus(answer={0x15}))
    assert not bus.probe(0x14)
    assert bus.probe(0x15)


def test_バスのロックは再入できる() -> None:
    bus, _ = _bus(FakeSMBus())
    with bus.lock, bus.lock:
        bus.write_byte(0x27, 1)


# ---- HAT のマイコン ----


def test_PWMのレジスタの番地() -> None:
    assert channel_register(0) == 0x20
    assert channel_register(13) == 0x2D
    channels = (0, 3, 4, 12, 15, 16, 17, 18, 19)
    assert [timer_index(c) for c in channels] == [0, 0, 1, 3, 3, 4, 4, 5, 6]
    assert prescaler_register(3) == 0x43
    assert period_register(3) == 0x47
    assert prescaler_register(4) == 0x50
    assert period_register(6) == 0x56
    with pytest.raises(ValueError):
        channel_register(20)


def test_値は16ビットの上位と下位に分ける() -> None:
    assert encode_word(4095) == [0x0F, 0xFF]
    assert encode_word(0) == [0, 0]
    with pytest.raises(ValueError):
        encode_word(0x10000)
    with pytest.raises(ValueError):
        encode_word(-1)


def _write_uuid(root: Path, folder: str, text: str) -> None:
    (root / folder).mkdir()
    (root / folder / "uuid").write_bytes(text.encode() + b"\x00")


def test_HATの版をuuidで見分ける(tmp_path: Path) -> None:
    _write_uuid(tmp_path, "hat", V5_UUID)
    assert read_hat_version(tmp_path) == "v5"


def test_EEPROMのないHATはv4(tmp_path: Path) -> None:
    # v4 には EEPROM がないので、デバイスツリーに hat の項目がない（robot-hat 2.5.8 と同じ）
    (tmp_path / "chosen").mkdir()
    assert read_hat_version(tmp_path) == "v4"


def test_uuidのないhatの項目はv4(tmp_path: Path) -> None:
    (tmp_path / "hat").mkdir()
    assert read_hat_version(tmp_path) == "v4"


def test_v5でないuuidのHATはv4(tmp_path: Path) -> None:
    _write_uuid(tmp_path, "hat", "00000000-0000-0000-0000-000000000000")
    assert read_hat_version(tmp_path) == "v4"


def test_読めないデバイスツリーはunknown(tmp_path: Path) -> None:
    assert read_hat_version(tmp_path / "none") == "unknown"


class RecordingGpio:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def claim_output(self, pin: int, level: int) -> None:
        self.calls.append(("claim", pin, level))

    def write(self, pin: int, level: int) -> None:
        self.calls.append(("write", pin, level))


def test_マイコンは順にアドレスを試してPWMを書く(tmp_path: Path) -> None:
    fake = FakeSMBus(answer={0x16})
    bus, _ = _bus(fake)
    hat = HatMcu(bus, RecordingGpio(), sleep=lambda s: None, device_tree=tmp_path)
    assert hat.address == 0x16
    hat.set_prescaler(13, 10)
    hat.set_period(13, 4095)
    hat.set_channel_value(13, 2048)
    # プリスケーラは「値 − 1」を書く（robot-hat 2.5.x）
    assert fake.writes == [(0x16, 0x43, [0, 9]), (0x16, 0x47, [0x0F, 0xFF]), (0x16, 0x2D, [8, 0])]


def test_どのアドレスも応えなければOSError(tmp_path: Path) -> None:
    bus, _ = _bus(FakeSMBus(answer=set()))
    with pytest.raises(OSError):
        HatMcu(bus, RecordingGpio(), sleep=lambda s: None, device_tree=tmp_path)


def test_リセットはBCM5を10ミリ秒Lowにしてから0_2秒待つ(tmp_path: Path) -> None:
    events: list[tuple[str, float]] = []
    gpio = RecordingGpio()
    bus, _ = _bus(FakeSMBus())
    hat = HatMcu(bus, gpio, sleep=lambda s: events.append(("sleep", s)), device_tree=tmp_path)
    gpio.calls.clear()
    events.clear()
    hat.reset()
    assert gpio.calls == [("claim", 5, 1), ("write", 5, 0), ("write", 5, 1)]
    assert events == [("sleep", 0.01), ("sleep", 0.2)]


def test_マイコンは版を覚える(tmp_path: Path) -> None:
    _write_uuid(tmp_path, "hat", V5_UUID)
    bus, _ = _bus(FakeSMBus())
    hat = HatMcu(bus, RecordingGpio(), sleep=lambda s: None, device_tree=tmp_path)
    assert hat.hat_version() == "v5"


# ---- GPIO ----


def test_ピンの割り当て表() -> None:
    assert PIN_MAP["D0"] == 17
    assert PIN_MAP["D4"] == 23
    assert PIN_MAP["D5"] == 24
    assert PIN_MAP["D16"] == 21
    assert len([k for k in PIN_MAP if k.startswith("D")]) == 17
    assert resolve_pin("d0") == 17
    with pytest.raises(ValueError):
        resolve_pin("D17")


def test_gpiochipはラベルで選ぶ() -> None:
    labels = {0: "pinctrl-rp1", 10: "gpio-brcmstb@107d508500"}
    assert select_gpiochip(labels, None) == 0
    assert select_gpiochip({4: "pinctrl-rp1", 0: "gpio-brcmstb"}, None) == 4
    assert select_gpiochip({0: "pinctrl-bcm2711", 1: "raspberrypi-exp-gpio"}, None) == 0
    assert select_gpiochip({}, None) == 0


def test_環境変数のgpiochipを優先する() -> None:
    assert select_gpiochip({0: "pinctrl-rp1"}, "4") == 4
    with pytest.raises(ValueError):
        select_gpiochip({0: "pinctrl-rp1"}, "abc")


def test_スピーカーのピンは版と設定で決まる() -> None:
    assert speaker_pin("v4", "auto") == 20
    assert speaker_pin("v5", "auto") == 12
    assert speaker_pin("unknown", "auto") == 20
    assert speaker_pin("v5", 6) == 6


def _gpio(labels: dict[int, str], environ: dict[str, str] | None = None) -> tuple[Gpio, FakeLgpio]:
    lg = FakeLgpio(labels)
    return Gpio(lg=lg, environ=environ or {}, chips=sorted(labels)), lg


def test_gpioはラベルの合うチップを開いたままにする() -> None:
    gpio, lg = _gpio({0: "gpio-brcmstb", 4: "pinctrl-rp1"})
    assert gpio.chip == 4
    assert lg.closed == [0]
    gpio.close()
    assert lg.closed == [0, 4]


def test_gpioは環境変数の番号のチップだけを開く() -> None:
    gpio, lg = _gpio({0: "gpio-brcmstb", 4: "pinctrl-rp1"}, {"ROBOT_HAT_GPIOCHIP": "0"})
    assert gpio.chip == 0
    assert lg.opened == [0]


def test_ボタンは立ち下がりを時刻付きで知らせる() -> None:
    gpio, lg = _gpio({0: "pinctrl-rp1"})
    buttons = HatButtons(gpio, extra_pin="D0", monotonic=lambda: 12.5)
    got: list[tuple[str, float]] = []
    buttons.start(lambda source, at: got.append((source, at)))
    assert ("alert", 25, lg.FALLING_EDGE, lg.SET_PULL_UP) in lg.calls
    assert ("alert", 17, lg.FALLING_EDGE, lg.SET_PULL_UP) in lg.calls
    lg.callbacks[25](0, 25, 0, 0)
    lg.callbacks[17](0, 17, 0, 0)
    assert got == [("usr", 12.5), ("extra", 12.5)]
    buttons.close()
    assert all(cb.cancelled for cb in lg.handles)
    assert ("free", 25) in lg.calls
    buttons.close()


def test_知らせの受け手の例外でボタンは止まらない() -> None:
    gpio, lg = _gpio({0: "pinctrl-rp1"})
    buttons = HatButtons(gpio, extra_pin=None, monotonic=lambda: 1.0)

    def boom(source: str, at: float) -> None:
        raise RuntimeError("x")

    buttons.start(boom)
    lg.callbacks[25](0, 25, 0, 0)


def test_追加のボタンの名前が誤りならValueError() -> None:
    gpio, _ = _gpio({0: "pinctrl-rp1"})
    with pytest.raises(ValueError):
        HatButtons(gpio, extra_pin="X9")


def test_LEDはBCM26を操作する() -> None:
    gpio, lg = _gpio({0: "pinctrl-rp1"})
    led = HatLed(gpio)
    led.set(True)
    led.set(False)
    assert lg.calls == [("out", 26, 0), ("write", 26, 1), ("write", 26, 0)]


def test_ボタンは見張りの後と知らせの前に30ミリ秒のチャタリング除けを入れる() -> None:
    gpio, lg = _gpio({0: "pinctrl-rp1"})
    gpio.watch_falling(17, lambda chip, pin, level, tick: None)
    assert BUTTON_DEBOUNCE_MICROS == 30_000
    assert lg.calls == [
        ("alert", 17, lg.FALLING_EDGE, lg.SET_PULL_UP),
        ("debounce", 100, 17, 30_000),
        ("callback", 17, lg.FALLING_EDGE),
    ]


def test_チャタリング除けの長さは変えられる() -> None:
    gpio, lg = _gpio({0: "pinctrl-rp1"})
    gpio.watch_falling(25, lambda chip, pin, level, tick: None, debounce_micros=5_000)
    assert ("debounce", 100, 25, 5_000) in lg.calls


class FakeRun:
    def __init__(self, returncode: int = 0, error: BaseException | None = None) -> None:
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        self.calls.append((list(args), kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(args, self.returncode)


def test_スピーカーの有効化はpinctrlでピンをHighにする() -> None:
    run = FakeRun()
    assert enable_speaker("v5", "auto", run=run) == 12
    assert enable_speaker("v4", "auto", run=run) == 20
    assert enable_speaker("unknown", 6, run=run) == 6
    assert [args for args, _ in run.calls] == [
        ["pinctrl", "set", "12", "op", "dh"],
        ["pinctrl", "set", "20", "op", "dh"],
        ["pinctrl", "set", "6", "op", "dh"],
    ]
    _, kwargs = run.calls[0]
    assert kwargs["check"] is False
    assert 0 < kwargs["timeout"] <= 5


def test_スピーカーの有効化はlgpioを使わない() -> None:
    gpio, lg = _gpio({0: "pinctrl-rp1"})
    enable_speaker("v4", "auto", run=FakeRun())
    assert lg.calls == []
    gpio.close()


@pytest.mark.parametrize(
    ("run", "message"),
    [
        (FakeRun(returncode=1), "pinctrl failed with return code 1"),
        (FakeRun(error=FileNotFoundError(2, "No such file", "/secret/pinctrl")), "pinctrl"),
        (FakeRun(error=subprocess.TimeoutExpired(["pinctrl"], 2.0, output=b"x")), "pinctrl"),
        (FakeRun(error=PermissionError(13, "denied", "/secret")), "pinctrl"),
    ],
)
def test_スピーカーの有効化の失敗はOSError(run: FakeRun, message: str) -> None:
    with pytest.raises(OSError) as info:
        enable_speaker("v4", "auto", run=run)
    text = str(info.value)
    assert text.startswith(message)
    assert "/secret" not in text
    assert info.value.__cause__ is None
