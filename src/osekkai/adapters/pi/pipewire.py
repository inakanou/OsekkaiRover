"""paplay（PipeWire）で WAV を再生し、終わるまで待つ（6.1）。"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]

# 1 行は 300 文字まで（話す速さで 1 分に収まる）。超えたら paplay を止める
PLAY_TIMEOUT: Final = 60.0


class PaplayPlayer:
    """AudioPlayerPort の実機の実装。sink が空なら既定の再生先。"""

    def __init__(
        self,
        *,
        command: str = "paplay",
        run: Runner = subprocess.run,
        timeout: float = PLAY_TIMEOUT,
    ) -> None:
        self._command = command
        self._run = run
        self._timeout = timeout

    def play(self, wav: Path, sink: str) -> None:
        args = [self._command]
        if sink:
            args.append(f"--device={sink}")
        args.append(str(wav))
        name = Path(self._command).name
        try:
            result = self._run(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{name} timed out after {self._timeout:g} s") from None
        if result.returncode != 0:
            raise RuntimeError(f"{name} failed with return code {result.returncode}")
