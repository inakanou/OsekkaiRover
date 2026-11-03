"""静かな状態（9.4〜9.6）。

- 単純な真偽値。誰が求めたかは数えず、最後の要求が勝つ。同じ状態への要求は何もしない
- 変わるたびに、基盤の StateStore（`core`）の `quiet` に `{"quiet", "changed_at"}` を保存する
- 起動時に前の値を読み、`was_quiet_before_restart` として出す。起動時の状態は「静かでない」。
  これは初めの値で変化ではないので、起動では保存しない。保存は、機能が最後に切り替えた状態を表す
  （機能が静かに戻す前に再起動が続いても、「静かだった」の印は消えない。夜の画面が点かない側に倒す）
- 静かに戻すかどうかは機能が決める。起動ではイベントを出さない

保存に失敗しても、状態は切り替えてイベントを出す（ログには例外の型だけを残す）。
イベントの順が状態の順と食い違わないよう、切り替え・保存・イベントを 1 つのロックの中で行う
（`EventBus.publish` は積むだけで、受け手は呼ばない）。
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Final

from osekkai.core.events import EventBus, QuietChanged

if TYPE_CHECKING:
    from osekkai.core.clock import Clock
    from osekkai.core.idle import IdleScreen
    from osekkai.core.storage import JsonValue, StateStore

logger = logging.getLogger(__name__)

STATE_KEY: Final = "quiet"


class QuietMode:
    """静かな状態の切り替え、知らせ、再起動をまたぐ保存。"""

    def __init__(self, idle: IdleScreen, bus: EventBus, store: StateStore, clock: Clock) -> None:
        self._idle = idle
        self._bus = bus
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()
        self._quiet = False
        self._was_quiet = self._load_previous()

    @property
    def is_quiet(self) -> bool:
        with self._lock:
            return self._quiet

    @property
    def was_quiet_before_restart(self) -> bool:
        return self._was_quiet

    def request_quiet(self) -> None:
        """静かな状態に入る。画面を消してバックライトを消す（9.4）。"""
        self._change(True)

    def end_quiet(self) -> None:
        """静かな状態を終える。バックライトを点けて待機画面に戻す（9.5）。"""
        self._change(False)

    def _change(self, quiet: bool) -> None:
        with self._lock:
            if self._quiet == quiet:
                return
            self._quiet = quiet
            self._idle.set_quiet(quiet)
            self._save()
            self._bus.publish(QuietChanged(quiet=quiet))

    def _load_previous(self) -> bool:
        try:
            data = self._store.load(STATE_KEY)
        except OSError as error:
            logger.warning("静かな状態を読めませんでした: %s", type(error).__name__)
            return False
        if data is None:
            return False
        value = data.get("quiet")
        return value if isinstance(value, bool) else False

    def _save(self) -> None:
        """ロックの中で呼ぶ。"""
        data: dict[str, JsonValue] = {
            "quiet": self._quiet,
            "changed_at": self._clock.now().isoformat(),
        }
        try:
            self._store.save(STATE_KEY, data)
        except OSError as error:
            logger.warning("静かな状態を保存できませんでした: %s", type(error).__name__)
