"""だるまさんの設定（`daruma:` の節）の形と検証（3.4、7.1、7.3）。

範囲の外の値は pydantic の誤りになり、基盤が場所（キーのパス）と理由だけを出して開始しない。
呼び名は、ログ・self-check・例外の文に出さない（7.5）。
"""

from __future__ import annotations

from pydantic import Field

from osekkai.core.config import FeatureConfig


class DarumaConfig(FeatureConfig):
    """だるまさんの設定（`daruma:` の節）。`enabled` は基盤の項目（7.2）。"""

    call_name: str = ""  # 空なら「きみ」（3.4）
    threshold: float = Field(default=0.01, gt=0.0, lt=1.0)  # これより大きいと動いた（2.6）
    watch_seconds: float = Field(default=2.5, ge=1.0, le=10.0)  # 見る長さ（2.5）
    max_rounds: int = Field(default=15, ge=1, le=50)  # 最大の回数（4.1）
    nobody_rounds: int = Field(default=5, ge=1, le=50)  # 続けて止まっていたら終える回数（4.2）
    end_before_task_min: int = Field(default=10, ge=0, le=120)  # 次のタスクの何分前（1.5、4.3）
