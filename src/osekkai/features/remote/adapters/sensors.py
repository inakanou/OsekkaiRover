"""HAT の超音波と床センサーを読む実機のアダプタ（要件 5.1、5.4）。

- 超音波は lgpio で読む。trig は D2（BCM 27）、echo は D3（BCM 22）。
  echo の上がりと下がりの端の時刻（ns）の差 × 343.3 m/s ÷ 2 で距離を出す
- 床センサーは smbus2 で HAT の ADC（A0〜A2）を読む。書き込みと 2 バイトの読み取りを
  1 回の `i2c_rdwr` で行う（基盤の LCD や PWM の書き込みと混ざらない）
- 開けない・読めないときは例外を外に出さず、読めない値（None）で返す。危険として扱われる
- smbus2 と lgpio は関数の中だけで読む。基盤が持つ GPIO（25・26・5 など）には触らない
- ログに出すのは例外の型の名前だけ。同じ種類の失敗は間をあけて出す
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Final

from osekkai.core.clock import Clock

from ..ports import SensorReading

logger = logging.getLogger(__name__)

SYS_ROOT: Final = Path("/sys")
DEV_ROOT: Final = Path("/dev")
GPIOCHIP_COMPATIBLE: Final = "raspberrypi,rp1-gpio"  # Pi 5 の 40 ピンの口
GPIOCHIP_LABEL: Final = "pinctrl-rp1"
GPIOCHIP_PREFIX: Final = "gpiochip"

SOUND_M_PER_S: Final = 343.3
TRIG_PULSE_US: Final = 10
ADC_MAX: Final = 4095
REOPEN_INTERVAL_S: Final = 5.0  # 開けなかったとき、開き直すまでの間
LOG_INTERVAL_S: Final = 60.0  # 同じ種類の読みの失敗をログに出す間

_UNREADABLE: Final = SensorReading(distance_cm=None, floor=(None, None, None))


def _module_available(name: str) -> bool:
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def sensors_available() -> bool:
    """smbus2 と lgpio を読み込めるか。開発 PC と CI では偽。"""
    return _module_available("smbus2") and _module_available("lgpio")


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("ascii", errors="replace")
    except OSError:
        return ""


def _sysfs_gpiochip(sys_root: Path) -> str | None:
    devices = sys_root / "bus" / "gpio" / "devices"
    try:
        entries = list(devices.glob(f"{GPIOCHIP_PREFIX}*"))
    except OSError:
        return None
    for number, entry in _numbered(entries):
        compatible = _read_text(entry / "of_node" / "compatible").split("\x00")
        label = _read_text(entry / "label").strip()
        if GPIOCHIP_COMPATIBLE in compatible or label == GPIOCHIP_LABEL:
            return f"/dev/{GPIOCHIP_PREFIX}{number}"
    return None


def _numbered(entries: list[Path]) -> list[tuple[int, Path]]:
    numbers: list[tuple[int, Path]] = []
    for entry in entries:
        suffix = entry.name.removeprefix(GPIOCHIP_PREFIX)
        if suffix.isdigit():
            numbers.append((int(suffix), entry))
    return sorted(numbers)


def _lgpio_gpiochip(dev_root: Path, lg: Any) -> str | None:
    """lgpio で /dev/gpiochipN を開き、label が `pinctrl-rp1` のものを探す。

    ほかの `pinctrl-` や chip 0 には倒さない（ちがうピンを動かさない）。
    """
    try:
        entries = list(dev_root.glob(f"{GPIOCHIP_PREFIX}*"))
    except OSError:
        return None
    for number, _ in _numbered(entries):
        try:
            handle = lg.gpiochip_open(number)
        except Exception:  # lgpio.error など。開けない chip は飛ばす
            continue
        try:
            label = str(lg.gpio_get_chip_info(handle)[3])
        except Exception:
            label = ""
        finally:
            try:
                lg.gpiochip_close(handle)
            except Exception as e:
                logger.debug("gpiochip を閉じられませんでした: %s", type(e).__name__)
        if label == GPIOCHIP_LABEL:
            return f"/dev/{GPIOCHIP_PREFIX}{number}"
    return None


def find_gpiochip(
    *, sys_root: Path = SYS_ROOT, dev_root: Path = DEV_ROOT, lgpio_module: Any = None
) -> str | None:
    """Pi 5 の 40 ピンの gpiochip の道（/dev/gpiochipN）。見つからなければ None。

    番号はカーネルの版で変わる。まず sysfs の `of_node/compatible` か `label` で探す。
    新しいカーネルでは sysfs に label がないことがあるので、次に lgpio の
    `gpio_get_chip_info` の label で探す。lgpio を読めなければ sysfs だけで探す。
    """
    found = _sysfs_gpiochip(sys_root)
    if found is not None:
        return found
    lg = lgpio_module
    if lg is None:
        try:
            lg = _import_lgpio()
        except ImportError:
            return None
    return _lgpio_gpiochip(dev_root, lg)


def _chip_number(path: str) -> int:
    return int(path.rsplit("/", 1)[-1].removeprefix(GPIOCHIP_PREFIX))


def _import_lgpio() -> Any:
    import lgpio  # 実機だけ

    return lgpio


def _import_smbus2() -> Any:
    import smbus2  # 実機だけ

    return smbus2


class _Echo:
    """1 回の測りの echo の端。lgpio の callback のスレッドから書かれる。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._armed = False
        self._rise: int | None = None
        self._fall: int | None = None
        self.done = threading.Event()

    def arm(self) -> None:
        with self._lock:
            self._armed = True
            self._rise = None
            self._fall = None
            self.done.clear()

    def disarm(self) -> tuple[int | None, int | None]:
        with self._lock:
            self._armed = False
            return self._rise, self._fall

    def on_edge(self, chip: int, gpio: int, level: int, tick: int) -> None:
        with self._lock:
            if not self._armed:
                return
            if level == 1 and self._rise is None:
                self._rise = tick
            elif level == 0 and self._rise is not None and self._fall is None:
                self._fall = tick
                self.done.set()


