"""いまラジコンを使えるかの判定と、端末への知らせの文（要件 11.1、11.2、11.3、11.5、11.6）。

時刻 → おやすみ → 時間帯 の順に見る。1 日に使える時間の上限は持たない（11.6）。
知らせの表はここに置く。texts はほかの部品を読まないので、texts の文をここで引く。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time
from enum import Enum
from types import MappingProxyType
from typing import Final

from . import texts


class Availability(Enum):
    """端末から見て、いまラジコンを使えるか。"""

    OK = "ok"
    OUT_OF_HOURS = "out_of_hours"  # 11.1
    QUIET = "quiet"  # 11.2
    NO_CLOCK = "no_clock"  # 11.3
    BUSY = "busy"  # 12.2（ほかの機能が使っている）
    OTHER_OPERATOR = "other"  # 3.2（その端末にとって）


# 使えないときの知らせの文。OK は知らせがないので入れない
MESSAGES: Final[Mapping[Availability, str]] = MappingProxyType(
    {
        Availability.OUT_OF_HOURS: texts.MSG_OUT_OF_HOURS,
        Availability.QUIET: texts.MSG_QUIET,
        Availability.NO_CLOCK: texts.MSG_NO_CLOCK,
        Availability.BUSY: texts.MSG_BUSY,
        Availability.OTHER_OPERATOR: texts.MSG_OTHER_OPERATOR,
    }
)


def message_for(value: Availability) -> str | None:
    """端末に出す知らせの文。使えるとき（OK）は None。"""
    return MESSAGES.get(value)


def within_hours(now: time, hours: tuple[time, time]) -> bool:
    """`now` が時間帯の中か。始まりは含み、終わりは含まない。日をまたぐ帯も扱う（11.5）。

    始まりと終わりが同じなら、空の帯とする。
    """
    start, end = hours
    if start < end:
        return start <= now < end
    if start > end:
        return now >= start or now < end
    return False


def judge_availability(
    *, synced: bool, now: datetime | None, quiet: bool, hours: tuple[time, time]
) -> Availability:
    """時刻 → おやすみ → 時間帯 の順に見て、使えるかを決める（11.1〜11.3）。"""
    if not synced or now is None:
        return Availability.NO_CLOCK
    if quiet:
        return Availability.QUIET
    if not within_hours(now.time(), hours):
        return Availability.OUT_OF_HOURS
    return Availability.OK
