"""超音波と床センサーの実機のアダプタ（要件 5.1、5.4）。

偽の lgpio（端の知らせを callback で起こす）、偽の smbus2（偽の bus）、
tmp_path の偽の sysfs で確かめる。実機のライブラリは読まない。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from remote_fakes import FakeClock

from osekkai.features.remote.adapters import sensors
from osekkai.features.remote.adapters.sensors import HatSensors, find_gpiochip, sensors_available
from osekkai.features.remote.ports import SensorPort

RISE_NS = 1_000_000_000
SOUND_CM_PER_NS = 343.3 * 100 / 1e9


HANDLE_OFFSET = 3  # 偽の handle は chip の番号 + 3（gpiochip4 → 7）


class FakeLgpioError(Exception):
    pass


class FakeCallback:
    def __init__(self, lg: FakeLgpio) -> None:
        self._lg = lg
        self.cancelled = 0

    def cancel(self) -> None:
        self.cancelled += 1
        self._lg.calls.append(("cancel",))


class FakeLgpio:
    """lgpio の偽物。trig のパルスで、台本の端を callback に知らせる。"""

    BOTH_EDGES = 3
    SET_PULL_DOWN = 64
    error = FakeLgpioError

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.func: Callable[[int, int, int, int], None] | None = None
        self.edges: list[tuple[int, int]] = []  # 1 回のパルスで知らせる (level, 時刻 ns)
        self.fail_open = False
        self.fail_trigger: Exception | None = None
        self.callback_obj: FakeCallback | None = None
        self.chip_labels: dict[int, str] = {}  # gpio_get_chip_info の label
        self.echo_level: int | Exception = 1  # 待ちの後の gpio_read の echo

    def gpiochip_open(self, chip: int) -> int:
        self.calls.append(("gpiochip_open", chip))
        if self.fail_open:
            raise FakeLgpioError("busy")
        return chip + HANDLE_OFFSET

    def gpio_get_chip_info(self, handle: int) -> tuple[int, int, str, str]:
        self.calls.append(("gpio_get_chip_info", handle))
        label = self.chip_labels.get(handle - HANDLE_OFFSET, "")
        return (0, 54, f"gpiochip{handle - HANDLE_OFFSET}", label)

    def gpio_read(self, handle: int, gpio: int) -> int:
        self.calls.append(("gpio_read", handle, gpio))
        if isinstance(self.echo_level, Exception):
            raise self.echo_level
        return self.echo_level

    def gpiochip_close(self, handle: int) -> None:
        self.calls.append(("gpiochip_close", handle))

    def gpio_claim_output(self, handle: int, gpio: int, level: int = 0) -> None:
        self.calls.append(("gpio_claim_output", handle, gpio, level))

    def gpio_claim_alert(self, handle: int, gpio: int, eflags: int, lflags: int = 0) -> None:
        self.calls.append(("gpio_claim_alert", handle, gpio, eflags, lflags))

    def callback(
        self, handle: int, gpio: int, edge: int, func: Callable[[int, int, int, int], None]
    ) -> FakeCallback:
        self.calls.append(("callback", handle, gpio, edge))
        self.func = func
        self.callback_obj = FakeCallback(self)
        return self.callback_obj

    def gpio_trigger(self, handle: int, gpio: int, pulse_us: int, level: int) -> None:
        self.calls.append(("gpio_trigger", handle, gpio, pulse_us, level))
        if self.fail_trigger is not None:
            raise self.fail_trigger
        assert self.func is not None
        for level_, ts in self.edges:
            self.func(0, sensors.HatSensors.ECHO_BCM, level_, ts)

    def gpio_free(self, handle: int, gpio: int) -> None:
        self.calls.append(("gpio_free", handle, gpio))

    def pins(self) -> set[int]:
        found: set[int] = set()
        for call in self.calls:
            if call[0] in {
                "gpio_claim_output",
                "gpio_claim_alert",
                "gpio_trigger",
                "gpio_free",
                "gpio_read",
            }:
                found.add(call[2])
        return found


class FakeMsg:
    def __init__(self, addr: int, data: Sequence[int] | None, length: int) -> None:
        self.addr = addr
        self.data = list(data) if data is not None else None
        self.length = length
        self.result: list[int] = []

    def __iter__(self) -> Iterator[int]:
        return iter(self.result)


class FakeI2cMsg:
    @staticmethod
    def write(addr: int, data: Sequence[int]) -> FakeMsg:
        return FakeMsg(addr, data, len(data))

    @staticmethod
    def read(addr: int, length: int) -> FakeMsg:
        return FakeMsg(addr, None, length)


class FakeBus:
    def __init__(self, owner: FakeSmbus, bus: int) -> None:
        self._owner = owner
        self.bus = bus
        self.closed = 0

    def i2c_rdwr(self, *msgs: FakeMsg) -> None:
        self._owner.transfers.append(msgs)
        write, read = msgs
        assert write.data is not None
        key = (write.addr, write.data[0])
        if write.addr in self._owner.dead or key in self._owner.broken:
            raise OSError("nack")
        msb, lsb = self._owner.values.get(key, (0, 0))
        read.result = [msb, lsb]

    def close(self) -> None:
        self.closed += 1


class FakeSmbus:
    """smbus2 の偽物。(アドレス, レジスタ) ごとの 2 バイトを返す。"""

    i2c_msg = FakeI2cMsg

    def __init__(self) -> None:
        self.values: dict[tuple[int, int], tuple[int, int]] = {}
        self.dead: set[int] = set()
        self.broken: set[tuple[int, int]] = set()
        self.transfers: list[tuple[FakeMsg, ...]] = []
        self.buses: list[FakeBus] = []
        self.fail_open = False
        self.SMBus = self._open_bus  # smbus2 の名前に合わせる

    def _open_bus(self, bus: int) -> FakeBus:
        if self.fail_open:
            raise OSError("no bus")
        made = FakeBus(self, bus)
        self.buses.append(made)
        return made


def _sysfs(root: Path, chips: dict[int, dict[str, bytes]]) -> Path:
    for number, files in chips.items():
        chip = root / "bus" / "gpio" / "devices" / f"gpiochip{number}"
        chip.mkdir(parents=True)
        for name, content in files.items():
            path = chip / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    return root


RP1_COMPATIBLE = b"raspberrypi,rp1-gpio\x00"


def _make(
    tmp_path: Path,
    *,
    lg: FakeLgpio | None = None,
    bus: FakeSmbus | None = None,
    clock: FakeClock | None = None,
) -> tuple[HatSensors, FakeLgpio, FakeSmbus, FakeClock]:
    lg = lg or FakeLgpio()
    bus = bus or FakeSmbus()
    clock = clock or FakeClock()
    root = tmp_path / "sys"
    if not root.exists():
        _sysfs(root, {4: {"of_node/compatible": RP1_COMPATIBLE}})
    dev = tmp_path / "dev"
    dev.mkdir(exist_ok=True)
    hat = HatSensors(clock=clock, lgpio_module=lg, smbus_module=bus, sys_root=root, dev_root=dev)
    return hat, lg, bus, clock


def _dev(root: Path, numbers: Sequence[int]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for number in numbers:
        (root / f"gpiochip{number}").touch()
    return root


def _find(sys_root: Path, dev_root: Path | None = None, lg: FakeLgpio | None = None) -> str | None:
    dev = dev_root if dev_root is not None else _dev(sys_root.parent / "nodev", [])
    return find_gpiochip(sys_root=sys_root, dev_root=dev, lgpio_module=lg or FakeLgpio())


def _echo(lg: FakeLgpio, width_ns: int) -> None:
    lg.edges = [(1, RISE_NS), (0, RISE_NS + width_ns)]


# --- ライブラリがあるか ---


def test_ライブラリがあるかを_find_spec_で見る(monkeypatch: pytest.MonkeyPatch) -> None:
    found: list[str] = []

    def fake_find_spec(name: str) -> object | None:
        found.append(name)
        return object() if name in {"smbus2", "lgpio"} else None

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.delitem(sys.modules, "smbus2", raising=False)
    monkeypatch.delitem(sys.modules, "lgpio", raising=False)
    assert sensors_available() is True
    assert set(found) == {"smbus2", "lgpio"}


def test_どちらかがなければ使えない(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "smbus2" else None,
    )
    monkeypatch.delitem(sys.modules, "smbus2", raising=False)
    monkeypatch.delitem(sys.modules, "lgpio", raising=False)
    assert sensors_available() is False


# --- gpiochip の見つけ方 ---


def test_compatible_が_rp1_の_gpiochip_を選ぶ(tmp_path: Path) -> None:
    root = _sysfs(
        tmp_path,
        {
            0: {"of_node/compatible": b"brcm,bcm2835-gpio\x00"},
            4: {"of_node/compatible": b"raspberrypi,rp1-gpio\x00raspberrypi,rp1\x00"},
        },
    )
    assert _find(root) == "/dev/gpiochip4"


def test_label_が_pinctrl_rp1_の_gpiochip_を選ぶ(tmp_path: Path) -> None:
    root = _sysfs(
        tmp_path,
        {
            10: {"label": b"gpio-brcmstb@107d508500\n"},
            13: {"label": b"pinctrl-rp1\n"},
        },
    )
    assert _find(root) == "/dev/gpiochip13"


def test_合う_gpiochip_がなければ_None(tmp_path: Path) -> None:
    root = _sysfs(tmp_path, {0: {"label": b"pinctrl-bcm2711\n"}, 1: {}})
    assert _find(root) is None
    assert _find(tmp_path / "missing") is None


def test_番号のない名前は候補にしない(tmp_path: Path) -> None:
    root = _sysfs(tmp_path, {})
    odd = root / "bus" / "gpio" / "devices" / "gpiochipX"
    odd.mkdir(parents=True)
    (odd / "label").write_bytes(b"pinctrl-rp1\n")
    assert _find(root) is None


def test_sysfs_になければ_lgpio_の_label_で探す(tmp_path: Path) -> None:
    root = _sysfs(tmp_path / "sys", {0: {}, 4: {}})
    dev = _dev(tmp_path / "dev", [0, 4, 10])
    lg = FakeLgpio()
    lg.chip_labels = {0: "gpio-brcmstb@107d508500", 4: "pinctrl-rp1", 10: "pinctrl-rp1"}
    assert _find(root, dev, lg) == "/dev/gpiochip4"
    # 見た chip は閉じる
    assert ("gpiochip_close", 0 + HANDLE_OFFSET) in lg.calls
    assert ("gpiochip_close", 4 + HANDLE_OFFSET) in lg.calls


def test_lgpio_でもほかの_pinctrl_と_chip_0_には倒さない(tmp_path: Path) -> None:
    root = _sysfs(tmp_path / "sys", {})
    dev = _dev(tmp_path / "dev", [0, 1])
    lg = FakeLgpio()
    lg.chip_labels = {0: "pinctrl-bcm2711", 1: "pinctrl-bcm2835"}
    assert _find(root, dev, lg) is None


def test_lgpio_で開けない_chip_は飛ばす(tmp_path: Path) -> None:
    class PartlyBroken(FakeLgpio):
        def gpiochip_open(self, chip: int) -> int:
            if chip == 0:
                raise FakeLgpioError("busy")
            return super().gpiochip_open(chip)

        def gpio_get_chip_info(self, handle: int) -> tuple[int, int, str, str]:
            if handle == 2 + HANDLE_OFFSET:
                raise FakeLgpioError("info")
            return super().gpio_get_chip_info(handle)

    root = _sysfs(tmp_path / "sys", {})
    dev = _dev(tmp_path / "dev", [0, 2, 5])
    lg = PartlyBroken()
    lg.chip_labels = {5: "pinctrl-rp1"}
    assert _find(root, dev, lg) == "/dev/gpiochip5"
    assert ("gpiochip_close", 2 + HANDLE_OFFSET) in lg.calls


def test_lgpio_がなければ_sysfs_だけで探す(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_lgpio() -> object:
        raise ImportError("lgpio")

    monkeypatch.setattr(sensors, "_import_lgpio", no_lgpio)
    root = _sysfs(tmp_path / "sys", {})
    dev = _dev(tmp_path / "dev", [4])
    assert find_gpiochip(sys_root=root, dev_root=dev) is None


def test_sysfs_で見つからなくても_lgpio_の_label_で読める(tmp_path: Path) -> None:
    _sysfs(tmp_path / "sys", {4: {}})
    _dev(tmp_path / "dev", [4])
    lg = FakeLgpio()
    lg.chip_labels = {4: "pinctrl-rp1"}
    hat, _, _, _ = _make(tmp_path, lg=lg)
    _echo(lg, 1_000_000)
    assert hat.read().distance_cm == pytest.approx(1_000_000 * SOUND_CM_PER_NS / 2)


# --- 床センサー（ADC） ---


def test_口の型を満たす(tmp_path: Path) -> None:
    hat, *_ = _make(tmp_path)
    assert isinstance(hat, SensorPort)


def test_床の値は上と下のバイトを組み立てる(tmp_path: Path) -> None:
    bus = FakeSmbus()
    bus.values = {(0x14, 0x17): (0x01, 0x2C), (0x14, 0x16): (0x0F, 0xFF), (0x14, 0x15): (0, 0)}
    hat, lg, _, _ = _make(tmp_path, bus=bus)
    _echo(lg, 1_000_000)
    assert hat.read().floor == (300, 4095, 0)


def test_1_回の_i2c_rdwr_で書いてから_2_バイト読む(tmp_path: Path) -> None:
    hat, lg, bus, _ = _make(tmp_path)
    _echo(lg, 1_000_000)
    hat.read()
    reads = [t for t in bus.transfers if t[0].data and t[0].data[0] in {0x17, 0x16, 0x15}]
    last = reads[-3:]
    assert [(w.addr, w.data, r.addr, r.length) for w, r in last] == [
        (0x14, [0x17, 0, 0], 0x14, 2),
        (0x14, [0x16, 0, 0], 0x14, 2),
        (0x14, [0x15, 0, 0], 0x14, 2),
    ]


def test_範囲の外の値は読めない(tmp_path: Path) -> None:
    bus = FakeSmbus()
    bus.values = {(0x14, 0x17): (0x10, 0x00), (0x14, 0x16): (0x0F, 0xFF), (0x14, 0x15): (0, 5)}
    hat, lg, _, _ = _make(tmp_path, bus=bus)
    _echo(lg, 1_000_000)
    assert hat.read().floor == (None, 4095, 5)


def test_0x14_が答えなければ_0x15_を使う(tmp_path: Path) -> None:
    bus = FakeSmbus()
    bus.dead = {0x14}
    bus.values = {(0x15, 0x17): (0, 10), (0x15, 0x16): (0, 20), (0x15, 0x15): (0, 30)}
    hat, lg, _, _ = _make(tmp_path, bus=bus)
    _echo(lg, 1_000_000)
    assert hat.read().floor == (10, 20, 30)
    assert bus.buses[0].bus == 1


def test_どのアドレスも答えなければ床は読めない(tmp_path: Path) -> None:
    bus = FakeSmbus()
    bus.dead = {0x14, 0x15}
    hat, lg, _, _ = _make(tmp_path, bus=bus)
    _echo(lg, 1_000_000)
    reading = hat.read()
    assert reading.floor == (None, None, None)
    assert reading.distance_cm is not None


def test_1_つの読みの失敗はその床だけ読めない(tmp_path: Path) -> None:
    bus = FakeSmbus()
    bus.values = {(0x14, 0x17): (0, 1), (0x14, 0x15): (0, 3)}
    hat, lg, _, _ = _make(tmp_path, bus=bus)
    _echo(lg, 1_000_000)
    hat.read()
    bus.broken = {(0x14, 0x16)}
    _echo(lg, 1_000_000)
    assert hat.read().floor == (1, None, 3)


def test_bus_が開けなければ床は読めず例外を出さない(tmp_path: Path) -> None:
    bus = FakeSmbus()
    bus.fail_open = True
    hat, lg, _, _ = _make(tmp_path, bus=bus)
    _echo(lg, 1_000_000)
    assert hat.read().floor == (None, None, None)


# --- 超音波 ---


def test_echo_の幅から距離を出す(tmp_path: Path) -> None:
    hat, lg, _, _ = _make(tmp_path)
    _echo(lg, 2_000_000)  # 2 ms
    reading = hat.read()
    assert reading.distance_cm == pytest.approx(2_000_000 * SOUND_CM_PER_NS / 2)
    assert reading.far is False


def test_trig_と_echo_のピンだけを使う(tmp_path: Path) -> None:
    hat, lg, _, _ = _make(tmp_path)
    _echo(lg, 1_000_000)
    hat.read()
    hat.close()
    assert ("gpiochip_open", 4) in lg.calls
    assert ("gpio_claim_output", 7, 27, 0) in lg.calls
    assert ("gpio_claim_alert", 7, 22, lg.BOTH_EDGES, lg.SET_PULL_DOWN) in lg.calls
    assert ("callback", 7, 22, lg.BOTH_EDGES) in lg.calls
    assert ("gpio_trigger", 7, 27, 10, 1) in lg.calls
    assert lg.pins() == {27, 22}


def test_echo_が上がらなければ距離は読めない(tmp_path: Path) -> None:
    hat, lg, _, _ = _make(tmp_path)
    lg.edges = []
    reading = hat.read()
    assert reading.distance_cm is None
    assert reading.far is False


def test_上がって下がらず_echo_がまだ高ければ物がない(tmp_path: Path) -> None:
    hat, lg, _, _ = _make(tmp_path)
    lg.edges = [(1, RISE_NS)]
    lg.echo_level = 1
    reading = hat.read()
    assert reading.far is True
    assert reading.distance_cm is None
    assert ("gpio_read", 7, 22) in lg.calls


def test_下がりの知らせが遅れて_echo_がもう低ければ危険(tmp_path: Path) -> None:
    hat, lg, _, _ = _make(tmp_path)
    lg.edges = [(1, RISE_NS)]
    lg.echo_level = 0
    reading = hat.read()
    assert reading.far is False
    assert reading.distance_cm is None


def test_echo_を読めなければ危険(tmp_path: Path) -> None:
    hat, lg, _, _ = _make(tmp_path)
    lg.edges = [(1, RISE_NS)]
    lg.echo_level = FakeLgpioError("read")
    reading = hat.read()
    assert reading.far is False
    assert reading.distance_cm is None


def test_待ちより長い_echo_も物がない(tmp_path: Path) -> None:
    hat, lg, _, _ = _make(tmp_path)
    _echo(lg, 38_000_000)
    reading = hat.read()
    assert reading.far is True


def test_上がりのない下がりは使わない(tmp_path: Path) -> None:
    hat, lg, _, _ = _make(tmp_path)
    lg.edges = [(0, RISE_NS), (1, RISE_NS + 10), (0, RISE_NS + 10 + 1_000_000)]
    reading = hat.read()
    assert reading.distance_cm == pytest.approx(1_000_000 * SOUND_CM_PER_NS / 2)


def test_読みの合間は_60_ms_あける(tmp_path: Path) -> None:
    hat, lg, _, clock = _make(tmp_path)
    _echo(lg, 1_000_000)
    hat.read()
    clock.advance(0.02)
    hat.read()
    assert sum(clock.sleeps) == pytest.approx(0.04)
    clock.advance(0.1)
    before = len(clock.sleeps)
    hat.read()
    assert len(clock.sleeps) == before


def test_パルスの失敗は距離が読めず例外を出さない(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    hat, lg, _, _ = _make(tmp_path)
    lg.fail_trigger = FakeLgpioError("trigger")
    with caplog.at_level(logging.WARNING):
        reading = hat.read()
    assert reading.distance_cm is None
    assert reading.floor == (0, 0, 0)
    assert "FakeLgpioError" in caplog.text
    assert "trigger" not in caplog.text


def test_gpiochip_が開けなければ距離は読めない(tmp_path: Path) -> None:
    lg = FakeLgpio()
    lg.fail_open = True
    hat, _, _, _ = _make(tmp_path, lg=lg)
    assert hat.read().distance_cm is None


def test_gpiochip_が見つからなければ距離は読めない(tmp_path: Path) -> None:
    _sysfs(tmp_path / "sys", {0: {"label": b"other\n"}})
    hat, lg, _, _ = _make(tmp_path)
    _echo(lg, 1_000_000)
    assert hat.read().distance_cm is None
    assert lg.calls == []


def test_失敗のログは間をあけて型の名前だけ(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bus = FakeSmbus()
    bus.broken = {(0x14, 0x16)}
    hat, lg, _, clock = _make(tmp_path, bus=bus)
    _echo(lg, 1_000_000)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            hat.read()
            clock.advance(0.1)
    assert caplog.text.count("OSError") == 1
    assert "nack" not in caplog.text


def test_開けない失敗は初めの_1_回だけログに出る(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    lg = FakeLgpio()
    lg.fail_open = True
    hat, _, _, clock = _make(tmp_path, lg=lg)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            hat.read()
            clock.advance(10.0)
    assert caplog.text.count("FakeLgpioError") == 1
    assert "busy" not in caplog.text


def test_開けなかったら間をあけて開き直す(tmp_path: Path) -> None:
    lg = FakeLgpio()
    lg.fail_open = True
    hat, _, _, clock = _make(tmp_path, lg=lg)
    hat.read()
    lg.fail_open = False
    clock.advance(0.1)
    assert hat.read().distance_cm is None  # まだ開き直さない
    clock.advance(10.0)
    _echo(lg, 1_000_000)
    assert hat.read().distance_cm is not None


def test_思わぬ例外でも読めない値で返す(tmp_path: Path) -> None:
    hat, lg, _, _ = _make(tmp_path)
    lg.fail_trigger = RuntimeError("hidden")
    reading = hat.read()
    assert reading.distance_cm is None


# --- 閉じる ---


def test_閉じるとピンを放して口を閉じる(tmp_path: Path) -> None:
    hat, lg, bus, _ = _make(tmp_path)
    _echo(lg, 1_000_000)
    hat.read()
    hat.close()
    assert lg.callback_obj is not None and lg.callback_obj.cancelled == 1
    assert ("gpio_free", 7, 27) in lg.calls
    assert ("gpio_free", 7, 22) in lg.calls
    assert ("gpiochip_close", 7) in lg.calls
    assert bus.buses[0].closed == 1


def test_閉じるのは何度でもよい(tmp_path: Path) -> None:
    hat, lg, bus, _ = _make(tmp_path)
    _echo(lg, 1_000_000)
    hat.read()
    hat.close()
    hat.close()
    assert lg.calls.count(("gpiochip_close", 7)) == 1
    assert bus.buses[0].closed == 1


def test_閉じた後の読みは読めない値(tmp_path: Path) -> None:
    hat, lg, _, _ = _make(tmp_path)
    hat.close()
    _echo(lg, 1_000_000)
    reading = hat.read()
    assert reading.distance_cm is None
    assert reading.floor == (None, None, None)
    assert lg.calls == []


def test_開く前に閉じても何もしない(tmp_path: Path) -> None:
    hat, lg, bus, _ = _make(tmp_path)
    hat.close()
    assert lg.calls == []
    assert bus.buses == []


def test_ほかのスレッドから読んでも混ざらない(tmp_path: Path) -> None:
    hat, lg, _, _ = _make(tmp_path)
    _echo(lg, 1_000_000)
    results: list[float | None] = []

    def worker() -> None:
        for _ in range(5):
            results.append(hat.read().distance_cm)

    threads = [threading.Thread(target=worker, name="remote-test") for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    assert len(results) == 15
    assert all(r == pytest.approx(1_000_000 * SOUND_CM_PER_NS / 2) for r in results)
