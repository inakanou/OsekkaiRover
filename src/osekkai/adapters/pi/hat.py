"""Robot HAT のマイコン（PWM のレジスタ、リセット、版の判定）。

レジスタの値は SunFounder robot-hat 2.5.x の `pwm.py` に合わせている:
- アドレス 0x14（予備 0x15、0x16）
- チャンネルの値 REG_CHN = 0x20 + ch（20 チャンネル）
- プリスケーラ REG_PSC = 0x40 + timer、周期 REG_ARR = 0x44 + timer（timer 0〜3）
- timer 4〜6 は REG_PSC2 = 0x50、REG_ARR2 = 0x54 から
- タイマーは ch 0〜15 が 4 つずつ、16・17 が timer 4、18 が 5、19 が 6
- 16 ビットの値は [上位, 下位] のブロック書き込み。プリスケーラは「値 − 1」を書く
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, Literal, Protocol

from osekkai.adapters.pi.i2c_bus import I2cBus

logger = logging.getLogger(__name__)

HatVersion = Literal["v4", "v5", "unknown"]

ADDRESSES: Final[tuple[int, ...]] = (0x14, 0x15, 0x16)
PWM_CHANNELS: Final = 20
REG_CHN: Final = 0x20
REG_PSC: Final = 0x40
REG_ARR: Final = 0x44
REG_PSC2: Final = 0x50
REG_ARR2: Final = 0x54

MCU_RESET_PIN: Final = 5  # BCM
V5_UUID: Final = "9daeea78-0000-076e-0032-582369ac3e02"
DEVICE_TREE: Final = Path("/proc/device-tree")


class _OutputPins(Protocol):
    def claim_output(self, pin: int, level: int) -> None: ...
    def write(self, pin: int, level: int) -> None: ...


def _check_channel(channel: int) -> None:
    if not 0 <= channel < PWM_CHANNELS:
        raise ValueError(f"PWM channel out of range: {channel}")


def channel_register(channel: int) -> int:
    _check_channel(channel)
    return REG_CHN + channel


def timer_index(channel: int) -> int:
    _check_channel(channel)
    if channel < 16:
        return channel // 4
    if channel < 18:
        return 4
    return channel - 13  # 18 → 5、19 → 6


def prescaler_register(timer: int) -> int:
    return REG_PSC + timer if timer < 4 else REG_PSC2 + timer - 4


def period_register(timer: int) -> int:
    return REG_ARR + timer if timer < 4 else REG_ARR2 + timer - 4


def encode_word(value: int) -> list[int]:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"not a 16-bit value: {value}")
    return [value >> 8, value & 0xFF]


def read_hat_version(device_tree: Path = DEVICE_TREE) -> HatVersion:
    """`*hat*/uuid` を読む。v5 の UUID なら v5、それ以外は v4（robot-hat 2.5.8 と同じ）。

    v4 には EEPROM がないので、hat の項目も uuid もないのが普通。
    デバイスツリー自体が読めないときだけ unknown にする。
    """
    try:
        folders = sorted(p for p in device_tree.iterdir() if "hat" in p.name)
    except OSError:
        return "unknown"
    for folder in folders:
        try:
            raw = (folder / "uuid").read_bytes()
        except OSError:
            continue
        uuid = raw.replace(b"\x00", b"").decode("ascii", "replace").strip().lower()
        if uuid == V5_UUID:
            return "v5"
    return "v4"


class HatMcu:
    """HAT のマイコン。作るときに応えるアドレスを探す（なければ OSError）。"""

    def __init__(
        self,
        bus: I2cBus,
        gpio: _OutputPins,
        *,
        sleep: Callable[[float], None] = time.sleep,
        device_tree: Path = DEVICE_TREE,
        addresses: Sequence[int] = ADDRESSES,
    ) -> None:
        self._bus = bus
        self._gpio = gpio
        self._sleep = sleep
        self._version: HatVersion = read_hat_version(device_tree)
        found = next((a for a in addresses if bus.probe(a)), None)
        if found is None:
            # 固まったマイコンは応えないので、1 回だけリセットして試し直す
            self.reset()
            found = next((a for a in addresses if bus.probe(a)), None)
        if found is None:
            raise OSError("Robot HAT MCU did not answer on I2C")
        self.address: Final[int] = found
        logger.info("Robot HAT %s at 0x%02x", self._version, found)

    @property
    def bus(self) -> I2cBus:
        return self._bus

    def hat_version(self) -> HatVersion:
        return self._version

    def _write_word(self, register: int, value: int) -> None:
        self._bus.write_block(self.address, register, encode_word(value))

    def set_channel_value(self, channel: int, value: int) -> None:
        self._write_word(channel_register(channel), value)

    def set_prescaler(self, channel: int, prescaler: int) -> None:
        """チャンネルのタイマーのプリスケーラ（同じタイマーのチャンネルで共有）。"""
        if prescaler < 1:
            raise ValueError(f"prescaler must be >= 1: {prescaler}")
        self._write_word(prescaler_register(timer_index(channel)), prescaler - 1)

    def set_period(self, channel: int, period: int) -> None:
        """チャンネルのタイマーの周期（ARR）。"""
        self._write_word(period_register(timer_index(channel)), period)

    def reset(self) -> None:
        """BCM 5 を 10 ms Low にしてから High にし、0.2 秒待つ。その間 I2C は書かない。"""
        with self._bus.lock:
            self._gpio.claim_output(MCU_RESET_PIN, 1)
            self._gpio.write(MCU_RESET_PIN, 0)
            self._sleep(0.01)
            self._gpio.write(MCU_RESET_PIN, 1)
            self._sleep(0.2)
