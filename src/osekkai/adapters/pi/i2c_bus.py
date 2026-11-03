"""I2C のバス 1 を共有する部品。HAT と LCD の書き込みを 1 つのロックで直列にする。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import Any, Protocol, cast


class _SMBusLike(Protocol):
    def write_quick(self, i2c_addr: int, /) -> Any: ...
    def write_byte(self, i2c_addr: int, value: int, /) -> Any: ...
    def write_i2c_block_data(self, i2c_addr: int, register: int, data: list[int], /) -> Any: ...
    def close(self) -> Any: ...


class _SMBusRdwr(Protocol):
    def i2c_rdwr(self, *msgs: Any) -> Any: ...


MessageWriter = Callable[[int, list[int]], Any]


def _write_message(addr: int, data: list[int]) -> Any:
    from smbus2 import i2c_msg  # 実機だけ

    return i2c_msg.write(addr, data)


def _open_smbus(bus_number: int) -> _SMBusLike:
    from smbus2 import SMBus  # 実機だけ

    bus: _SMBusLike = SMBus(bus_number)
    return bus


class I2cBus:
    """バスを最初の操作で 1 回だけ開く。書き込みの失敗は OSError のまま投げる。"""

    def __init__(
        self,
        bus_number: int = 1,
        *,
        opener: Callable[[int], _SMBusLike] | None = None,
        message_writer: MessageWriter | None = None,
    ) -> None:
        self._bus_number = bus_number
        self._opener = opener or _open_smbus
        self._message_writer = message_writer or _write_message
        self._bus: _SMBusLike | None = None
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        """続けて書く間、ほかの書き込みを止めるためのロック（再入できる）。"""
        return self._lock

    def _smbus(self) -> _SMBusLike:
        if self._bus is None:
            self._bus = self._opener(self._bus_number)
        return self._bus

    def probe(self, addr: int) -> bool:
        """アドレスが応えるか（i2cdetect と同じ quick write で確かめる）。"""
        with self._lock:
            bus = self._smbus()  # 開けないときは OSError のまま投げる
            try:
                bus.write_quick(addr)
            except OSError:
                return False
            return True

    def write_byte(self, addr: int, value: int) -> None:
        with self._lock:
            self._smbus().write_byte(addr, value)

    def write_block(self, addr: int, register: int, data: Sequence[int]) -> None:
        with self._lock:
            self._smbus().write_i2c_block_data(addr, register, list(data))

    def write_block_emergency(self, addr: int, register: int, data: Sequence[int]) -> None:
        """非常停止だけが使う。ロックを取らず、I2C_RDWR の 1 回の ioctl で書く。

        I2C_RDWR は 1 通ずつ相手のアドレスを持つので、共有の fd の相手（I2C_SLAVE）を
        変えず、ほかのスレッドの書き込みの途中にも割り込まない。
        線の上のバイト列は write_i2c_block_data と同じ（アドレス＋W、レジスタ、データ）。
        """
        message = self._message_writer(addr, [register, *data])
        cast(_SMBusRdwr, self._smbus()).i2c_rdwr(message)  # smbus2.SMBus は i2c_rdwr を持つ

    def close(self) -> None:
        with self._lock:
            if self._bus is not None:
                bus, self._bus = self._bus, None
                bus.close()
