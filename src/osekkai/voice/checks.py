"""音声の入口の self-check の項目（6.4、8.4）。

- `voice.mic`: `arecord -L` の一覧に設定のマイクの名前があるか（ハードの項目、5 秒で打ち切り）
- `voice.model`: モデルの置き場所に要るファイルがそろっているか

実際に使う聞き取りが打った文字のとき（設定が typed、または auto で faster_whisper を
読み込めないとき）は、どちらも飛ばす。開発 PC と CI の self-check はこれで赤くならない。
設定で無効なら入口は動かないので、どちらも飛ばす。入口の設定を使えないとき
（設定ファイルがない・誤りがある）も飛ばす。誤りは基盤の `core.config` が知らせる。

理由はすべて固定の文言にする。マイクの名前・パス・モデルの大きさを入れない。
"""

from __future__ import annotations

from typing import Final

from osekkai.core.selfcheck import Check, CheckContext, CheckResult, CheckStatus
from osekkai.voice.adapters.whisper import mic_present, model_files_present, whisper_available
from osekkai.voice.config import VoiceConfig, model_dir

__all__ = [
    "REASON_CONFIG_UNUSABLE",
    "REASON_DISABLED",
    "REASON_NO_MIC",
    "REASON_NO_MODEL",
    "REASON_TYPED",
    "VOICE_CHECKS",
]

FEATURE_NAME: Final = "voice"

# 固定の理由の文言
REASON_NO_MIC: Final = "マイクが見つからない"
REASON_NO_MODEL: Final = "聞き取りのモデルがない"
REASON_TYPED: Final = "打った文字の聞き取り"
REASON_DISABLED: Final = "無効"
REASON_CONFIG_UNUSABLE: Final = "入口の設定を使えない"

# arecord -L は 5 秒で打ち切る。それより少し長く待ち、打ち切りを「マイクが見つからない」にする
MIC_TIMEOUT_S: Final = 7.0
MODEL_TIMEOUT_S: Final = 5.0

OK: Final = CheckResult(CheckStatus.OK)


def _skip_reason(ctx: CheckContext) -> tuple[VoiceConfig | None, str]:
    """確かめるなら（設定, ""）、飛ばすなら（None, 理由）を返す。"""
    config = ctx.report.features.get(FEATURE_NAME)
    if not isinstance(config, VoiceConfig):
        return None, REASON_CONFIG_UNUSABLE
    if not config.enabled:
        return None, REASON_DISABLED
    if config.recognizer == "typed" or (config.recognizer == "auto" and not whisper_available()):
        return None, REASON_TYPED
    return config, ""


def _check_mic(ctx: CheckContext) -> CheckResult:
    config, reason = _skip_reason(ctx)
    if config is None:
        return CheckResult(CheckStatus.SKIPPED, reason)
    if mic_present(config.mic_device):
        return OK
    return CheckResult(CheckStatus.FAILED, REASON_NO_MIC)


def _check_model(ctx: CheckContext) -> CheckResult:
    config, reason = _skip_reason(ctx)
    if config is None:
        return CheckResult(CheckStatus.SKIPPED, reason)
    if model_files_present(model_dir(ctx.paths, config)):
        return OK
    return CheckResult(CheckStatus.FAILED, REASON_NO_MODEL)


VOICE_CHECKS: Final[tuple[Check, ...]] = (
    # マイクはハードの項目。ハードを触れないとき（サービスが動いている・--no-hardware）は
    # サービスの報告にマイクの名前がないので、基盤が飛ばす
    Check(f"{FEATURE_NAME}.mic", _check_mic, timeout_s=MIC_TIMEOUT_S, hardware=True),
    Check(f"{FEATURE_NAME}.model", _check_model, timeout_s=MODEL_TIMEOUT_S),
)
