"""画面（LCD）と LED の操作（7.1、7.5、7.6）。

行は変換後のセルで数えて 16 セルに切り詰め、空白で埋める。濁点・半濁点は 1 セルなので、
16 セル目のかなの濁点が 17 セル目になるときは、濁点だけが切れる。
ログには失敗した例外の型だけを出し、表示する文字は出さない。
書けない間の警告は最初の 1 回だけ出し、書けるようになるまでは DEBUG にする
（待機画面は毎秒描き直すので、LCD が外れるとログが埋まる）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.ports import DisplayPort, LedPort, Unavailable
from osekkai.core.types import DeviceUnavailable

logger = logging.getLogger(__name__)

WIDTH = 16
ROWS = 2
_SPACE = 0x20


def _fit(text: str) -> tuple[int, ...]:
    codes = to_lcd_codes(text)[:WIDTH]
    return tuple(codes) + (_SPACE,) * (WIDTH - len(codes))


class FailureLog:
    """続けて失敗する間は、最初の 1 回だけ警告する。成功したら次の失敗でまた警告する。

    呼ぶ側のロックの中で使う。
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._failing = False

    def failed(self, message: str, *args: object) -> None:
        level = logging.DEBUG if self._failing else logging.WARNING
        self._failing = True
        self._logger.log(level, message, *args)

    def succeeded(self) -> None:
        self._failing = False


@contextmanager
def _device_errors(device: str, failures: FailureLog) -> Iterator[None]:
    try:
        yield
    except Exception as error:
        failures.failed("%s に書き込めませんでした: %s", device, type(error).__name__)
        raise DeviceUnavailable(device) from error
    failures.succeeded()


class DisplayService:
    """2 行の表示とバックライト。変わった行だけをデバイスに送る。"""

    def __init__(self, port: DisplayPort | Unavailable) -> None:
        self._port = port
        self._lock = threading.Lock()
        self._failures = FailureLog(logger)
        # デバイスに送ったと分かっている行（分からなければ None）
        self._sent: list[tuple[int, ...] | None] = [None] * ROWS

    def show(self, line1: str, line2: str = "") -> None:
        rows = (_fit(line1), _fit(line2))
        with self._lock:
            port = self._available()
            for row, codes in enumerate(rows):
                if self._sent[row] == codes:
                    continue
                # 失敗したら中身が分からないので、次の表示で送り直す
                self._sent[row] = None
                with _device_errors("display", self._failures):
                    port.write_line(row, codes)
                self._sent[row] = codes

    def clear(self) -> None:
        with self._lock:
            port = self._available()
            self._sent = [None] * ROWS
            with _device_errors("display", self._failures):
                port.clear()

    def set_backlight(self, on: bool) -> None:
        with self._lock:
            port = self._available()
            with _device_errors("display", self._failures):
                port.set_backlight(on)

    def _available(self) -> DisplayPort:
        port = self._port
        if isinstance(port, Unavailable):
            raise DeviceUnavailable(port.device)
        return port


class LedService:
    """Robot HAT の LED の点灯と消灯。"""

    def __init__(self, port: LedPort | Unavailable) -> None:
        self._port = port
        self._lock = threading.Lock()
        self._failures = FailureLog(logger)

    def set(self, on: bool) -> None:
        with self._lock:
            port = self._port
            if isinstance(port, Unavailable):
                raise DeviceUnavailable(port.device)
            with _device_errors("led", self._failures):
                port.set(on)
