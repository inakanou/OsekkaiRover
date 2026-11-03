"""lgpio のピン（ボタン、LED、スピーカーの有効化、MCU リセット）。

ピンの割り当ては SunFounder robot-hat 2.5.x の `pin.py`（HAT v4 以降の表）に合わせている。
lgpio がスレッドから安全か分からないので、GPIO の操作は I2C とは別の 1 つのロックで直列にする。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal

from osekkai.adapters.pi.hat import HatVersion

logger = logging.getLogger(__name__)

# robot-hat 2.5.x pin.py の表（D0〜D16 と名前つきのピン → BCM）
PIN_MAP: Final[Mapping[str, int]] = {
    "D0": 17,
    "D1": 4,
    "D2": 27,
    "D3": 22,
    "D4": 23,
    "D5": 24,
    "D6": 25,
    "D7": 4,
    "D8": 5,
    "D9": 6,
    "D10": 12,
    "D11": 13,
    "D12": 19,
    "D13": 16,
    "D14": 26,
    "D15": 20,
    "D16": 21,
    "USR": 25,
    "LED": 26,
    "MCURST": 5,
}

USR_BUTTON_PIN: Final = 25  # プルアップ、押すと 0
LED_PIN: Final = 26
SPEAKER_PIN_V4: Final = 20
SPEAKER_PIN_V5: Final = 12

GPIOCHIP_ENV: Final = "ROBOT_HAT_GPIOCHIP"
GPIOCHIP_LABEL: Final = "pinctrl-rp1"  # Pi 5。番号はカーネルで変わる


def resolve_pin(name: str) -> int:
    """`D0` などの名前を BCM の番号にする。"""
    try:
        return PIN_MAP[name.strip().upper()]
    except KeyError:
        raise ValueError(f"unknown pin name: {name!r}") from None


def select_gpiochip(labels: Mapping[int, str], env_value: str | None) -> int:
    """環境変数の番号、`pinctrl-rp1`、ほかの `pinctrl-`（Pi 4 以前）、0 の順に選ぶ。"""
    if env_value is not None and env_value.strip():
        try:
            return int(env_value.strip())
        except ValueError:
            raise ValueError(f"{GPIOCHIP_ENV} is not a number") from None
    chips = sorted(labels)
    exact = [c for c in chips if labels[c] == GPIOCHIP_LABEL]
    other = [c for c in chips if labels[c].startswith("pinctrl-")]
    return next(iter(exact + other), 0)


def speaker_pin(version: HatVersion, configured: Literal["auto"] | int) -> int:
    """スピーカーを有効にするピン。版が分からないときは v4 のピンにする。"""
    if configured != "auto":
        return configured
    return SPEAKER_PIN_V5 if version == "v5" else SPEAKER_PIN_V4


def _chip_numbers(dev: Path = Path("/dev")) -> list[int]:
    numbers: list[int] = []
    for path in dev.glob("gpiochip*"):
        suffix = path.name.removeprefix("gpiochip")
        if suffix.isdigit():
            numbers.append(int(suffix))
    return sorted(numbers)


def _import_lgpio() -> Any:
    import lgpio  # 実機だけ

    return lgpio


class Gpio:
    """1 つの gpiochip を開いて持つ。すべての lgpio の呼び出しを 1 つのロックで直列にする。"""

    def __init__(
        self,
        *,
        lg: Any = None,
        environ: Mapping[str, str] | None = None,
        chips: Sequence[int] | None = None,
    ) -> None:
        self._lg: Any = lg if lg is not None else _import_lgpio()
        self._lock = threading.RLock()
        self._claimed: set[int] = set()
        env = (os.environ if environ is None else environ).get(GPIOCHIP_ENV)
        if env is not None and env.strip():
            self.chip: int = select_gpiochip({}, env)
            self._handle: Any = self._lg.gpiochip_open(self.chip)
        else:
            self.chip, self._handle = self._open_by_label(
                _chip_numbers() if chips is None else chips
            )
        logger.info("gpiochip%d opened", self.chip)

    def _open_by_label(self, chips: Sequence[int]) -> tuple[int, Any]:
        handles: dict[int, Any] = {}
        labels: dict[int, str] = {}
        for chip in chips:
            try:
                handle = self._lg.gpiochip_open(chip)
            except Exception:  # lgpio.error など。開けないチップは候補から外す
                continue
            handles[chip] = handle
            try:
                labels[chip] = str(self._lg.gpio_get_chip_info(handle)[3])
            except Exception:
                labels[chip] = ""
        chosen = select_gpiochip(labels, None)
        for chip, handle in handles.items():
            if chip != chosen:
                self._lg.gpiochip_close(handle)
        if chosen in handles:
            return chosen, handles[chosen]
        return chosen, self._lg.gpiochip_open(chosen)

    @property
    def lg(self) -> Any:
        return self._lg

    def claim_output(self, pin: int, level: int) -> None:
        """出力として取る（取り済みなら何もしない）。"""
        with self._lock:
            if pin not in self._claimed:
                self._lg.gpio_claim_output(self._handle, pin, level)
                self._claimed.add(pin)

    def write(self, pin: int, level: int) -> None:
        with self._lock:
            self._lg.gpio_write(self._handle, pin, 1 if level else 0)

    def watch_falling(
        self, pin: int, func: Callable[[int, int, int, int], None], *, pull_up: bool = True
    ) -> Any:
        """立ち下がりを lgpio のスレッドで func に知らせる。戻り値は cancel() を持つ。"""
        with self._lock:
            flags = self._lg.SET_PULL_UP if pull_up else 0
            self._lg.gpio_claim_alert(self._handle, pin, self._lg.FALLING_EDGE, flags)
            self._claimed.add(pin)
            return self._lg.callback(self._handle, pin, self._lg.FALLING_EDGE, func)

    def free(self, pin: int) -> None:
        with self._lock:
            if pin in self._claimed:
                self._claimed.discard(pin)
                self._lg.gpio_free(self._handle, pin)

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                handle, self._handle = self._handle, None
                self._claimed.clear()
                self._lg.gpiochip_close(handle)


class HatButtons:
    """USR ボタン（BCM 25）と追加のボタンの立ち下がりを (source, 単調な時刻) で知らせる。"""

    def __init__(
        self,
        gpio: Gpio,
        *,
        extra_pin: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gpio = gpio
        self._monotonic = monotonic
        self._pins: dict[int, str] = {USR_BUTTON_PIN: "usr"}
        if extra_pin is not None:
            pin = resolve_pin(extra_pin)
            if pin == USR_BUTTON_PIN:
                raise ValueError("extra button pin is the USR button")
            self._pins[pin] = "extra"
        self._callbacks: list[Any] = []
        self._lock = threading.Lock()

    def start(self, on_falling: Callable[[str, float], None]) -> None:
        with self._lock:
            if self._callbacks:
                return
            for pin, source in self._pins.items():
                handler = self._handler(source, on_falling)
                self._callbacks.append(self._gpio.watch_falling(pin, handler))

    def _handler(
        self, source: str, on_falling: Callable[[str, float], None]
    ) -> Callable[[int, int, int, int], None]:
        def handle(chip: int, pin: int, level: int, tick: int) -> None:
            if level == 2:  # lgpio の見張りの時間切れ。押下ではない
                return
            try:
                on_falling(source, self._monotonic())
            except Exception:
                logger.exception("button callback failed")

        return handle

    def close(self) -> None:
        with self._lock:
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback.cancel()
        if callbacks:
            for pin in self._pins:
                self._gpio.free(pin)


class HatLed:
    """HAT の LED（BCM 26）。"""

    def __init__(self, gpio: Gpio) -> None:
        self._gpio = gpio
        gpio.claim_output(LED_PIN, 0)

    def set(self, on: bool) -> None:
        self._gpio.write(LED_PIN, 1 if on else 0)


def enable_speaker(gpio: Gpio, version: HatVersion, configured: Literal["auto"] | int) -> int:
    """スピーカーのアンプを有効にする（ピンを High）。使ったピンを返す。"""
    pin = speaker_pin(version, configured)
    gpio.claim_output(pin, 1)
    gpio.write(pin, 1)
    return pin
