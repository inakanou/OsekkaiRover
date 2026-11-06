"""基盤と道具が共有するファイルの形の見本（tests/fixtures/）を確かめる。

この段階では、見本が JSON として読め、設計の Data Models のキーと型を持つことだけを見る。
"""

from typing import Any

import pytest

from shared_fixtures import (
    FIXTURES_DIR,
    NOTICE_FIXTURES,
    SELFCHECK_FIXTURES,
    STATUS_FIXTURES,
    load_fixture,
    version_log_prefix,
)

STATUS_KEYS = {
    "schema",
    "pid",
    "version",
    "heartbeat",
    "driving",
    "busy",
    "unavailable",
    "features",
}
RESOURCE_VALUES = {"voice", "display", "led", "button", "neck", "drive"}
CHECK_STATUSES = {"ok", "failed", "skipped"}


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def test_fixture_files_exist() -> None:
    names = [*STATUS_FIXTURES, *NOTICE_FIXTURES, *SELFCHECK_FIXTURES, "version_log.json"]
    for name in names:
        assert (FIXTURES_DIR / name).is_file(), name


def test_load_fixture_returns_dict() -> None:
    assert isinstance(load_fixture("status_driving_busy.json"), dict)


@pytest.mark.parametrize("name", STATUS_FIXTURES)
def test_status_fixture_has_service_snapshot_shape(name: str) -> None:
    data = load_fixture(name)
    assert set(data) == STATUS_KEYS
    assert data["schema"] == 1
    assert isinstance(data["pid"], int) and not isinstance(data["pid"], bool)
    assert isinstance(data["version"], str) and data["version"]
    assert _is_number(data["heartbeat"])
    assert isinstance(data["driving"], bool)
    busy = data["busy"]
    assert isinstance(busy, dict)
    for resource, owner in busy.items():
        assert resource in RESOURCE_VALUES
        assert isinstance(owner, str) and owner
        # 基盤の内部の持ち主は作業中に含めない
        assert not owner.startswith("core.")
    assert _is_str_list(data["unavailable"])
    assert _is_str_list(data["features"])


def test_status_fixtures_cover_driving_busy_and_idle() -> None:
    active = load_fixture("status_driving_busy.json")
    assert active["driving"] is True
    assert active["busy"]
    assert active["unavailable"]
    idle = load_fixture("status_idle.json")
    assert idle["driving"] is False
    assert idle["busy"] == {}


@pytest.mark.parametrize("name", NOTICE_FIXTURES)
def test_notice_fixture_has_notice_shape(name: str) -> None:
    data = load_fixture(name)
    assert set(data) == {"schema", "rows", "until"}
    assert data["schema"] == 1
    rows = data["rows"]
    assert isinstance(rows, list) and len(rows) == 2
    assert all(row is None or isinstance(row, str) for row in rows)
    assert data["until"] is None or _is_number(data["until"])


def test_notice_fixtures_cover_timed_and_untimed() -> None:
    version = load_fixture("notice_version.json")
    assert _is_number(version["until"])
    reminder = load_fixture("notice_reminder.json")
    assert reminder["until"] is None
    assert reminder["rows"][0] is None


@pytest.mark.parametrize("name", SELFCHECK_FIXTURES)
def test_selfcheck_fixture_has_result_shape(name: str) -> None:
    data = load_fixture(name)
    assert set(data) == {"schema", "ok", "results"}
    assert data["schema"] == 1
    assert isinstance(data["ok"], bool)
    results = data["results"]
    assert isinstance(results, list) and results
    for result in results:
        assert set(result) == {"name", "status", "reason"}
        assert isinstance(result["name"], str) and result["name"]
        assert result["status"] in CHECK_STATUSES
        assert isinstance(result["reason"], str)
    # だめが 1 つでもあれば ok は False
    failed = any(result["status"] == "failed" for result in results)
    assert data["ok"] is (not failed)


def test_version_log_fixture_has_prefix_and_example() -> None:
    data = load_fixture("version_log.json")
    assert set(data) == {"prefix", "example"}
    assert data["prefix"] == "版: "
    assert data["example"].startswith(data["prefix"])
    assert version_log_prefix() == "版: "
