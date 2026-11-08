"""ラジコンの self-check の項目（1.4、1.5、13.4〜13.6）。

- `remote.setup`: pin とポートの設定が使えるか。ハードの項目ではない
- `remote.sensors`: 前の物の距離と床の 3 つを読めるか（ハードの項目）
- `remote.usb_camera`: USB のカメラのデバイスのファイルがあるか（カメラは開かない）

無効なときは、すべて飛ばす。設定の誤りは `remote.setup` をだめにし、ほかは飛ばす。
ロボットが動いているときは、ポートを確かめない。センサーは基盤が飛ばす（13.5）。
理由はすべて決まった文にする。pin・アドレス・道・読んだ値を入れない（13.6）。
"""

from __future__ import annotations

import importlib.util
import math
import socket
from typing import Final

from osekkai.core.clock import Clock, SystemClock
from osekkai.core.selfcheck import Check, CheckContext, CheckResult, CheckStatus

from .adapters.sensors import FLOOR_WARMUP_S, HatSensors, sensors_available
from .adapters.usb_camera import find_usb_camera
from .config import RemoteConfig
from .ports import SensorPort, SensorReading

__all__ = [
    "REASON_CONFIG_ERROR",
    "REASON_CONFIG_UNUSABLE",
    "REASON_DISABLED",
    "REASON_NO_CAMERA",
    "REASON_NO_FLASK",
    "REASON_NO_LIBRARY",
    "REASON_PIN_UNUSABLE",
    "REASON_PORT_UNUSABLE",
    "REASON_SENSORS_UNREADABLE",
    "REMOTE_CHECKS",
    "flask_available",
    "make_clock",
    "make_sensors",
]

FEATURE_NAME: Final = "remote"
LISTEN_HOST: Final = "0.0.0.0"  # Web のサーバーと同じ（IPv4 だけ）

# 決まった理由の文
REASON_CONFIG_ERROR: Final = "設定に誤りがあります"
REASON_CONFIG_UNUSABLE: Final = "ラジコンの設定を使えない"
REASON_DISABLED: Final = "無効"
REASON_PIN_UNUSABLE: Final = "pin は引用符で囲んだ 4〜8 けたの数字にしてください"
REASON_NO_FLASK: Final = "Web の部品（flask）がありません"
REASON_PORT_UNUSABLE: Final = "ポートを使えません"
REASON_NO_LIBRARY: Final = "センサーのライブラリがない"
REASON_SENSORS_UNREADABLE: Final = "センサーを読めません"
REASON_NO_CAMERA: Final = "USB カメラが見つかりません"

# 床は開いてから FLOOR_WARMUP_S の間は読めない。少し余して待つ
WARMUP_MARGIN_S: Final = 0.2
SETUP_TIMEOUT_S: Final = 5.0
SENSORS_TIMEOUT_S: Final = 6.0
CAMERA_TIMEOUT_S: Final = 5.0

OK: Final = CheckResult(CheckStatus.OK)


class _NotSynced:
    """時刻の同期を問わない（確かめは経った時間だけを使う）。"""

    def is_synchronized(self) -> bool:
        return False


def make_clock() -> Clock:
    """センサーの確かめで使う時計。テストで差し替える。"""
    return SystemClock(_NotSynced())


def make_sensors(clock: Clock) -> SensorPort:
    """実機のセンサー。テストで差し替える。"""
    return HatSensors(clock=clock)


def flask_available() -> bool:
    """Web の部品（flask）を読み込めるか。読み込みはしない。"""
    try:
        return importlib.util.find_spec("flask") is not None
    except (ImportError, ValueError):
        return False


def _port_usable(port: int) -> bool:
    """ポートを一度開いて閉じる。開けなければ偽。"""
    try:
        server = socket.create_server((LISTEN_HOST, port))
    except OSError:
        return False
    server.close()
    return True


def _config(ctx: CheckContext) -> RemoteConfig | CheckResult:
    """使える設定か、飛ばした結果。設定の誤りは呼ぶ側で分ける。"""
    config = ctx.report.features.get(FEATURE_NAME)
    if not isinstance(config, RemoteConfig):
        return CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)
    if not config.enabled:
        return CheckResult(CheckStatus.SKIPPED, REASON_DISABLED)
    return config


def _check_setup(ctx: CheckContext) -> CheckResult:
    if FEATURE_NAME in ctx.report.feature_errors:
        return CheckResult(CheckStatus.FAILED, REASON_CONFIG_ERROR)
    config = _config(ctx)
    if isinstance(config, CheckResult):
        return config
    if not config.pin_usable():
        return CheckResult(CheckStatus.FAILED, REASON_PIN_UNUSABLE)
    if not flask_available():
        return CheckResult(CheckStatus.FAILED, REASON_NO_FLASK)
    # 動いているときは、ラジコンがポートを使っている。確かめない（1.5）
    if ctx.hardware_allowed and not _port_usable(config.port):
        return CheckResult(CheckStatus.FAILED, REASON_PORT_UNUSABLE)
    return OK


def _readable(reading: SensorReading) -> bool:
    distance = reading.distance_cm
    distance_ok = reading.far or (distance is not None and not math.isnan(distance))
    return distance_ok and all(value is not None for value in reading.floor)


def _check_sensors(ctx: CheckContext) -> CheckResult:
    config = _config(ctx)
    if isinstance(config, CheckResult):
        return config
    if not sensors_available():
        return CheckResult(CheckStatus.SKIPPED, REASON_NO_LIBRARY)
    clock = make_clock()
    sensors = make_sensors(clock)
    try:
        sensors.read()  # 開く。床はまだ読めない
        clock.sleep(FLOOR_WARMUP_S + WARMUP_MARGIN_S)
        reading = sensors.read()
    except Exception:  # 読めないのと同じ。値も中身も出さない
        return CheckResult(CheckStatus.FAILED, REASON_SENSORS_UNREADABLE)
    finally:
        sensors.close()
    if _readable(reading):
        return OK
    return CheckResult(CheckStatus.FAILED, REASON_SENSORS_UNREADABLE)


def _check_usb_camera(ctx: CheckContext) -> CheckResult:
    config = _config(ctx)
    if isinstance(config, CheckResult):
        return config
    if find_usb_camera(config.usb_camera) is not None:
        return OK
    return CheckResult(CheckStatus.FAILED, REASON_NO_CAMERA)


REMOTE_CHECKS: Final[tuple[Check, ...]] = (
    Check(f"{FEATURE_NAME}.setup", _check_setup, timeout_s=SETUP_TIMEOUT_S),
    # ハードの項目。ハードを触れないとき（動いている・--no-hardware）は基盤が飛ばす
    Check(f"{FEATURE_NAME}.sensors", _check_sensors, timeout_s=SENSORS_TIMEOUT_S, hardware=True),
    Check(f"{FEATURE_NAME}.usb_camera", _check_usb_camera, timeout_s=CAMERA_TIMEOUT_S),
)
