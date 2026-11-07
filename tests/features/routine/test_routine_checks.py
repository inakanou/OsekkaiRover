"""日課の self-check の項目のテスト（2.6、8.5、11.3）。基盤の SelfCheckRunner で動かす。"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from routine_fixtures import ROUTINE_FEATURE, task_dict

from osekkai.core.config import ConfigLoader, ConfigReport
from osekkai.core.paths import Paths
from osekkai.core.selfcheck import CheckContext, CheckResult, CheckStatus, SelfCheckRunner
from osekkai.features.routine.checks import (
    REASON_LEFTOVER,
    REASON_NO_CONFIG,
    REASON_NO_HOLIDAYS,
    REASON_NOT_WRITABLE,
    REASON_QUIET_OVERLAP,
    REASON_UNREADABLE,
    ROUTINE_CHECKS,
)
from osekkai.features.routine.config import RoutineConfig

NAMES = ["routine.holidays", "routine.quiet_hours", "routine.records"]
SLEEP_ID = "bedtime_hidden"

pytestmark = pytest.mark.usefixtures("xdg_dirs")


def _report(paths: Paths, *, hours: list[str] | None = None, routine: Any = None) -> ConfigReport:
    data: dict[str, Any] = {}
    if hours is not None:
        data["core"] = {"startup_voice_hours": hours}
    if routine is not None:
        data[ROUTINE_FEATURE] = routine
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return ConfigLoader(paths, {ROUTINE_FEATURE: RoutineConfig}).load()


def _ctx(paths: Paths, report: ConfigReport | None = None) -> CheckContext:
    return CheckContext(
        paths=paths,
        report=report if report is not None else _report(paths),
        service=None,
        hardware_allowed=False,  # 日課の項目はハードに触れないので、それでも動く
    )


def _run(name: str, ctx: CheckContext) -> CheckResult:
    check = next(c for c in ROUTINE_CHECKS if c.name == name)
    [(got, result)] = SelfCheckRunner().run([check], ctx)
    assert got == name
    return result


# 版を戻しても直らないことは、よいに理由を添えて知らせる（配置の道具が版を戻さない）
_NOTICE_ONLY = {REASON_QUIET_OVERLAP, REASON_UNREADABLE, REASON_LEFTOVER}


def _assert_fixed(result: CheckResult, reason: str) -> None:
    status = CheckStatus.OK if reason in _NOTICE_ONLY else CheckStatus.FAILED
    assert result == CheckResult(status, reason)
    # 文言に設定の値（時刻・id）やパスを入れない
    assert re.search(r"[0-9/:]", result.reason) is None
    assert SLEEP_ID not in result.reason


def _sleep(at: str, **fields: Any) -> dict[str, Any]:
    return task_dict(SLEEP_ID, at=at, sleep=True, **fields)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _dirs(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_dir()}


# --- 一覧 -----------------------------------------------------------------


def test_項目は3つでハードに触れない() -> None:
    assert [c.name for c in ROUTINE_CHECKS] == NAMES
    assert all(not c.hardware and c.device is None for c in ROUTINE_CHECKS)
    assert isinstance(ROUTINE_CHECKS, tuple)


def test_ふつうの設定と空の保存先では全部よい() -> None:
    paths = Paths()
    report = _report(paths, routine={"tasks": [task_dict(), _sleep("21:00")]})
    results = SelfCheckRunner().run(ROUTINE_CHECKS, _ctx(paths, report))
    assert results == [(name, CheckResult(CheckStatus.OK)) for name in NAMES]


# --- routine.holidays ------------------------------------------------------


def test_祝日を判定できればよい() -> None:
    assert _run("routine.holidays", _ctx(Paths())) == CheckResult(CheckStatus.OK)


def test_祝日のライブラリを読み込めなければだめ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "jpholiday", None)
    _assert_fixed(_run("routine.holidays", _ctx(Paths())), REASON_NO_HOLIDAYS)


def test_元日を祝日と判定できなければだめ(monkeypatch: pytest.MonkeyPatch) -> None:
    import jpholiday

    monkeypatch.setattr(jpholiday, "is_holiday", lambda _day: False)
    _assert_fixed(_run("routine.holidays", _ctx(Paths())), REASON_NO_HOLIDAYS)


# --- routine.quiet_hours ---------------------------------------------------


def _quiet(hours: list[str] | None, routine: Any) -> CheckResult:
    paths = Paths()
    return _run("routine.quiet_hours", _ctx(paths, _report(paths, hours=hours, routine=routine)))


@pytest.mark.parametrize(
    ("hours", "tasks", "wake"),
    [
        (None, [task_dict(), _sleep("21:00")], None),  # 既定の 8〜20 時
        (None, [task_dict(), _sleep("20:00")], None),  # 終わりと寝る時刻が同じ
        (None, [task_dict(), _sleep("21:00")], {"holiday": "08:00"}),  # 始まりと起床が同じ
        (["05:00", "23:30"], [task_dict(at="06:00")], None),  # 寝るタスクがない
        (["08:00", "08:00"], [task_dict(), _sleep("19:00")], None),  # 空の時間帯
        (["09:00", "18:00"], [task_dict(at="08:30"), _sleep("19:00")], None),
    ],
)
def test_重ならなければよい(hours: list[str] | None, tasks: Any, wake: Any) -> None:
    routine: dict[str, Any] = {"tasks": tasks}
    if wake is not None:
        routine["wake"] = wake
    assert _quiet(hours, routine) == CheckResult(CheckStatus.OK)


@pytest.mark.parametrize(
    ("hours", "tasks", "wake"),
    [
        (None, [task_dict(), _sleep("19:30")], None),  # 終わりが寝る時刻より後
        (None, [task_dict(), _sleep("19:00", days="weekday")], None),  # 平日だけの寝る時刻
        (None, [task_dict(), _sleep("21:00")], {"holiday": "08:30"}),  # 始まりが休日の起床より前
        (["06:00", "20:00"], [task_dict(), _sleep("21:00")], None),  # 始まりが起床より前
        (["22:00", "06:00"], [task_dict(), _sleep("23:00")], None),  # 日をまたぐ時間帯
    ],
)
def test_重なればだめ(hours: list[str] | None, tasks: Any, wake: Any) -> None:
    routine: dict[str, Any] = {"tasks": tasks}
    if wake is not None:
        routine["wake"] = wake
    _assert_fixed(_quiet(hours, routine), REASON_QUIET_OVERLAP)


def test_日課の設定が誤りなら飛ばす() -> None:
    # 寝るタスクが起床より前の誤り。誤りは基盤の core.config が知らせる
    result = _quiet(None, {"tasks": [task_dict(at="07:00"), _sleep("06:00")]})
    assert result == CheckResult(CheckStatus.SKIPPED, REASON_NO_CONFIG)


def test_日課の設定がなくても寝るタスクがないのでよい() -> None:
    assert _quiet(None, None) == CheckResult(CheckStatus.OK)


# --- routine.records -------------------------------------------------------


def _records_ctx() -> tuple[CheckContext, Path]:
    paths = Paths()
    ctx = _ctx(paths)
    return ctx, paths.state_dir_for(ROUTINE_FEATURE)


def _write(directory: Path, name: str, data: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(data)


def test_保存先がなくてもよく作らない() -> None:
    ctx, directory = _records_ctx()
    before = _dirs(ctx.paths.state_dir)
    assert _run("routine.records", ctx) == CheckResult(CheckStatus.OK)
    assert not directory.exists()
    assert _dirs(ctx.paths.state_dir) == before


def test_正しい記録はよい() -> None:
    ctx, directory = _records_ctx()
    _write(directory, "records.json", json.dumps({"version": 1, "stars": 3, "days": {}}).encode())
    _write(directory, "stars.json", json.dumps({"version": 1, "stars": 3}).encode())
    before = _snapshot(directory)
    assert _run("routine.records", ctx) == CheckResult(CheckStatus.OK)
    assert _snapshot(directory) == before


@pytest.mark.parametrize(
    "data",
    [
        b"{not json",
        b"\xff\xfe",
        b"[1, 2]",
        json.dumps({"version": 2, "days": {}}).encode(),
        json.dumps({"days": {}}).encode(),
    ],
)
def test_読めない記録はだめで動かさない(data: bytes, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    ctx, directory = _records_ctx()
    _write(directory, "records.json", data)
    before = _snapshot(directory)
    _assert_fixed(_run("routine.records", ctx), REASON_UNREADABLE)
    assert _snapshot(directory) == before  # 退避しない
    assert str(directory) not in caplog.text


@pytest.mark.parametrize(
    "name",
    [
        "records.corrupt-20260406T060000.json",
        "stars.corrupt-20260406T060000-1.json",
        "records.other-version-20260406T060000.json",
    ],
)
def test_壊れた記録が残っていればだめ(name: str) -> None:
    ctx, directory = _records_ctx()
    _write(directory, "records.json", json.dumps({"version": 1, "days": {}}).encode())
    _write(directory, name, b"{}")
    before = _snapshot(directory)
    _assert_fixed(_run("routine.records", ctx), REASON_LEFTOVER)
    assert _snapshot(directory) == before


needs_non_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root は書き込みの権限を無視する"
)


@needs_non_root
def test_保存先に書けなければだめ() -> None:
    ctx, directory = _records_ctx()
    _write(directory, "records.json", json.dumps({"version": 1, "days": {}}).encode())
    directory.chmod(0o500)
    try:
        before = _snapshot(directory)
        _assert_fixed(_run("routine.records", ctx), REASON_NOT_WRITABLE)
        assert _snapshot(directory) == before
    finally:
        directory.chmod(0o700)


@needs_non_root
def test_保存先がなく親に書けなければだめ() -> None:
    ctx, directory = _records_ctx()
    parent = ctx.paths.state_dir
    parent.mkdir(parents=True, exist_ok=True)
    parent.chmod(0o500)
    try:
        _assert_fixed(_run("routine.records", ctx), REASON_NOT_WRITABLE)
        assert not directory.exists()
    finally:
        parent.chmod(0o700)


def test_知らせるだけの理由は_self_check_全体をだめにしない() -> None:
    # 配置の道具は、だめの項目が 1 つでもあると版を戻す。知らせるだけの理由は戻さない
    from osekkai.core.selfcheck import exit_code

    for reason in sorted(_NOTICE_ONLY):
        assert exit_code([("routine.records", CheckResult(CheckStatus.OK, reason))]) == 0
    assert (
        exit_code([("routine.records", CheckResult(CheckStatus.FAILED, REASON_NOT_WRITABLE))]) == 1
    )
