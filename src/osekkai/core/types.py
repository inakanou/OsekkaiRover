"""基盤で共通に使う値の型と例外。"""

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Literal, TypeAlias

FeatureId: TypeAlias = str
"""機能の名前（`FeatureSpec.name`）。"""


class Resource(Enum):
    """機能が借りる資源の種類。"""

    VOICE = "voice"
    DISPLAY = "display"
    LED = "led"
    BUTTON = "button"
    NECK = "neck"
    DRIVE = "drive"


class Priority(IntEnum):
    """優先度の帯。数が大きいほど優先する。帯の中では 1〜9 を足してよい。"""

    IDLE = 0
    BACKGROUND = 10
    REMOTE = 30
    INTERACTIVE = 50
    SCHEDULED = 80


class Glyph(IntEnum):
    """LCD の絵の文字（CGRAM のコード 0〜7）。"""

    EYE = 0
    EYE_HAPPY = 1
    EYE_CLOSED = 2
    SMILE = 3
    MOUTH_O = 4
    STAR = 5
    HEART = 6
    ZZZ = 7


class Face(Enum):
    """待機画面などに出す顔の種類。"""

    NORMAL = "normal"
    CALL = "call"
    HAPPY = "happy"
    SLEEP = "sleep"


@dataclass(frozen=True)
class ButtonPress:
    """ボタンの 1 回の押下。`at` は単調な時刻（秒）。"""

    at: float
    source: Literal["usr", "extra"]


class ResourceBusy(Exception):
    """資源が同じか高い優先度の機能に使われていて、借りられない。"""


class LeaseRevoked(Exception):
    """リースが取り上げられた、または中断を通知された後に操作した。"""


class DeviceUnavailable(Exception):
    """そのデバイスが使えない（起動時の準備に失敗した、など）。"""


class MotionAborted(Exception):
    """動きの途中で中止した。"""


class SpeechFailed(Exception):
    """声の合成か再生に失敗した。"""
