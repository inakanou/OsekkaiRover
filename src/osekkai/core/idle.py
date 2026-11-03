"""待機画面（1.2、9.1〜9.3、10.2）。

優先度 0 の内部の持ち主 `core.idle` として DISPLAY を借りる。中断を知らされたら直ちに返し、
DISPLAY が空いたら借り直して描き直す。

- 1 行目: `MM/DD HH:MM`（11 セル）と、右寄せの短い情報。16 セルに収めるため、
  短い情報は時刻との間に 1 セル空けて、残りの 4 セルまでに切り詰める
  （`set_info` は機能の契約どおり 6 セルまで受け取る）。
  時刻が確かでなければ `MM/DD --:--`
- 2 行目: 顔を中央に置き、視線（−1.0〜1.0）に応じて ±4 セル動かす
- 静かな間も DISPLAY は持ったまま（優先度 0 なので機能は借りられる）、空白を出して
  バックライトを消す。静かな状態を終えたらバックライトを点けて描き直す
- 専用のスレッドが `interval` 秒（実際の時間）ごとに `tick()` を呼び、分の変化を出す。
  待つのは止める合図の `threading.Event`（偽の時計は進めない）。テストは `tick()` で進める

`Lease.release()` は空いた合図を同じスレッドで呼ぶので、返すときは自分のロックの外で返し、
自分で返したときの合図では借り直さない（借りたい機能に先を譲る）。
"""

from __future__ import annotations

import logging
import math
import threading
from enum import Enum
from typing import TYPE_CHECKING, Final

from osekkai.core.display import WIDTH
from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.types import (
    DeviceUnavailable,
    Face,
    LeaseRevoked,
    Priority,
    Resource,
    ResourceBusy,
)

if TYPE_CHECKING:
    from osekkai.core.arbiter import Arbiter, Lease
    from osekkai.core.clock import Clock

logger = logging.getLogger(__name__)

IDLE_OWNER: Final = "core.idle"
INFO_MAX_CELLS: Final = 6  # 機能の契約の上限（9.2）
_CLOCK_CELLS: Final = 11  # "MM/DD HH:MM"
# 1 行目で短い情報に使えるセル。時刻との間に 1 セル空けて読みやすくする
_INFO_CELLS: Final = WIDTH - _CLOCK_CELLS - 1
GAZE_CELLS: Final = 4


class IdleState(Enum):
    SHOWING = "showing"
    YIELDED = "yielded"
    QUIET = "quiet"


def _truncate(text: str, cells: int) -> str:
    """変換後のセル数で `cells` 以内に切り詰める（濁点だけを残さない）。"""
    out = ""
    for ch in text:
        if len(to_lcd_codes(out + ch)) > cells:
            break
        out += ch
    return out


def _cells(text: str) -> int:
    return len(to_lcd_codes(text))


