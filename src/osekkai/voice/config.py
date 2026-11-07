"""音声の入口の設定（`voice:` の節）の形と検証（1.3、5.1、8.1〜8.3）。

誤りの文言には、場所（キーのパス）と理由だけを入れる。設定の値は入れない。
時刻の書き方は日課の設定と同じ決まり。共通の補助は作らず、同じ規則をここに書く。
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import AfterValidator, BeforeValidator, Field, field_validator

from osekkai.core.config import FeatureConfig

if TYPE_CHECKING:
    from pathlib import Path

    from osekkai.core.paths import Paths

RecognizerKind = Literal["auto", "whisper", "typed"]
ModelSize = Literal["base", "small"]

MODELS_DIR_NAME: Final = "models"
MODEL_DIR_PREFIX: Final = "faster-whisper-"

_MINUTES_PER_DAY = 24 * 60
# YAML の 60 進の数の H:MM は、先頭が 1〜9 なので 60（1:00）より小さくならない
_MIN_BASE60 = 60

MSG_TIME_FORMAT = '時刻は "HH:MM" のように書いてください'
MSG_TIME_ZONE = "時刻にタイムゾーンは書けません"
MSG_TIME_SECONDS = "時刻に秒は書けません"
MSG_HOURS_EMPTY = "時間帯の始めと終わりが同じです（止めるときは enabled: false にしてください）"


def _time_before(value: object) -> object:
    """YAML が 60 進の数として読んだ時刻（例: 21:30 → 1290）を、時と分に戻す。

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


_Time = Annotated[time, BeforeValidator(_time_before), AfterValidator(_time_after)]


class VoiceConfig(FeatureConfig):
    """音声の入口の設定（`voice:` の節）。"""

    mic_device: str = "plughw:CARD=U0x46d0x825,DEV=0"  # arecord -L の名前
    listen_seconds: float = Field(default=5.0, ge=2.0, le=15.0)
    model: ModelSize = "base"
    # 始めを含み、終わりを含まない。始めが終わりより後なら、日をまたぐ帯
    hours: tuple[_Time, _Time] = (time(7, 0), time(21, 30))
    follow_up_seconds: float = Field(default=30.0, ge=5.0, le=120.0)
    recognizer: RecognizerKind = "auto"
    max_gain: float = Field(default=20.0, ge=1.0, le=50.0)  # 小さい声を持ち上げる上限

    @field_validator("hours")
    @classmethod
    def _check_hours(cls, value: tuple[time, time]) -> tuple[time, time]:
        start, end = value
        if start == end:
            raise ValueError(MSG_HOURS_EMPTY)
        return value


def model_dir(paths: Paths, config: VoiceConfig) -> Path:
    """聞き取りのモデルの置き場所（`<データの置き場所>/models/faster-whisper-<大きさ>`）。

    機能（聞き取りを選ぶとき）と self-check の両方が使う。
    """
    return paths.data_dir / MODELS_DIR_NAME / f"{MODEL_DIR_PREFIX}{config.model}"
