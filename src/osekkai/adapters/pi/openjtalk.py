"""Open JTalk で 1 行の文章を WAV にし、音量をそろえる（6.1、6.2）。

open_jtalk は標準入力の最初の 1 行だけを読むので、改行を除いた 1 行を渡す。
例外のメッセージには、文章も stderr も入れない（stderr は文章を写しうるため）。
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import wave
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from osekkai.core.config import VoiceSettings

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]

SYNTH_TIMEOUT: Final = 30.0  # 1 行は 300 文字まで。Pi 5 なら数秒で終わる
PEAK_TARGET: Final = 0.95  # 最大の振幅（割れない範囲で大きく）
FULL_SCALE: Final = 32767  # 16 ビットの PCM
SAMPLE_WIDTH: Final = 2


def peak_gain(peak: int) -> float | None:
    """最大の振幅を PEAK_TARGET にする倍率。無音なら None。"""
    if peak <= 0:
        return None
    return PEAK_TARGET * FULL_SCALE / peak


def _peak_numpy(frames: bytes) -> int:
    import numpy as np

    samples = np.frombuffer(frames, dtype="<i2")
    if samples.size == 0:
        return 0
    return int(np.abs(samples.astype(np.int32)).max())


def _scale_numpy(frames: bytes, gain: float) -> bytes:
    import numpy as np

    samples = np.frombuffer(frames, dtype="<i2").astype(np.float64)
    scaled = np.clip(np.rint(samples * gain), -FULL_SCALE - 1, FULL_SCALE)
    return bytes(scaled.astype("<i2").tobytes())


def normalize_wav(
    path: Path,
    *,
    peak: Callable[[bytes], int] = _peak_numpy,
    scale: Callable[[bytes, float], bytes] = _scale_numpy,
) -> None:
    """16 ビットの WAV を、最大の振幅が 0.95 になるよう書き直す。無音ならそのまま。"""
    with wave.open(str(path), "rb") as src:
        params = src.getparams()
        if params.sampwidth != SAMPLE_WIDTH:
            raise ValueError(f"unsupported sample width: {params.sampwidth}")
        frames = src.readframes(params.nframes)
    gain = peak_gain(peak(frames))
    if gain is None:
        return
    data = scale(frames, gain)
    # 一時のファイルに書いてから置き換える（途中で止まっても壊れた WAV を残さない）
    fd, tmp_name = tempfile.mkstemp(prefix=".norm-", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as raw, wave.open(raw, "wb") as dst:
            dst.setparams(params)
            dst.writeframes(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


class OpenJTalkSynth:
    """SpeechSynthPort の実機の実装。"""

    def __init__(
        self,
        *,
        run: Runner = subprocess.run,
        normalize: Callable[[Path], None] = normalize_wav,
        timeout: float = SYNTH_TIMEOUT,
    ) -> None:
        self._run = run
        self._normalize = normalize
        self._timeout = timeout

    def synthesize(self, line: str, settings: VoiceSettings, out: Path) -> None:
        one_line = "".join(line.splitlines())
        args = [
            settings.open_jtalk,
            "-x",
            str(settings.dictionary.expanduser()),
            "-m",
            str(settings.htsvoice.expanduser()),
            "-fm",
            str(settings.pitch),
            "-r",
            str(settings.speed),
            "-ow",
            str(out),
        ]
        name = Path(settings.open_jtalk).name
        try:
            try:
                result = self._run(
                    args,
                    input=(one_line + "\n").encode("utf-8"),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self._timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                # 元の例外は出力（文章を含みうる）を持つので、つながない
                raise RuntimeError(f"{name} timed out after {self._timeout:g} s") from None
            if result.returncode != 0:
                raise RuntimeError(f"{name} failed with return code {result.returncode}")
            self._normalize(out)
        except BaseException:
            with contextlib.suppress(OSError):
                out.unlink(missing_ok=True)
            raise
