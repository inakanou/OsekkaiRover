"""門の判定。要件 5.1、5.2、5.3、5.6、9.2。

外部の会話の AI に送る前に、送ってよいかを確かめる。止めるときは理由を 1 つ返す。
理由の値は `texts.GATE_REPLIES` のキーと同じ文字列にする。
"""

from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum


class GateReason(StrEnum):
    """門で止めた理由。値は `texts.GATE_REPLIES` のキー。"""

    NOT_SET_UP = "not_set_up"  # 鍵がない（9.2）
    CLOCK_UNSYNCED = "clock_unsynced"  # 時刻が確かでない（5.2）
    OUT_OF_HOURS = "out_of_hours"  # 時間帯の外（5.1）
    LIMIT = "limit"  # 上限に達した、または記録を読めない日（5.3、5.6）


def within_hours(now: time, hours: tuple[time, time]) -> bool:
    """`now` が時間帯の中か。始めは含み、終わりは含まない。始めが終わりより後なら日をまたぐ。

    音声の入口の `within_hours` と同じ読み方。始めと終わりが同じ帯は設定で断るので、
    ここでは空の帯とする。
    """
    start, end = hours
    if start < end:
        return start <= now < end
    if start > end:
        return now >= start or now < end
    return False


def check_gate(
    *,
    ready: bool,
    synchronized: bool,
    now: datetime,
    hours: tuple[time, time],
    usage_reached: bool,
) -> GateReason | None:
    """止める理由を 1 つ返す。どれでもなければ None（通す）。

    順は 鍵 → 時刻 → 時間帯 → 上限。時刻が確かでないときは時間帯を見ない。
    """
    if not ready:
        return GateReason.NOT_SET_UP
    if not synchronized:
        return GateReason.CLOCK_UNSYNCED
    if not within_hours(now.time(), hours):
        return GateReason.OUT_OF_HOURS
    if usage_reached:
        return GateReason.LIMIT
    return None
