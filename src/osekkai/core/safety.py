"""中立の姿勢への復帰と、停止の片付けの手順（1.1、2.2、2.4）。

各段階の失敗はログに残して次の段階に進む。ログには段階の名前と例外の型だけを出す。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from osekkai.core.display import DisplayService, LedService
    from osekkai.core.motion import MotionService

logger = logging.getLogger(__name__)


class SafetyStop:
    """モーター停止・首とステアリングの復帰・LED と LCD の消灯。"""

    def __init__(self, motion: MotionService, display: DisplayService, led: LedService) -> None:
        self._motion = motion
        self._display = display
        self._led = led

    def neutral(self) -> list[str]:
        """モーター停止 → 首とステアリングを正面 → LED を消す。失敗した段階の名前を返す。"""
        failed: list[str] = []
        # ロックを待たずに先に止める（例外を出さない）
        self._motion.emergency_stop()
        self._step("stop", self._motion.stop, failed)
        self._step("home", self._motion.home, failed)
        self._step("led", lambda: self._led.set(False), failed)
        return failed

    def halt(self) -> list[str]:
        """中立の後に LCD を消してバックライトを消す。失敗した段階の名前を返す。"""
        failed = self.neutral()
        self._step("display_clear", self._display.clear, failed)
        self._step("display_backlight", lambda: self._display.set_backlight(False), failed)
        return failed

    @staticmethod
    def _step(name: str, action: Callable[[], None], failed: list[str]) -> None:
        try:
            action()
        except Exception as error:
            failed.append(name)
            logger.warning("安全の手順 %s に失敗しました: %s", name, type(error).__name__)
