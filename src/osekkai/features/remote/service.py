"""操作の受け付けと、0.1 秒ごとの走行の繰り返し（要件 3.2、3.4、4.2〜4.8、5.1〜5.6、6.1〜6.3、
11.1〜11.3、12.3）。

- どの操作も「使えるか → 操作する端末か → 操作の段のリース」の順で確かめる。
  だめなら NOT_NOW を返し、その端末への知らせの文を覚える
- 走る命令は、最新の 1 つを時刻つきで置くだけ。止めるのも途切れ（0.5 秒）も、
  補助のスレッド（remote-drive）の繰り返しが走行の判定（decide）で決める
- 繰り返しは操作の段のリースの間だけ動く。リースの段を変える（want）のは繰り返しの外だけ
  （want は最長 2〜3 秒止まることがある）
- 危険で止めたときの声は補助のスレッド（remote-say）で話し、繰り返しを止めない
- ログには型の名前と決まった文言だけを出す。印・文字・言葉は出さない
"""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Mapping
from enum import Enum
from typing import Final

from osekkai.core.arbiter import Lease
from osekkai.core.feature import FeatureContext
from osekkai.core.lcd_text import center
from osekkai.core.types import Face, LeaseRevoked, Resource

from . import texts
from .availability import Availability, judge_availability, message_for
from .config import RemoteConfig
from .drive_rules import (
    COMMAND_TIMEOUT_S,
    FORWARD_MOVES,
    Command,
    Danger,
    DangerVoice,
    Decision,
    DriveState,
    LcdState,
    Move,
    decide,
)
from .leases import LeaseKeeper, Level
from .media import Camera, FrameHub
from .operator_slot import ClaimResult, OperatorSlot
from .photos import PhotoStore
from .ports import SensorPort, SensorReading

__all__ = ["DRIVE_PERIOD_S", "MAX_FAILURES", "RemoteService", "Reply"]

logger = logging.getLogger(__name__)

DRIVE_PERIOD_S: Final = 0.1  # 走行の繰り返しの間隔（5.1）
MAX_FAILURES: Final = 10  # 続けてこれだけ失敗したらリースを返す（12.6）
JOIN_S: Final = 1.0  # 閉じるときに繰り返しを待つ上限

_DRIVE_LCD: Final[Mapping[LcdState, str]] = {
    LcdState.STOPPED: texts.LCD_IDLE,
    LcdState.FORWARD: texts.LCD_FORWARD,
    LcdState.BACK: texts.LCD_BACK,
}
# 読めない（UNKNOWN）は物と同じ知らせにする（止まる側。5.4、5.5）
_DANGER_MESSAGE: Final[Mapping[Danger, str]] = {
    Danger.OBSTACLE: texts.MSG_DANGER_OBSTACLE,
    Danger.UNKNOWN: texts.MSG_DANGER_OBSTACLE,
    Danger.EDGE: texts.MSG_DANGER_CLIFF,
}
_DANGER_VOICE: Final[Mapping[Danger, str]] = {
    Danger.OBSTACLE: texts.SAY_DANGER_OBSTACLE,
    Danger.UNKNOWN: texts.SAY_DANGER_OBSTACLE,
    Danger.EDGE: texts.SAY_DANGER_CLIFF,
}
_SHOWN_STOP: Final = "stop"  # LCD に「ｽﾄｯﾌﾟ!」を出している


class Reply(Enum):
    """操作の結果。"""

    OK = "ok"
    NOT_NOW = "not_now"
    BUSY_SPEAKING = "busy_speaking"
    EMPTY = "empty"
    IGNORED = "ignored"
    FAILED = "failed"


