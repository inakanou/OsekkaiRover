"""基盤と道具が共有するファイルの形の見本（tests/fixtures/）を読む。

基盤のテスト（tests/core）と道具のテスト（tests/deploy）の両方から
`from shared_fixtures import load_fixture` で読む。道具は osekkai を読み込まないので、
ここも標準ライブラリだけを使う。
"""

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# 状態のファイル（status.json）の見本。ServiceSnapshot を JSON にしたもの
STATUS_FIXTURES = ("status_driving_busy.json", "status_idle.json")
# 知らせ（notice.json）の見本。版の知らせと、戻し忘れの知らせ
NOTICE_FIXTURES = ("notice_version.json", "notice_reminder.json")
# self-check の JSON の見本
SELFCHECK_FIXTURES = ("selfcheck_failed.json", "selfcheck_ok.json")


def load_fixture(name: str) -> dict[str, Any]:
    """見本を JSON として読む。形が dict でなければ止める。"""
    data = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"見本の形が違います: {name}")
    return data


def version_log_prefix() -> str:
    """版のログの文言の頭（基盤と道具の両方がこれと同じ文言を使う）。"""
    prefix = load_fixture("version_log.json")["prefix"]
    if not isinstance(prefix, str):
        raise ValueError("版のログの見本の形が違います")
    return prefix
