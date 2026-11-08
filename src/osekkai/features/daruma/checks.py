"""だるまさんの self-check の項目（7.4、7.5）。

- `daruma.camera`: いま遊べるか。ハードの項目ではない（速く終わる）

設定で無効なとき・設定を使えないとき（ファイルがない・誤りがある）は飛ばす。
誤りは基盤の `core.config` が知らせる。

次に見つけてあいさつの設定を見る（ロボットが動いていなくても見る）。使えない・無効なら
遊べないので、よいに理由を添える。ロボットが動いていないときは飛ばす。このときカメラの
ハードは `presence.camera` が確かめる。ロボットが動いているときは、見つけてあいさつが
開始した機能にあるかを見る。遊べなくてもだめにしない（カメラのハードは
`presence.camera` の役目で、設定の問題は版を戻しても直らない）。

理由はすべて固定の文言にする。呼び名・設定の値を入れない（7.5）。
"""

from __future__ import annotations

from typing import Final

from osekkai.core.config import FeatureConfig
from osekkai.core.selfcheck import Check, CheckContext, CheckResult, CheckStatus
from osekkai.features.daruma.config import DarumaConfig

__all__ = [
    "DARUMA_CHECKS",
    "REASON_CONFIG_UNUSABLE",
    "REASON_DISABLED",
    "REASON_NOT_RUNNING",
    "REASON_PRESENCE_CONFIG_UNUSABLE",
    "REASON_PRESENCE_DISABLED",
    "REASON_PRESENCE_NOT_RUNNING",
]

FEATURE_NAME: Final = "daruma"
PRESENCE_NAME: Final = "presence"

# 固定の理由の文言
REASON_CONFIG_UNUSABLE: Final = "設定を使えない"
REASON_DISABLED: Final = "無効"
REASON_NOT_RUNNING: Final = "ロボットが動いていないので確かめない"
REASON_PRESENCE_CONFIG_UNUSABLE: Final = "見つけてあいさつの設定を使えないので遊べない"
REASON_PRESENCE_DISABLED: Final = "見つけてあいさつが無効なので遊べない"
REASON_PRESENCE_NOT_RUNNING: Final = "見つけてあいさつが動いていないので遊べない"


def _check_camera(ctx: CheckContext) -> CheckResult:
    config = ctx.report.features.get(FEATURE_NAME)
    if not isinstance(config, DarumaConfig):
        return CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)
    if not config.enabled:
        return CheckResult(CheckStatus.SKIPPED, REASON_DISABLED)
    # presence の設定の型は読まない（ほかの機能は api だけ）。基盤の型で見る
    presence = ctx.report.features.get(PRESENCE_NAME)
    if not isinstance(presence, FeatureConfig):
        return CheckResult(CheckStatus.OK, REASON_PRESENCE_CONFIG_UNUSABLE)
    if not presence.enabled:
        return CheckResult(CheckStatus.OK, REASON_PRESENCE_DISABLED)
    if ctx.service is None:
        return CheckResult(CheckStatus.SKIPPED, REASON_NOT_RUNNING)
    if PRESENCE_NAME not in ctx.service.features:
        return CheckResult(CheckStatus.OK, REASON_PRESENCE_NOT_RUNNING)
    return CheckResult(CheckStatus.OK)


DARUMA_CHECKS: Final[tuple[Check, ...]] = (Check(f"{FEATURE_NAME}.camera", _check_camera),)
