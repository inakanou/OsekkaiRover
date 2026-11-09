"""ラジコンの設定（`remote:` の節）の形と検証（1.3、1.4、4.5、5.7、6.2、9.6、11.5、13.1、13.2）。

範囲の外の値は pydantic の誤りになり、基盤が場所（キーのパス）と理由だけを出して開始しない。
誤りの文言には設定の値を入れない。pin は `SecretStr` で持ち、表示・例外・ログに値を出さない。
使えない pin は設定の誤りにしない。`pin_usable` で判定し、待ち受けないだけにする（1.4）。
時刻の書き方と、始めと終わりが同じ時間帯の誤りは、おしゃべりの設定と同じ決まり。
機能どうしは内部を読み込まないので、共通の補助は作らず、同じ規則をここに書く。
"""

from __future__ import annotations

from datetime import time
from typing import Annotated, Final

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
)

from osekkai.core.config import FeatureConfig

_MINUTES_PER_DAY = 24 * 60
# YAML の 60 進の数の H:MM は、先頭が 1〜9 なので 60（1:00）より小さくならない
_MIN_BASE60 = 60
_PIN_MIN: Final = 4
_PIN_MAX: Final = 8
_ASCII_DIGITS: Final = frozenset("0123456789")
_DEVICE_PREFIX: Final = "/dev/"

MSG_TIME_FORMAT = '時刻は "HH:MM" のように書いてください'
MSG_TIME_ZONE = "時刻にタイムゾーンは書けません"
MSG_TIME_SECONDS = "時刻に秒は書けません"
MSG_HOURS_EMPTY = "時間帯の始めと終わりが同じです（止めるときは enabled: false にしてください）"
MSG_SPEED_ORDER = "speed_fast は speed_slow 以上にしてください"
MSG_USB_CAMERA = "USB カメラの指定は空にするか、/dev/ で始めてください"


def _time_before(value: object) -> object:
    """YAML が 60 進の数として読んだ時刻（例: 19:30 → 1170）を、時と分に戻す。

    60 より小さい数（例: 7）は、時刻の書き方の誤りにする。
    """
    if isinstance(value, bool):
        raise ValueError(MSG_TIME_FORMAT)
    if isinstance(value, int):
        if _MIN_BASE60 <= value < _MINUTES_PER_DAY:
            return time(value // 60, value % 60)
        raise ValueError(MSG_TIME_FORMAT)
    if isinstance(value, float):
        raise ValueError(MSG_TIME_FORMAT)
    return value


def _time_after(value: time) -> time:
    if value.tzinfo is not None:
        raise ValueError(MSG_TIME_ZONE)
    if value.second or value.microsecond:
        raise ValueError(MSG_TIME_SECONDS)
    return value


def _pin_before(value: object) -> object:
    """文字でない pin は、空の pin にする（設定の誤りにしない。1.4）。

    引用符のない数は、YAML が先頭の 0 を消したり 8 進の数として読んだりするので使わない。
    """
    if isinstance(value, str | SecretStr):
        return value
    return ""


_Time = Annotated[time, BeforeValidator(_time_before), AfterValidator(_time_after)]
_Pin = Annotated[SecretStr, BeforeValidator(_pin_before)]


class RemoteConfig(FeatureConfig):
    """ラジコンの設定（`remote:` の節）。`enabled` は既定で無効（13.1）。"""

    enabled: bool = False
    pin: _Pin = SecretStr("")  # 値の検証はしない（pin_usable で判定する。1.4）
    port: int = Field(default=8080, ge=1024, le=65535)  # 1.3
    # 始めを含み、終わりを含まない。始めが終わりより後なら、日をまたぐ帯（11.5）
    hours: tuple[_Time, _Time] = (time(7, 0), time(20, 0))
    speed_slow: int = Field(default=20, ge=1, le=100)  # ゆっくり（4.5）
    speed_fast: int = Field(default=35, ge=1, le=100)  # はやい。ゆっくり以上（4.5）
    stop_cm: float = Field(default=20.0, ge=5.0, le=100.0)  # これより近い物は危険
    cliff_stop: bool = True  # 床の段差の判定（5.7）
    cliff_reference: int = Field(default=500, ge=0, le=4095)  # 値がこれ以下なら段差
    back_max_seconds: float = Field(default=2.0, ge=0.5, le=10.0)  # 後退を続けられる長さ（6.2）
    photo_days: int = Field(default=30, ge=1, le=365)  # 写真を残す日数（9.6）
    usb_camera: str = ""  # 空なら C270 を自動で探す（13.1）

    @field_validator("hours")
    @classmethod
    def _check_hours(cls, value: tuple[time, time]) -> tuple[time, time]:
        start, end = value
        if start == end:
            raise ValueError(MSG_HOURS_EMPTY)
        return value

    @field_validator("speed_fast")
    @classmethod
    def _check_speed_order(cls, value: int, info: ValidationInfo) -> int:
        slow = info.data.get("speed_slow")  # ゆっくりが誤りなら、そちらだけを出す
        if isinstance(slow, int) and value < slow:
            raise ValueError(MSG_SPEED_ORDER)
        return value

    @field_validator("usb_camera")
    @classmethod
    def _check_usb_camera(cls, value: str) -> str:
        if value and not value.startswith(_DEVICE_PREFIX):
            raise ValueError(MSG_USB_CAMERA)
        return value

    def pin_usable(self) -> bool:
        """pin が 4〜8 けたの ASCII の数字だけか（1.4）。値は返さない。"""
        pin = self.pin.get_secret_value()
        return _PIN_MIN <= len(pin) <= _PIN_MAX and set(pin) <= _ASCII_DIGITS
