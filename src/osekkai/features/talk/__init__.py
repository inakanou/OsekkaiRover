"""朝と夜のお話（talk）。寝る前と朝に、天気と子どもの好きなものの豆知識を短く話す。

基盤はエントリーポイント（`pyproject.toml` の `osekkai.features`）から `TalkFeature` を見つける。
"""

from osekkai.features.talk.feature import TalkFeature

__all__ = ["TalkFeature"]
