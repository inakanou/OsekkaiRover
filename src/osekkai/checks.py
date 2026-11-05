"""基盤の確認の項目（device-deployment 6.1、6.5、6.6）。

設定・保存先・時刻・声の合成・再生先・LCD・HAT・ボタン・サーボを確かめる。
ハードは軽い確かめ（`Probes`）だけで触る。モーターを回さず、首を動かさず、音を鳴らさない。
理由はすべて固定の文言にする。設定の値・アドレス・再生先の名前・文章を入れない。

`__main__` と同じ層にある。実機の部品の決まった値（HAT のアドレス、ボタンの線）は、
使うときに `osekkai.adapters.pi` から読み込む（ハードのライブラリは読み込まない）。
"""

from __future__ import annotations

import os
import tempfile
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from pydantic import ValidationError

from osekkai.core.config import Calibration
from osekkai.core.paths import ensure_private_dir
from osekkai.core.selfcheck import Check, CheckContext, CheckResult, CheckStatus

if TYPE_CHECKING:
    from osekkai.core.config import VoiceSettings

# 固定の理由の文言
REASON_CONFIG: Final = "設定の誤りがある（{count} 件）"
REASON_NOT_WRITABLE: Final = "書き込めない"
REASON_NOT_SYNCED: Final = "同期していない"
REASON_NO_WAV: Final = "音声ができない"
REASON_NO_SINK: Final = "再生先がない"
REASON_NO_ANSWER: Final = "応答がない"
REASON_PIN_BUSY: Final = "線を取れない"
REASON_CALIBRATION: Final = "調整値が範囲の外"

# 合成は Open JTalk の時間切れ（30 秒）と音量の調整より長く待つ
VOICE_SYNTH_TIMEOUT_S: Final = 40.0
TEMP_PREFIX: Final = ".selfcheck-"

OK: Final = CheckResult(CheckStatus.OK)


class Probes(Protocol):
    """動かさない・鳴らさない、軽い確かめの口。"""

    def i2c_responds(self, address: int) -> bool: ...
    def gpio_input_free(self, pin: int) -> bool: ...
    def audio_sink_exists(self, sink: str) -> bool: ...
    def synthesize(self, settings: VoiceSettings, out: Path) -> None: ...
    def time_synchronized(self) -> bool: ...


def config_reason(count: int) -> str:
    """設定の誤りの理由（件数だけを出す）。"""
    return REASON_CONFIG.format(count=count)


def _failed(reason: str) -> CheckResult:
    return CheckResult(CheckStatus.FAILED, reason)


def _check_config(ctx: CheckContext) -> CheckResult:
    report = ctx.report
    count = len(report.fatal_errors) + sum(len(e) for e in report.feature_errors.values())
    return _failed(config_reason(count)) if count else OK


def _check_storage(ctx: CheckContext) -> CheckResult:
    for directory in (ctx.paths.state_dir, ctx.paths.cache_dir):
        try:
            ensure_private_dir(directory)
            fd, name = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=directory)
            try:
                os.write(fd, b"ok")
            finally:
                os.close(fd)
                os.unlink(name)
        except OSError:
            return _failed(REASON_NOT_WRITABLE)
    return OK


def _has_frames(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as src:
            return src.getnframes() > 0
    except (OSError, EOFError, wave.Error):
        return False


def _hat_responds(probes: Probes) -> bool:
    from osekkai.adapters.pi.hat import ADDRESSES

    return any(probes.i2c_responds(address) for address in ADDRESSES)


def _calibration_in_range(calibration: Calibration) -> bool:
    """設定の型で検証し直す（範囲は `core.config.Calibration` だけで決める）。"""
    try:
        Calibration.model_validate(calibration.model_dump())
    except ValidationError:
        return False
    return True


def base_checks(probes: Probes) -> list[Check]:
    """基盤の確認の項目を、表示の順に並べる。"""

    def timesync(_ctx: CheckContext) -> CheckResult:
        return OK if probes.time_synchronized() else _failed(REASON_NOT_SYNCED)

    def voice_synth(ctx: CheckContext) -> CheckResult:
        # 保存先とは別の一時ディレクトリに作り、終わったら消す。再生はしない
        with tempfile.TemporaryDirectory(prefix="osekkai-selfcheck-") as tmp:
            out = Path(tmp) / "check.wav"
            probes.synthesize(ctx.report.core.voice, out)  # 例外は型の名前だけになる
            return OK if _has_frames(out) else _failed(REASON_NO_WAV)

    def audio_sink(ctx: CheckContext) -> CheckResult:
        sink = ctx.report.core.voice.sink  # 空なら既定の再生先
        return OK if probes.audio_sink_exists(sink) else _failed(REASON_NO_SINK)

    def lcd(ctx: CheckContext) -> CheckResult:
        address = ctx.report.core.hardware.lcd_address
        return OK if probes.i2c_responds(address) else _failed(REASON_NO_ANSWER)

    def hat(_ctx: CheckContext) -> CheckResult:
        return OK if _hat_responds(probes) else _failed(REASON_NO_ANSWER)

    def buttons(ctx: CheckContext) -> CheckResult:
        from osekkai.adapters.pi.gpio import USR_BUTTON_PIN, resolve_pin

        pins = [USR_BUTTON_PIN]
        extra = ctx.report.core.hardware.extra_button_pin
        if extra is not None:
            pins.append(resolve_pin(extra))
        for pin in pins:
            if not probes.gpio_input_free(pin):
                return _failed(REASON_PIN_BUSY)
        return OK

    def servos(ctx: CheckContext) -> CheckResult:
        # 首とステアリングは動かさない。HAT の応答と調整値だけを見る
        if not _hat_responds(probes):
            return _failed(REASON_NO_ANSWER)
        if not _calibration_in_range(ctx.report.core.motion.calibration):
            return _failed(REASON_CALIBRATION)
        return OK

    return [
        Check("core.config", _check_config),
        Check("core.storage", _check_storage),
        Check("core.timesync", timesync),
        Check("core.voice_synth", voice_synth, timeout_s=VOICE_SYNTH_TIMEOUT_S),
        Check("core.audio_sink", audio_sink),
        Check("core.lcd", lcd, hardware=True, device="display"),
        Check("core.hat", hat, hardware=True, device="hat"),
        Check("core.buttons", buttons, hardware=True, device="buttons"),
        Check("core.servos", servos, hardware=True, device="servos"),
    ]
