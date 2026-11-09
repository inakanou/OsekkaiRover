"""ラジコン（remote）。保護者が決めた時間だけ、子どもが端末の画面から車を動かす。

基盤はエントリーポイント（`pyproject.toml` の `osekkai.features`）から機能を見つける。
"""

from osekkai.features.remote.feature import RemoteFeature

__all__ = ["RemoteFeature"]
