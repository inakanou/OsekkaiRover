"""日課の self-check の項目（2.6、8.5、11.3）。

- `routine.holidays`: 祝日を判定できるか（既知の祝日の元日で確かめる）
- `routine.quiet_hours`: 基盤の起動の声の時間帯が、おやすみの時間と重ならないか
- `routine.records`: 記録を読めるか、壊れた記録が残っていないか、保存先に書けるか

どれもハードに触れない。ファイルは読むだけで、書いたり動かしたりしない。
理由はすべて固定の文言にする。設定の値（時刻・id）・パス・記録の中身を入れない。

だめ（FAILED）にするのは、日課が動けないことだけ（祝日を判定できない・記録を書けない）。
配置の道具は、だめの項目が 1 つでもあると版を戻す。版を戻しても直らないこと
（壊れた記録が残っている・記録を読めない・時間帯の重なり）は、よい（OK）に理由を
添えて知らせる（表示に理由が出る）。保護者が設定を直すか、残った記録を消せば消える。
"""

from __future__ import annotations

import json
import os
from datetime import date, time
from pathlib import Path
from typing import Final

from osekkai.core.selfcheck import Check, CheckContext, CheckResult, CheckStatus
from osekkai.features.routine.calendar import JpHolidaySource
from osekkai.features.routine.config import RoutineConfig, WakeKind
from osekkai.features.routine.records import OTHER_VERSION_MARK

FEATURE_NAME: Final = "routine"

# 固定の理由の文言
REASON_NO_HOLIDAYS: Final = "祝日を判定できない"
REASON_QUIET_OVERLAP: Final = "起動の声の時間がおやすみの時間と重なる"
REASON_NO_CONFIG: Final = "日課の設定を使えない"
REASON_UNREADABLE: Final = "記録を読めない"
REASON_LEFTOVER: Final = "壊れた記録が残っている"
REASON_NOT_WRITABLE: Final = "記録を書けない"

KNOWN_HOLIDAY: Final = date(2026, 1, 1)  # 元日
_RECORDS_FILE: Final = "records.json"
_RECORDS_VERSION: Final = 1
# 基盤が退避した壊れた記録と、版の違う記録の名前
_LEFTOVER_PATTERNS: Final = ("*.corrupt-*.json", f"records.{OTHER_VERSION_MARK}-*.json")
_WAKE_KINDS: Final[tuple[WakeKind, ...]] = ("weekday", "holiday")
_DAY_SECONDS: Final = 24 * 60 * 60

OK: Final = CheckResult(CheckStatus.OK)


def _failed(reason: str) -> CheckResult:
    return CheckResult(CheckStatus.FAILED, reason)


def _notice(reason: str) -> CheckResult:
    """版を戻さずに知らせる（よいに理由を添える）。"""
    return CheckResult(CheckStatus.OK, reason)


def _check_holidays(_ctx: CheckContext) -> CheckResult:
    source = JpHolidaySource()
    if source.available and source.is_holiday(KNOWN_HOLIDAY):
        return OK
    return _failed(REASON_NO_HOLIDAYS)


def _seconds(value: time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def _segments(start: time, end: time) -> list[tuple[int, int]]:
    """時間帯を、1 日の中の区間（秒。始まりを含み、終わりを含まない）にする。

    基盤の `within_hours` と同じ読み方: 日をまたぐ帯は 2 つに分け、始まりと終わりが同じなら空。
    """
    s, e = _seconds(start), _seconds(end)
    if s < e:
        return [(s, e)]
    if s > e:
        return [(s, _DAY_SECONDS), (0, e)]
    return []


def _quiet_segments(config: RoutineConfig) -> list[tuple[int, int]]:
    """おやすみの時間（寝る時刻から 0 時まで、0 時から起床時刻まで）の区間。

    寝るタスクがなければ、おやすみの時間はない。種類の日ごとに違うときは、
    最も早い寝る時刻と、最も遅い起床時刻で、いちばん広く見る。
    """
    sleeps = [task.at for task in config.tasks if task.sleep and task.at is not None]
    if not sleeps:
        return []
    segments = [(_seconds(min(sleeps)), _DAY_SECONDS)]
    wakes = [w for w in (config.wake_time(kind) for kind in _WAKE_KINDS) if w is not None]
    if wakes:
        segments.append((0, _seconds(max(wakes))))
    return segments


def _check_quiet_hours(ctx: CheckContext) -> CheckResult:
    config = ctx.report.features.get(FEATURE_NAME)
    if not isinstance(config, RoutineConfig):
        # 設定の誤りは基盤の core.config が知らせる
        return CheckResult(CheckStatus.SKIPPED, REASON_NO_CONFIG)
    voice = _segments(*ctx.report.core.startup_voice_hours)
    for v_start, v_end in voice:
        for q_start, q_end in _quiet_segments(config):
            if v_start < q_end and q_start < v_end:
                return _notice(REASON_QUIET_OVERLAP)
    return OK


def _records_readable(path: Path) -> bool:
    """退避せずに読む。版 1 の対応表なら読める。"""
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("version") == _RECORDS_VERSION


def _has_leftover(directory: Path) -> bool:
    try:
        return any(any(directory.glob(pattern)) for pattern in _LEFTOVER_PATTERNS)
    except OSError:
        return False


def _writable(directory: Path) -> bool:
    """ディレクトリに書けるか。まだなければ、いちばん近くにある親に作れるかを見る。"""
    current = directory
    while not current.exists():
        if current.parent == current:
            return False
        current = current.parent
    return current.is_dir() and os.access(current, os.W_OK | os.X_OK)


def _check_records(ctx: CheckContext) -> CheckResult:
    directory = ctx.paths.state_dir_for(FEATURE_NAME)
    path = directory / _RECORDS_FILE
    # 初めての起動では、ディレクトリも記録もなくてよい
    if not _writable(directory):
        return _failed(REASON_NOT_WRITABLE)
    # 読めない記録は、日課が次に起動したときに退避して控えの星から続ける
    if path.exists() and not _records_readable(path):
        return _notice(REASON_UNREADABLE)
    if directory.is_dir() and _has_leftover(directory):
        return _notice(REASON_LEFTOVER)
    return OK


# 機能の申告（`FeatureSpec.checks`）にそのまま渡す項目の一覧
ROUTINE_CHECKS: Final[tuple[Check, ...]] = (
    Check(f"{FEATURE_NAME}.holidays", _check_holidays),
    Check(f"{FEATURE_NAME}.quiet_hours", _check_quiet_hours),
    Check(f"{FEATURE_NAME}.records", _check_records),
)
