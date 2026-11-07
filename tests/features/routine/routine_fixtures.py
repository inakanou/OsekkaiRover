"""日課のテストで共通に使う部品の本体（conftest から使う。型も確かめる）。"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.config import ConfigLoader, ConfigReport
from osekkai.core.paths import Paths
from osekkai.core.storage import StateStore
from osekkai.features.routine.config import RoutineConfig

TOKYO = ZoneInfo("Asia/Tokyo")
# 平日（月曜）の朝。日課のテストの始まりの時刻
ROUTINE_START = datetime(2026, 4, 6, 6, 0, tzinfo=TOKYO)
ROUTINE_FEATURE = "routine"


def make_clock(start: datetime = ROUTINE_START) -> FakeClock:
    """日本の時刻（Asia/Tokyo）で始まる偽の時計を作る。"""
    return FakeClock(start)


def make_store(paths: Paths, clock: FakeClock) -> StateStore:
    """日課の機能の保存先を作る。"""
    return StateStore.for_feature(paths, ROUTINE_FEATURE, clock)


# 設定を組み立てる補助。値はテスト用の決まったもので、個人の値は入れない
def task_dict(task_id: str = "wash", **fields: Any) -> dict[str, Any]:
    """タスク 1 つの設定（dict）。既定は時刻 07:00 の毎日のタスク。"""
    data: dict[str, Any] = {
        "id": task_id,
        "at": "07:00",
        "call": "かおを あらおう",
        "praise": "すごいね",
        "lcd": "ｶｵ ｱﾗｳ",
    }
    data.update(fields)
    return {k: v for k, v in data.items() if v is not None}


def routine_section(tasks: list[dict[str, Any]] | None = None, **fields: Any) -> dict[str, Any]:
    """`routine:` の節（dict）。"""
    section: dict[str, Any] = dict(fields)
    if tasks is not None:
        section["tasks"] = tasks
    return section


def routine_yaml(section: dict[str, Any]) -> str:
    """`routine:` の節だけの設定ファイルの中身。"""
    return yaml.safe_dump({ROUTINE_FEATURE: section}, allow_unicode=True, sort_keys=False)


def build_config(tasks: list[dict[str, Any]] | None = None, **fields: Any) -> RoutineConfig:
    """設定のモデルを直接作る（誤りがあれば ValidationError）。"""
    return RoutineConfig.model_validate(routine_section(tasks, **fields))


def load_report(paths: Paths, text: str) -> ConfigReport:
    """設定ファイルを書き、基盤の読み込みに日課のモデルを渡して読む。"""
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")
    return ConfigLoader(paths, {ROUTINE_FEATURE: RoutineConfig}).load()
