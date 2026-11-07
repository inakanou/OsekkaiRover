"""音声の入口。ボタンを押して聞き取った言葉を、登録した機能に振り分ける。

`VoiceGatewayFeature` は使うときに読み込む（言葉を登録するだけの機能が `osekkai.voice.api`
を読むときに、聞き取りのアダプタまで読み込まないため）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from osekkai.voice.feature import VoiceGatewayFeature

__all__ = ["VoiceGatewayFeature"]


def __getattr__(name: str) -> Any:
    if name == "VoiceGatewayFeature":
        from osekkai.voice.feature import VoiceGatewayFeature

        return VoiceGatewayFeature
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
