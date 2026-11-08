"""おしゃべり（conversation）。当てはまらない言葉に、子ども向けの短い返事を Claude で返す。

基盤はエントリーポイント（`pyproject.toml` の `osekkai.features`）から
`ConversationFeature` を見つける。
"""

from osekkai.features.conversation.feature import ConversationFeature

__all__ = ["ConversationFeature"]
