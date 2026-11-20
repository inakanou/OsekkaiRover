"""朝と夜のお話の設定（`talk:` の節）の形と検証（1.1〜1.4、1.8、5.4〜5.6、9.1、9.2、10.2）。

範囲や書き方の外の値は pydantic の誤りになり、基盤が場所（キーのパス）と理由だけを出して開始しない。
誤りの文言には設定の値（現在地・好きなもの・呼び名）を入れない。
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, ValidationInfo, field_validator

from osekkai.core.config import FeatureConfig

MSG_TOPIC_HAS_NAME = "好きなものに呼び名を入れないでください（呼び名は外へ送らないため）"

_Topic = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]


class Location(BaseModel):
    """現在地。天気の問い合わせには 0.1 度に丸めて使う（9.2）。"""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class WarningArea(BaseModel):
    """注意報と警報の地域。気象庁のコードを ASCII の数字で書く（docs/talk.md）。"""

    office: str = Field(pattern=r"^[0-9]{6}$")  # 府県予報区のコード（例: 130000）
    # 市区町村のコード（area.json の class20s。例: 1311300）
    city: str = Field(pattern=r"^[0-9]{7}$")


class TalkConfig(FeatureConfig):
    """朝と夜のお話の設定（`talk:` の節）。`enabled` は既定で無効（1.2）。"""

    enabled: bool = False
    call_name: str = ""  # 始まりと終わりの言葉の {name}。外へ送らない（9.1）
    night: bool = True  # 夜のお話を使うか（1.3）
    morning: bool = True  # 朝のお話を使うか（1.3）
    location: Location | None = None  # なければ天気の話をしない（1.5）
    warning_area: WarningArea | None = None  # なければ注意報と警報に触れない
    topics: list[_Topic] = Field(default_factory=list, max_length=20)  # 好きなもの（1.6）
    rain_percent: int = Field(default=50, ge=1, le=100)  # 傘を勧める降る確率（5.4）
    cold_below: int = Field(default=10, ge=-20, le=30)  # 上着を勧める低い気温（5.5）
    hot_at_or_above: int = Field(default=30, ge=20, le=45)  # 水を勧める高い気温（5.6）
    # 天気と豆知識の問い合わせの時間切れ（10.2）
    fetch_timeout_seconds: float = Field(default=20.0, ge=5.0, le=60.0)

    @field_validator("topics")
    @classmethod
    def _check_topics_without_name(cls, value: list[str], info: ValidationInfo) -> list[str]:
        """呼び名を含む好きなものは誤りにする（9.1 の守り。誤って書いても外へ出ない）。"""
        name = info.data.get("call_name")
        name = name.strip() if isinstance(name, str) else ""
        if name and any(name in topic for topic in value):
            raise ValueError(MSG_TOPIC_HAS_NAME)
        return value