class HatSensors:
    """HAT の超音波と床センサー（SensorPort）。初めの読みで開く。"""

    TRIG_BCM: Final = 27  # D2
    ECHO_BCM: Final = 22  # D3
    ECHO_TIMEOUT_S: Final = 0.03
    MIN_INTERVAL_S: Final = 0.06
    ADC_ADDRESSES: Final = (0x14, 0x15)
    FLOOR_REGISTERS: Final = (0x17, 0x16, 0x15)  # A0〜A2

    def __init__(
        self,
        *,
        clock: Clock,
        bus_number: int = 1,
        lgpio_module: Any = None,
        smbus_module: Any = None,
        sys_root: Path = SYS_ROOT,
        dev_root: Path = DEV_ROOT,
    ) -> None:
        self._clock = clock
        self._bus_number = bus_number
        self._lg_module = lgpio_module
        self._smbus_module = smbus_module
        self._sys_root = sys_root
        self._dev_root = dev_root
        self._lock = threading.Lock()
        self._closed = False
        self._echo = _Echo()
        # 超音波
        self._lg: Any = None
        self._handle: Any = None
        self._claimed: list[int] = []
        self._callback: Any = None
        self._gpio_retry_at: float | None = None
        self._last_ping: float | None = None
        # 床センサー
        self._bus: Any = None
        self._msg: Any = None
        self._address: int | None = None
        self._bus_retry_at: float | None = None
        # ログ
        self._logged_at: dict[str, float] = {}

    # --- 公開の口 ---

    def read(self) -> SensorReading:
        """1 回読む。最長でおよそ 0.1 秒。例外は出さない。"""
        with self._lock:
            if self._closed:
                return _UNREADABLE
            try:
                distance, far = self._read_distance()
            except Exception as e:  # 思わぬ失敗も読めない値にする（5.4）
                self._warn("ultrasonic", e)
                distance, far = None, False
            try:
                floor = self._read_floor()
            except Exception as e:
                self._warn("floor", e)
                floor = (None, None, None)
            return SensorReading(distance_cm=distance, floor=floor, far=far)

    def close(self) -> None:
        """ピンを放して口を閉じる。何度呼んでもよい。"""
        with self._lock:
            self._closed = True
            self._close_gpio()
            self._close_bus()

    # --- 超音波 ---

    def _open_gpio(self) -> bool:
        if self._handle is not None:
            return True
        now = self._clock.monotonic()
        if self._gpio_retry_at is not None and now < self._gpio_retry_at:
            return False
        self._gpio_retry_at = now + REOPEN_INTERVAL_S
        try:
            lg = self._lg_module if self._lg_module is not None else _import_lgpio()
        except Exception as e:
            self._warn_once("gpio_open", type(e).__name__)
            return False
        chip = find_gpiochip(sys_root=self._sys_root, dev_root=self._dev_root, lgpio_module=lg)
        if chip is None:
            self._warn_once("gpiochip", "見つかりません")
            return False
        try:
            self._lg = lg
            self._handle = lg.gpiochip_open(_chip_number(chip))
            lg.gpio_claim_output(self._handle, self.TRIG_BCM, 0)
            self._claimed.append(self.TRIG_BCM)
            lg.gpio_claim_alert(self._handle, self.ECHO_BCM, lg.BOTH_EDGES, lg.SET_PULL_DOWN)
            self._claimed.append(self.ECHO_BCM)
            self._callback = lg.callback(
                self._handle, self.ECHO_BCM, lg.BOTH_EDGES, self._echo.on_edge
            )
        except Exception as e:
            self._warn_once("gpio_open", type(e).__name__)
            self._close_gpio()
            return False
        return True

    def _read_distance(self) -> tuple[float | None, bool]:
        if not self._open_gpio():
            return None, False
        self._keep_interval()
        self._echo.arm()
        try:
            self._lg.gpio_trigger(self._handle, self.TRIG_BCM, TRIG_PULSE_US, 1)
            self._echo.done.wait(self.ECHO_TIMEOUT_S)
        finally:
            rise, fall = self._echo.disarm()
        if rise is None:
            return None, False  # 上がらない＝読み取りの失敗
        if fall is None:
            return self._after_timeout()
        width_s = (fall - rise) / 1e9
        if width_s > self.ECHO_TIMEOUT_S:
            return None, True
        if width_s < 0:
            return None, False
        return width_s * SOUND_M_PER_S / 2 * 100, False

    def _after_timeout(self) -> tuple[float | None, bool]:
        """上がったが待ちの中で下がりの知らせがない。echo がまだ高いときだけ物がない。

        もう低いのは、下がりの知らせが遅れただけ（近くに物があるかもしれない）。
        読めないときも危険とする。遅れた知らせを安全にしない。
        """
        try:
            level = self._lg.gpio_read(self._handle, self.ECHO_BCM)
        except Exception as e:
            self._warn("echo_level", e)
            return None, False
        if level == 1:
            return None, True  # 待ちの中で下がらない＝前に物がない
        return None, False

    def _keep_interval(self) -> None:
        now = self._clock.monotonic()
        if self._last_ping is not None:
            wait = self._last_ping + self.MIN_INTERVAL_S - now
            if wait > 0:
                self._clock.sleep(wait)
                now = self._clock.monotonic()
        self._last_ping = now

    def _close_gpio(self) -> None:
        lg, handle = self._lg, self._handle
        callback, self._callback = self._callback, None
        claimed, self._claimed = self._claimed, []
        self._handle = None
        if callback is not None:
            self._quietly(callback.cancel)
        if handle is None:
            return
        for pin in claimed:
            self._quietly(lg.gpio_free, handle, pin)
        self._quietly(lg.gpiochip_close, handle)

    # --- 床センサー ---

    def _open_bus(self) -> bool:
        if self._bus is not None:
            return True
        now = self._clock.monotonic()
        if self._bus_retry_at is not None and now < self._bus_retry_at:
            return False
        self._bus_retry_at = now + REOPEN_INTERVAL_S
        try:
            smbus = self._smbus_module if self._smbus_module is not None else _import_smbus2()
            self._msg = smbus.i2c_msg
            self._bus = smbus.SMBus(self._bus_number)
        except Exception as e:
            self._warn_once("bus_open", type(e).__name__)
            self._bus = None
            return False
        for address in self.ADC_ADDRESSES:
            try:
                self._read_register(address, self.FLOOR_REGISTERS[0])
            except Exception:  # 答えないアドレスは次を試す
                continue
            self._address = address
            return True
        self._warn_once("adc", "答えません")
        self._close_bus()
        return False

    def _read_register(self, address: int, register: int) -> int:
        write = self._msg.write(address, [register, 0, 0])
        read = self._msg.read(address, 2)
        self._bus.i2c_rdwr(write, read)
        data = [int(b) for b in read]
        return (data[0] << 8) | data[1]

    def _read_floor(self) -> tuple[int | None, int | None, int | None]:
        if not self._open_bus() or self._address is None:
            return (None, None, None)
        values: list[int | None] = []
        for register in self.FLOOR_REGISTERS:
            try:
                value = self._read_register(self._address, register)
            except Exception as e:
                self._warn("floor", e)
                values.append(None)
                continue
            values.append(value if 0 <= value <= ADC_MAX else None)
        return (values[0], values[1], values[2])

    def _close_bus(self) -> None:
        bus, self._bus = self._bus, None
        self._address = None
        if bus is not None:
            self._quietly(bus.close)

    # --- ログと後始末 ---

    def _warn(self, kind: str, error: BaseException) -> None:
        now = self._clock.monotonic()
        last = self._logged_at.get(kind)
        if last is not None and now - last < LOG_INTERVAL_S:
            return
        self._logged_at[kind] = now
        logger.warning("センサーを読めませんでした（%s）: %s", kind, type(error).__name__)

    def _warn_once(self, kind: str, reason: str) -> None:
        if kind in self._logged_at:
            return
        self._logged_at[kind] = self._clock.monotonic()
        logger.warning("センサーを開けませんでした（%s）: %s", kind, reason)

    @staticmethod
    def _quietly(func: Any, *args: Any) -> None:
        try:
            func(*args)
        except Exception as e:  # 後始末の失敗は型の名前だけ残して続ける
            logger.debug("センサーの後始末に失敗しました: %s", type(e).__name__)
