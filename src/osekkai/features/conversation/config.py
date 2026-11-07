"""おしゃべりの設定（`conversation:` の節）の形と検証（4.1、5.1、5.3、2.4、6.4、9.3、9.5）。

誤りの文言には、場所（キーのパス）と理由だけを入れる。設定の値は入れない。
時刻の書き方と、始めと終わりが同じ時間帯の誤りは、音声の入口の設定と同じ決まり。
機能から入口の内部は読み込めないので、共通の補助は作らず、同じ規則をここに書く。
"""

from __future__ import annotations

from datetime import time
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, Field, field_validator

from osekkai.core.config import FeatureConfig

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


class ConversationConfig(FeatureConfig):
    """おしゃべりの設定（`conversation:` の節）。"""

    call_name: str = ""  # 空なら「きみ」（4.1）。外部に送らない
    # 聞き取りが書く別の形（漢字など）。呼び名と同じく目印に置き換える（4.2）
    call_name_variants: list[str] = Field(default_factory=list)
    # 始めを含み、終わりを含まない。始めが終わりより後なら、日をまたぐ帯（5.1）
    hours: tuple[_Time, _Time] = (time(7, 0), time(21, 30))
    daily_limit: int = Field(default=60, ge=1, le=500)  # 1 日に話す返事の数（5.3）
    max_turns: int = Field(default=10, ge=1, le=30)  # 1 つの会話で覚える往復（2.4）
    reply_timeout_seconds: float = Field(default=20.0, ge=5.0, le=60.0)  # 6.4

    @field_validator("hours")
    @classmethod
    def _check_hours(cls, value: tuple[time, time]) -> tuple[time, time]:
        start, end = value
        if start == end:
            raise ValueError(MSG_HOURS_EMPTY)
        return value