class RemoteService:
    """端末からの操作を受け、確かめてから走行の繰り返しに渡す。"""

    def __init__(
        self,
        ctx: FeatureContext,
        config: RemoteConfig,
        sensors: SensorPort,
        hubs: Mapping[Camera, FrameHub],
        photos: PhotoStore,
        slot: OperatorSlot,
        leases: LeaseKeeper,
        rng: random.Random,
    ) -> None:
        self._ctx = ctx
        self._config = config
        self._sensors = sensors
        self._hubs = hubs
        self._photos = photos
        self._slot = slot
        self._leases = leases
        self._rng = rng
        self._speeds = (config.speed_slow, config.speed_fast)
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None
        self._threaded = True  # 偽なら繰り返しを始めない（テストが 1 歩ずつ進める）
        # 走行（_lock の中で読み書きする）
        self._command: Command | None = None
        self._fast = False  # 始めはゆっくり（4.5）
        self._accepted_lease: Lease | None = None  # 命令を置いたときのリース
        # 繰り返しだけが読み書きする
        self._state = DriveState()
        self._driving_lease: Lease | None = None
        self._shown: LcdState | str | None = None  # LCD に出している表示
        self._failures = 0
        self._give_up = False
        self._sensor_failed = False
        self._voice = DangerVoice()
        # 知らせ（_lock の中で読み書きする）
        self._notices: dict[str, str] = {}  # 端末ごとの断りの知らせ
        self._danger_message: str | None = None  # 危険で止めた知らせ（操作する端末に出す）
        self._speaking = False  # 自分が話している途中（1 つ）

    # --- 操作 ---

    def drive(self, token: str, move: Move) -> Reply:
        """走る命令を時刻つきで置く。止めるのも同じ（次の繰り返しで止まる）。"""
        if not self._accept(token):
            return Reply.NOT_NOW
        with self._lock:
            self._command = Command(move, self._ctx.clock.monotonic())
        return Reply.OK

    def set_fast(self, token: str, fast: bool) -> Reply:
        """速さを選ぶ。次の繰り返しから使う（4.5、4.6）。"""
        if not self._accept(token):
            return Reply.NOT_NOW
        with self._lock:
            self._fast = fast
        return Reply.OK

    def close(self) -> None:
        """走行の繰り返しを止めて待つ。何度呼んでもよい。"""
        self._closing.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(JOIN_S)

    # --- 受け付けの順 ---

    def _accept(self, token: str) -> bool:
        """使えるか → 操作する端末か → 操作の段のリース、の順に確かめる。"""
        clock = self._ctx.clock
        availability = judge_availability(
            synced=clock.is_synchronized(),
            now=clock.now(),
            quiet=self._ctx.is_quiet,
            hours=self._config.hours,
        )
        if availability is not Availability.OK:  # 11.1〜11.3
            return self._refuse(token, message_for(availability))
        if self._slot.claim(token) is ClaimResult.OTHER:  # 3.2
            return self._refuse(token, message_for(Availability.OTHER_OPERATOR))
        # want はロックを持たずに呼ぶ（止まることがある）
        if self._leases.want(Level.OPERATE) is not Level.OPERATE:  # 12.3
            return self._refuse(token, message_for(Availability.BUSY))
        lease = self._leases.lease
        with self._lock:
            self._notices.pop(token, None)
            if lease is not self._accepted_lease:
                # 借り直した。止まる前の命令を続けない（12.3）
                self._accepted_lease = lease
                self._command = None
            if self._closing.is_set():
                return False
            if self._thread is None and self._threaded:
                self._give_up = False
                self._failures = 0
                self._thread = threading.Thread(target=self._run, name="remote-drive", daemon=True)
                self._thread.start()
        return True

    def _refuse(self, token: str, message: str | None) -> bool:
        with self._lock:
            if message is None:
                self._notices.pop(token, None)
            else:
                self._notices[token] = message
        return False

    def _message_for(self, token: str) -> str | None:
        """その端末に出す知らせ。断りの知らせが先、次に危険の知らせ（操作する端末だけ）。"""
        with self._lock:
            notice = self._notices.get(token)
            danger = self._danger_message
        if notice is not None:
            return notice
        if danger is not None and self._slot.holder() == token:
            return danger
        return None

    # --- 走行の繰り返し ---

    def _run(self) -> None:
        """0.1 秒ごとに 1 歩ずつ進める。リースが操作の段でなくなったら終わる。"""
        clock = self._ctx.clock
        try:
            while True:
                start = clock.monotonic()
                go_on = self._drive_step()
                if not go_on:
                    # 確かめ直しと終わりの支度を 1 つのロックの中で行う（受け付けが間に入らない）
                    with self._lock:
                        # 終わる間際に借り直されたなら続ける
                        go_on = not (self._closing.is_set() or self._give_up) and self._operating()
                        if not go_on:
                            self._thread = None
                            give_up = self._give_up
                            self._command = None
                if not go_on:
                    break
                clock.sleep(max(0.0, DRIVE_PERIOD_S - (clock.monotonic() - start)))
        except Exception as e:
            logger.warning("走行の繰り返しが止まりました: %s", type(e).__name__)
            self._stop_quietly(self._last_lease())
            with self._lock:
                self._thread = None
                self._command = None
            return
        if give_up:
            # 繰り返しの外で返す（12.6）
            logger.warning("走行の失敗が続いたので、リースを返します")
            self._leases.want(Level.NONE)

    def _last_lease(self) -> Lease | None:
        """最後に走らせたリース。なければ今のリース。読めなければ None。"""
        if self._driving_lease is not None:
            return self._driving_lease
        try:
            return self._leases.lease
        except Exception as e:
            logger.warning("リースを読めません: %s", type(e).__name__)
            return None

    def _operating(self) -> bool:
        lease = self._leases.lease
        return lease is not None and Resource.DRIVE in lease.resources

    def _drive_step(self) -> bool:
        """1 歩。続けるなら真。例外は受けて止め、次の命令を受け付ける状態に戻す（4.8）。"""
        lease = self._leases.lease
        if self._closing.is_set() or lease is None or Resource.DRIVE not in lease.resources:
            self._stop_quietly(lease)
            self._reset()
            return False
        if lease is not self._driving_lease:
            self._reset()
            self._driving_lease = lease
        try:
            self._apply(lease)
        except LeaseRevoked:
            self._reset()
            return False
        except Exception as e:
            logger.warning("走行の途中で失敗しました: %s", type(e).__name__)
            self._stop_quietly(lease)
            self._reset()
            self._failures += 1
            if self._failures >= MAX_FAILURES:
                with self._lock:
                    self._give_up = True
                return False
            return True
        self._failures = 0
        return True

    def _apply(self, lease: Lease) -> None:
        with self._lock:
            # 受け付けがまだ見ていないリースでは、前の命令を使わない（12.3）
            command = self._command if lease is self._accepted_lease else None
            fast = self._fast
        now = self._ctx.clock.monotonic()
        reading = self._read_front(command, now)
        decision, self._state = decide(
            command,
            now,
            reading,
            self._state,
            fast=fast,
            speeds=self._speeds,
            back_max_s=self._config.back_max_seconds,
            stop_cm=self._config.stop_cm,
            cliff_stop=self._config.cliff_stop,
            cliff_reference=self._config.cliff_reference,
        )
        if decision.speed == 0:
            lease.drive.stop()
        else:
            lease.drive.drive(decision.steering, decision.speed)
        self._show(lease, decision)
        if decision.stopped_by is not None:
            self._on_danger(lease, decision.stopped_by, now)
        elif decision.speed != 0:
            with self._lock:
                self._danger_message = None

    def _read_front(self, command: Command | None, now: float) -> SensorReading | None:
        """前に進む命令が生きているときだけ 1 回読む（5.1）。読めなければ None（5.4）。"""
        if command is None or command.move not in FORWARD_MOVES:
            return None
        if not 0.0 <= now - command.at <= COMMAND_TIMEOUT_S:
            return None
        try:
            reading = self._sensors.read()
        except Exception as e:
            if not self._sensor_failed:
                logger.warning("前のセンサーを読めません: %s", type(e).__name__)
            self._sensor_failed = True
            return None
        self._sensor_failed = False
        return reading

    def _show(self, lease: Lease, decision: Decision) -> None:
        """走行の LCD を、変わったときだけ出す（4.7、5.5）。"""
        shown: LcdState | str
        if decision.stopped_by is not None:
            shown, line1, face = _SHOWN_STOP, texts.LCD_STOP, texts.FACE_STOP
        else:
            shown, line1, face = decision.lcd, _DRIVE_LCD[decision.lcd], texts.FACE_DRIVE
        if shown == self._shown:
            return
        _display(lease, line1, face)
        self._shown = shown

    def _on_danger(self, lease: Lease, danger: Danger, now: float) -> None:
        """危険の知らせは毎回、声は 10 秒に 1 回（5.5、5.6）。"""
        with self._lock:
            self._danger_message = _DANGER_MESSAGE[danger]
            if self._speaking:
                return
            if not self._voice.should_speak(now):
                return
            self._speaking = True
        thread = threading.Thread(
            target=self._say, args=(lease, _DANGER_VOICE[danger]), name="remote-say", daemon=True
        )
        thread.start()

    def _say(self, lease: Lease, text: str) -> None:
        """決まった言葉を話す（補助のスレッド）。"""
        try:
            lease.voice.say(text, keep=True)
        except LeaseRevoked:
            pass
        except Exception as e:
            logger.warning("話せませんでした: %s", type(e).__name__)
        finally:
            with self._lock:
                self._speaking = False

    def _stop_quietly(self, lease: Lease | None) -> None:
        """止めるのを試す。失敗は型の名前だけ残す。"""
        if lease is None or not lease.active or Resource.DRIVE not in lease.resources:
            return
        try:
            lease.drive.stop()
        except LeaseRevoked:
            pass
        except Exception as e:
            logger.warning("止められませんでした: %s", type(e).__name__)

    def _reset(self) -> None:
        """走行の状態を空にする。LCD は次の歩で出し直す。"""
        self._state = DriveState()
        self._shown = None


def _display(lease: Lease, line1: str, face: Face) -> None:
    lease.display.show(center(line1), center(face.text()))