class IdleScreen:
    """何もしていないときの画面。"""

    def __init__(self, arbiter: Arbiter, clock: Clock, *, interval: float = 1.0) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self._arbiter = arbiter
        self._clock = clock
        self.interval = interval
        self._lock = threading.Lock()
        self._lease: Lease | None = None
        self._info: str | None = None
        self._face = Face.NORMAL
        self._gaze = 0.0
        self._quiet = False
        self._started = False
        self._stopped = False
        self._releasing = False  # 自分で返している間（その合図では借り直さない）
        self._shown: tuple[str, str] | None = None  # 最後に出した行（分からなければ None）
        self._backlight: bool | None = None  # 最後に送ったバックライト
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        arbiter.on_resource_freed(Resource.DISPLAY, self._on_display_freed)

    # --- 状態 ---

    @property
    def state(self) -> IdleState:
        with self._lock:
            if self._quiet:
                return IdleState.QUIET
            return IdleState.SHOWING if self._lease is not None else IdleState.YIELDED

    @property
    def quiet(self) -> bool:
        with self._lock:
            return self._quiet

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # --- 始める・止める ---

    def start(self, *, run_thread: bool = True) -> None:
        """画面を借りて描く。使われていれば、空いたときに借りる（1.2）。"""
        with self._lock:
            if self._started:
                return
            self._started = True
        self._try_acquire()
        if run_thread:
            self._thread = threading.Thread(target=self._run, name="osekkai-idle", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """スレッドを止めて画面を返す。以後は借り直さない。"""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            self._stopped = True
            lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()

    def tick(self) -> None:
        """1 回たしかめる。空いた合図を逃していたら借り直し、変わった行だけを描く。"""
        with self._lock:
            missing = self._current() is None
        if missing:
            self._try_acquire()
        with self._lock:
            self._render()

    # --- 機能の道具（5.1）から呼ぶ口 ---

    def set_info(self, text: str | None) -> None:
        """短い情報（9.2）。6 セルまでに切り詰める。None で消す。"""
        value = _truncate(text, INFO_MAX_CELLS) if text else None
        with self._lock:
            self._info = value or None
            self._render()

    def set_face(self, face: Face, gaze: float = 0.0) -> None:
        """顔と視線（9.3）。視線は −1.0〜1.0 に収める。"""
        if not math.isfinite(gaze):
            gaze = 0.0
        with self._lock:
            self._face = face
            self._gaze = max(-1.0, min(1.0, gaze))
            self._render()

    def set_quiet(self, quiet: bool) -> None:
        """静かな状態の切り替え（QuietMode から呼ぶ）。"""
        with self._lock:
            self._quiet = quiet
            self._render()

    # --- 内側 ---

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as error:
                logger.warning("待機画面を更新できませんでした: %s", type(error).__name__)

    def _try_acquire(self) -> None:
        with self._lock:
            if self._stopped or self._current() is not None or self._releasing:
                return
            try:
                lease = self._arbiter.acquire(
                    IDLE_OWNER,
                    Priority.IDLE,
                    [Resource.DISPLAY],
                    wait=0.0,
                    on_preempt=self._on_preempt,
                )
            except ResourceBusy:
                return
            self._lease = lease
            # ほかの機能が描いた後なので、全部を送り直す
            self._shown = None
            self._backlight = None
            self._render()

    def _current(self) -> Lease | None:
        """ロックの中で呼ぶ。終わったリースは忘れる（取り上げられたときなど）。"""
        if self._lease is not None and not self._lease.active:
            self._lease = None
        return self._lease

    def _on_display_freed(self) -> None:
        self._try_acquire()

    def _on_preempt(self) -> None:
        # 中断を知らされたら、描くのをやめて直ちに返す（4.3）
        with self._lock:
            lease = self._lease
            if lease is None or not lease.preempt_requested.is_set():
                return
            self._lease = None
            self._releasing = True
        try:
            lease.release()
        finally:
            with self._lock:
                self._releasing = False

    def _lines(self) -> tuple[str, str]:
        now = self._clock.now()
        hhmm = now.strftime("%H:%M") if self._clock.is_synchronized() else "--:--"
        line1 = f"{now:%m/%d} {hhmm}"
        if self._info:
            info = _truncate(self._info, _INFO_CELLS)
            line1 += " " * (1 + _INFO_CELLS - _cells(info)) + info
        face = self._face.text()
        cells = _cells(face)
        pad = (WIDTH - cells) // 2 + round(self._gaze * GAZE_CELLS)
        pad = max(0, min(pad, WIDTH - cells))
        return line1, " " * pad + face

    def _render(self) -> None:
        """ロックの中で呼ぶ。持っていなければ描かない。"""
        lease = self._current()
        if lease is None:
            return
        backlight = not self._quiet
        lines = ("", "") if self._quiet else self._lines()
        handle = lease.display
        try:
            if self._backlight != backlight:
                self._backlight = None
                handle.set_backlight(backlight)
                self._backlight = backlight
            if self._shown != lines:
                self._shown = None
                handle.show(*lines)
                self._shown = lines
        except LeaseRevoked:
            # 取り上げられた。空いた合図か次のたしかめで借り直す
            self._lease = None
        except DeviceUnavailable as error:
            logger.warning("待機画面を描けませんでした: %s", type(error).__name__)
