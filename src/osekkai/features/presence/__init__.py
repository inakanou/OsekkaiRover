"""見つけてあいさつ（presence）。カメラで顔を見つけて首で追い、覚えた顔にあいさつする。

`PresenceFeature` は使うときに読み込む（カメラを借りるだけの機能が
`osekkai.features.presence.api` を読むときに、機能の本体まで読み込まないため）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from osekkai.features.presence.feature import PresenceFeature

__all__ = ["PresenceFeature"]


def __getattr__(name: str) -> Any:
    if name == "PresenceFeature":
        from osekkai.features.presence.feature import PresenceFeature

        return PresenceFeature
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
