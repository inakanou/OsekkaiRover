"""LCD の知らせと、戻し忘れの確認のテスト（5.4、5.5、7.1、7.3）。"""

import json
import logging
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.notice import Notice as CoreNotice
from rover_deploy.common import Running, Target
from rover_deploy.notices import (
    NOTICE_FILE_NAME,
    REMIND_AFTER_H,
    REMINDER_TEXT,
    VERSION_NOTICE_S,
    Notice,
    clear_notice,
    lcd_cells,
    notice_to_dict,
    remind,
    send_reminder,
    version_notice,
    write_notice,
)
from rover_deploy.state import DeployState
from shared_fixtures import load_fixture

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
SAMPLE = Target(kind="sample", ref=None, ref_kind=None, sha=None)
TAG = Target(kind="release", ref="v0.2.0", ref_kind="tag", sha=SHA)
BRANCH = Target(kind="release", ref="feat/x", ref_kind="branch", sha=OTHER_SHA)
NOW = 1793833260.0
HOUR = 3600.0


def read_written(runtime_dir: Path) -> dict[str, object]:
    data = json.loads((runtime_dir / NOTICE_FILE_NAME).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_constants() -> None:
    assert VERSION_NOTICE_S == 20
    assert REMIND_AFTER_H == 3
    assert NOTICE_FILE_NAME == "notice.json"


# --- 版の知らせ ---


def test_version_notice_matches_fixture() -> None:
    notice = version_notice(TAG, SAMPLE, NOW)
    assert notice_to_dict(notice) == load_fixture("notice_version.json")


def test_version_notice_everyday() -> None:
    notice = version_notice(TAG, TAG, NOW)
    assert notice.rows == ("v0.2.0", "ふだんのばん")
    assert notice.until == NOW + VERSION_NOTICE_S


def test_version_notice_sample_is_everyday() -> None:
    notice = version_notice(SAMPLE, SAMPLE, NOW)
    assert notice.rows == ("sample", "ふだんのばん")


@pytest.mark.parametrize(
    ("everyday", "row2"),
    [
        (SAMPLE, "ためし ふだん:さんぷる"),
        (None, "ためし ふだん:なし"),
        (
            Target(kind="release", ref="v0.1.0", ref_kind="tag", sha=OTHER_SHA),
            "ためし ふだん:v0.1.0",
        ),
    ],
)
def test_version_notice_trial_names_everyday(everyday: Target | None, row2: str) -> None:
    notice = version_notice(BRANCH, everyday, NOW)
    assert notice.rows == ("feat/x@fedcba9", row2)


def test_version_notice_long_everyday_name_fits_16_cells() -> None:
    everyday = Target(kind="release", ref="v10.20.30-rc1", ref_kind="tag", sha=OTHER_SHA)
    row2 = version_notice(TAG, everyday, NOW).rows[1]
    assert row2 is not None
    assert row2.startswith("ためし ふだん:v10")
    assert len(to_lcd_codes(row2)) <= 16
    assert lcd_cells(row2) == len(to_lcd_codes(row2))


def test_version_notice_row1_long_branch_keeps_short_sha() -> None:
    """長いブランチの名前は名前のほうを縮め、コミットの識別子を残す（実機確認で決めた）。"""
    target = Target(kind="release", ref="feature/very-long-name", ref_kind="branch", sha=SHA)
    row1 = version_notice(target, SAMPLE, NOW).rows[0]
    assert row1 == f"feature/@{SHA[:7]}"
    assert len(to_lcd_codes(row1)) == 16


def test_version_notice_row1_long_tag_cut_to_16() -> None:
    target = Target(kind="release", ref="v10.20.30-rc1-extra", ref_kind="tag", sha=SHA)
    row1 = version_notice(target, SAMPLE, NOW).rows[0]
    assert row1 == "v10.20.30-rc1-ex"


def test_version_notice_row1_branch_that_fits_is_kept() -> None:
    target = Target(kind="release", ref="feat/abcdefg", ref_kind="branch", sha=SHA)
    assert version_notice(target, SAMPLE, NOW).rows[0] == f"feat/abc@{SHA[:7]}"
    target = Target(kind="release", ref="feat/abc", ref_kind="branch", sha=SHA)  # ちょうど 16
    assert version_notice(target, SAMPLE, NOW).rows[0] == f"feat/abc@{SHA[:7]}"


def test_version_notice_row1_short_label_kept() -> None:
    assert version_notice(TAG, SAMPLE, NOW).rows[0] == "v0.2.0"


@pytest.mark.parametrize(
    "text", ["ふだんのばん", "ためし ふだん:さんぷる", "ためし ふだん:なし", REMINDER_TEXT]
)
def test_fixed_rows_fit_16_cells(text: str) -> None:
    assert lcd_cells(text) == len(to_lcd_codes(text))
    assert len(to_lcd_codes(text)) <= 16


def test_reminder_text() -> None:
    assert REMINDER_TEXT == "ためしのばん もどしてね"


# --- 書く・消す ---


def test_write_notice_is_read_by_core(tmp_path: Path) -> None:
    runtime = tmp_path / "run" / "osekkai"
    write_notice(runtime, version_notice(TAG, SAMPLE, NOW))
    data = read_written(runtime)
    assert data == load_fixture("notice_version.json")
    core = CoreNotice.from_dict(data)
    assert core.rows == ("v0.2.0", "ためし ふだん:さんぷる")
    assert core.until == NOW + VERSION_NOTICE_S


def test_write_notice_permissions(tmp_path: Path) -> None:
    runtime = tmp_path / "run" / "osekkai"
    write_notice(runtime, version_notice(TAG, SAMPLE, NOW))
    assert stat.S_IMODE(os.stat(runtime).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(runtime / NOTICE_FILE_NAME).st_mode) == 0o600
    # 一時ファイルが残らない
    assert [p.name for p in runtime.iterdir()] == [NOTICE_FILE_NAME]


def test_write_notice_replaces(tmp_path: Path) -> None:
    write_notice(tmp_path, version_notice(TAG, SAMPLE, NOW))
    write_notice(tmp_path, Notice(rows=(None, REMINDER_TEXT), until=None))
    assert read_written(tmp_path) == load_fixture("notice_reminder.json")


def test_clear_notice(tmp_path: Path) -> None:
    write_notice(tmp_path, version_notice(TAG, SAMPLE, NOW))
    clear_notice(tmp_path)
    assert not (tmp_path / NOTICE_FILE_NAME).exists()
    clear_notice(tmp_path)  # なくてもよい
    clear_notice(tmp_path / "missing")  # ディレクトリがなくてもよい


# --- 戻し忘れ ---


def trial_state(since: float = NOW - 3 * HOUR) -> DeployState:
    return DeployState(everyday=SAMPLE, current=BRANCH, trial_since=since)


def test_remind_at_exactly_3_hours() -> None:
    state = trial_state(since=NOW - 3 * HOUR)
    assert remind(state, Running.CORE, NOW) is True


def test_remind_not_before_3_hours() -> None:
    state = trial_state(since=NOW - 3 * HOUR + 1)
    assert remind(state, Running.CORE, NOW) is False


def test_remind_long_after() -> None:
    assert remind(trial_state(since=NOW - 30 * HOUR), Running.CORE, NOW) is True


@pytest.mark.parametrize("running", [Running.NONE, Running.SAMPLE, Running.BOTH])
def test_remind_only_while_core_runs(running: Running) -> None:
    assert remind(trial_state(), running, NOW) is False


def test_remind_not_when_current_is_everyday() -> None:
    state = DeployState(everyday=TAG, current=TAG, trial_since=NOW - 10 * HOUR)
    assert remind(state, Running.CORE, NOW) is False


def test_remind_not_without_trial_since_or_current() -> None:
    assert remind(replace(trial_state(), trial_since=None), Running.CORE, NOW) is False
    assert remind(replace(trial_state(), current=None), Running.CORE, NOW) is False


def test_remind_trial_when_everyday_unset() -> None:
    state = replace(trial_state(), everyday=None)
    assert remind(state, Running.CORE, NOW) is True


def test_send_reminder_writes_notice_logs_and_records(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = tmp_path / "run" / "osekkai"
    state = trial_state()
    with caplog.at_level(logging.WARNING, logger="rover_deploy.notices"):
        updated = send_reminder(runtime, state, NOW)
    data = read_written(runtime)
    assert data == load_fixture("notice_reminder.json")
    assert CoreNotice.from_dict(data).until is None
    assert updated == replace(state, reminded_at=NOW)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    # 版の名前は出さない
    assert "feat" not in message
    assert "fedcba9" not in message
