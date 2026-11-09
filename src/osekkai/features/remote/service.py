"""操作の受け付けと、0.1 秒ごとの走行の繰り返し、首・ブッブー・ダンス・話させる、
写真・状態の知らせ・定期の確かめ・止め方
（要件 3.2〜3.4、4.2〜4.8、5.1〜5.6、6.1〜6.3、7.1〜7.6、8.6、9.1〜9.4、10.1〜10.5、
11.1〜11.4、12.2〜12.4、12.6）。

- どの操作も「使えるか → 操作する端末か → 操作の段のリース」の順で確かめる。
  だめなら NOT_NOW を返し、その端末への知らせの文を覚える
- 走る命令は、最新の 1 つを時刻つきで置くだけ。止めるのも途切れ（0.5 秒）も、
  補助のスレッド（remote-drive）の繰り返しが走行の判定（decide）で決める
- 繰り返しは操作の段のリースの間だけ動く。リースの段を変える（want）のは繰り返しの外だけ
  （want は最長 2〜3 秒止まることがある）
- 危険で止めたときの声は補助のスレッド（remote-say）で話し、繰り返しを止めない
- 首は今の向きを覚えて 15 度ずつ動かす。右と上が正（pan は右、tilt は上）。
  基盤も範囲に収めるが、覚える向きも基盤の既定の範囲に収める（押した分を貯めない）
- ダンスは補助のスレッド（remote-dance）で首だけで踊る。踊っている間の走る命令は無視し、
  STOP の命令はダンスを止める合図にする
- 話させる文字と決まった言葉は補助のスレッド（remote-say）で話す。話させる文字は keep=False で、
  ログにも、このオブジェクトにも残さない
- ダンスと話す間の LCD は、走行の LCD より先に出す（危険の「ｽﾄｯﾌﾟ!」だけはさらに先）。
  終われば繰り返しが走行の表示に戻す
- 写真は要求のスレッドで撮る。「はい、チーズ！」を話し終えてから首・USB の順に 1 枚ずつ撮る。
  撮っている途中の次の要求は無視する（9.4）。写真の名前はログに出さない
- 定期の確かめ（tick）は機能のスレッドで 0.5 秒ごとに呼ぶ。走行の繰り返しからは呼ばない
  （want は最長 2〜3 秒止まることがある）。使えなくなれば止めて返し、10 秒の交代で段を下げ、
  見るだけの段を借り直し、首を持たない間は首のカメラを返す
- 中断の後片付け（on_lost）は基盤の短いスレッドで呼ばれる。止める合図を出し、首のカメラを返すだけ。
  話さない・待たない・リースの段を変えない（12.2）
- ログには型の名前と決まった文言だけを出す。印・文字・言葉は出さない
"""

from __future__ import annotations

import logging
import random
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal

from osekkai.core.arbiter import Lease
from osekkai.core.feature import FeatureContext
from osekkai.core.lcd_text import center
from osekkai.core.types import Face, LeaseRevoked, Resource

from . import texts
from .availability import Availability, judge_availability, message_for
from .config import RemoteConfig
from .dance import run_dance
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

__all__ = [
    "DRIVE_PERIOD_S",
    "LOOK_STEP_DEG",
    "MAX_FAILURES",
    "MAX_TEXT",
    "PhotoResult",
    "RemoteService",
    "Reply",
    "Status",
    "clean_text",
]

logger = logging.getLogger(__name__)

DRIVE_PERIOD_S: Final = 0.1  # 走行の繰り返しの間隔（5.1）
MAX_FAILURES: Final = 10  # 続けてこれだけ失敗したらリースを返す（12.6）
JOIN_S: Final = 1.0  # 閉じるときに繰り返しを待つ上限
LOOK_STEP_DEG: Final = 15.0  # 首を 1 回で動かす角度（7.1）
PAN_RANGE: Final = (-80.0, 80.0)  # 基盤の既定の首の左右の範囲（基盤も収める）
TILT_RANGE: Final = (-30.0, 60.0)  # 基盤の既定の首の上下の範囲
MAX_TEXT: Final = 120  # 話させる文字の長さの上限（10.1）
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")

Way = Literal["up", "down", "left", "right", "center"]
# 向きごとの (pan, tilt) の増え方。右と上が正
_LOOK_STEPS: Final[Mapping[str, tuple[float, float]]] = {
    "up": (0.0, LOOK_STEP_DEG),
    "down": (0.0, -LOOK_STEP_DEG),
    "left": (-LOOK_STEP_DEG, 0.0),
    "right": (LOOK_STEP_DEG, 0.0),
}
_OVERLAY_DANCE: Final = "dance"
_OVERLAY_TALK: Final = "talk"
_OVERLAY_PHOTO: Final = "photo"
_FACE_PHOTO: Final = texts.FACE_DRIVE  # 写真の LCD は笑顔
# 写真を撮る順（首が先）と、保存の名前のカメラ
_PHOTO_CAMERAS: Final[tuple[tuple[Camera, Literal["neck", "usb"]], ...]] = (
    (Camera.NECK, "neck"),
    (Camera.USB, "usb"),
)

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


@dataclass(frozen=True)
class Status:
    """端末ごとの状態。"""

    message: str | None  # 端末に出す知らせ（使えるか・ほかの端末・ほかの機能・危険）
    operator: Literal["me", "other", "none"]
    fast: bool
    cameras: Mapping[Camera, bool]  # カメラごとの使えるか（8.3）
    dancing: bool


@dataclass(frozen=True)
class PhotoResult:
    """写真の結果。"""

    names: tuple[str, ...]  # 撮れた写真の名前（首が先）
    reply: Reply


def clean_text(text: str) -> str:
    """制御文字（C0・DEL・C1）を空白に変え、前後の空白を除き、120 文字までに切る（10.1）。"""
    return _CONTROL.sub(" ", text).strip()[:MAX_TEXT]


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
        # 首とダンス（_lock の中で読み書きする）
        self._pan = 0.0
        self._tilt = 0.0
        self._neck_lease: Lease | None = None  # 向きを覚えたリース（借り直したら正面）
        self._dancing = False
        self._dance_stop = False  # STOP の命令が来た（7.6）
        # LCD（_lcd_lock の中で読み書きする。走行の LCD もこのロックの中で出す）
        self._lcd_lock = threading.Lock()
        self._overlays: list[tuple[str, str, Face]] = []  # 走行より先に出す表示（後が上）
        self._lcd_dirty = False  # 走行の表示を出し直す
        # 写真（9.4）。撮っている途中の印と、止める合図
        self._photo_lock = threading.Lock()
        self._photo_abort = threading.Event()
        # 段の決め方と借り方を 1 つずつにする（受け付けと定期の確かめが入れ違わない）
        self._level_lock = threading.Lock()

    # --- 操作 ---

    def drive(self, token: str, move: Move) -> Reply:
        """走る命令を時刻つきで置く。止めるのも同じ（次の繰り返しで止まる）。

        踊っている間は STOP だけを受け、ダンスを止める合図にする（7.5、7.6）。
        """
        if self._accept(token) is None:
            return Reply.NOT_NOW
        with self._lock:
            if self._dancing:
                if move is not Move.STOP:
                    return Reply.IGNORED
                self._dance_stop = True
            self._command = Command(move, self._ctx.clock.monotonic())
        return Reply.OK

    def look(self, token: str, way: Way) -> Reply:
        """首を 15 度動かす。「まんなか」は正面に戻す（7.1、7.2）。踊っている間は無視する。"""
        lease = self._accept(token)
        if lease is None:
            return Reply.NOT_NOW
        with self._lock:
            if self._dancing:
                return Reply.IGNORED
            pan, tilt = self._angles(lease)
            if way != "center":
                d_pan, d_tilt = _LOOK_STEPS[way]
                pan = _clamp(pan + d_pan, PAN_RANGE)
                tilt = _clamp(tilt + d_tilt, TILT_RANGE)
        try:
            if way == "center":
                lease.neck.home()
                pan = tilt = 0.0
            else:
                lease.neck.look(pan, tilt)
        except LeaseRevoked:
            return Reply.NOT_NOW
        except Exception as e:
            logger.warning("首を動かせませんでした: %s", type(e).__name__)
            return Reply.FAILED
        with self._lock:
            if self._neck_lease is lease:
                self._pan, self._tilt = pan, tilt
        return Reply.OK

    def horn(self, token: str) -> Reply:
        """4 つから 1 つを選んで話す。話している途中なら話さずに IGNORED（7.3）。"""
        lease = self._accept(token)
        if lease is None:
            return Reply.NOT_NOW
        with self._lock:
            if self._speaking:
                return Reply.IGNORED
            self._speaking = True
            words = self._rng.choice(texts.SAY_HORNS)
        threading.Thread(
            target=self._say, args=(lease, words), name="remote-say", daemon=True
        ).start()
        return Reply.OK

    def dance(self, token: str) -> Reply:
        """走行を止め、補助のスレッドで首だけで踊る（7.4）。踊っている間は無視する（7.5）。"""
        lease = self._accept(token)
        if lease is None:
            return Reply.NOT_NOW
        with self._lock:
            if self._dancing:
                return Reply.IGNORED
            self._dancing = True
            self._dance_stop = False
            self._command = Command(Move.STOP, self._ctx.clock.monotonic())  # 4.3
            back_to = self._angles(lease)
        threading.Thread(
            target=self._dance, args=(lease, token, back_to), name="remote-dance", daemon=True
        ).start()
        return Reply.OK

    def say_text(self, token: str, text: str) -> Reply:
        """送られた文字を整えて話す（10.1〜10.4）。文字はログにもここにも残さない（10.5）。"""
        lease = self._accept(token)
        if lease is None:
            return Reply.NOT_NOW
        words = clean_text(text)
        if not words:
            return Reply.EMPTY
        with self._lock:
            if self._speaking:
                return Reply.BUSY_SPEAKING
            self._speaking = True
        threading.Thread(
            target=self._talk, args=(lease, words), name="remote-say", daemon=True
        ).start()
        return Reply.OK

    def set_fast(self, token: str, fast: bool) -> Reply:
        """速さを選ぶ。次の繰り返しから使う（4.5、4.6）。"""
        if self._accept(token) is None:
            return Reply.NOT_NOW
        with self._lock:
            self._fast = fast
        return Reply.OK

    def take_photo(self, token: str) -> PhotoResult:
        """走行を止め、「はい、チーズ！」を話し終えてから 1 枚ずつ撮って保存する（9.1〜9.4）。

        要求のスレッドで待つ。撮っている途中の次の要求は IGNORED。
        途中で止められたら（中断の知らせ・止め方・閉じる）、撮らず話さずに FAILED。
        """
        lease = self._accept(token)
        if lease is None:
            return PhotoResult((), Reply.NOT_NOW)
        if not self._photo_lock.acquire(blocking=False):  # 9.4
            return PhotoResult((), Reply.IGNORED)
        try:
            self._photo_abort.clear()
            return self._photo(lease)
        finally:
            self._photo_lock.release()

    def status(self, token: str) -> Status:
        """その端末の状態。映像を見る・状態を聞くは操作に入らないので、席を取らない。"""
        holder = self._slot.holder()
        operator: Literal["me", "other", "none"]
        if holder is None:
            operator = "none"
        elif holder == token:
            operator = "me"
        else:
            operator = "other"
        with self._lock:
            fast = self._fast
            dancing = self._dancing
        return Status(
            message=self._status_message(token, operator),
            operator=operator,
            fast=fast,
            cameras={camera: hub.available() for camera, hub in self._hubs.items()},
            dancing=dancing,
        )

    def may_stream(self, token: str) -> bool:
        """映像を続けてよいか。時間帯の外・おやすみ・時刻が確かでないなら偽（8.6）。

        ログインが有効かは web が確かめる。印はここでは使わない。
        """
        del token
        return self._availability() is Availability.OK

    def neck_allowed(self) -> bool:
        """首のカメラを借りてよいか（NeckSource の may_borrow）。

        首のリースを持ち、中断の後でない。
        """
        lease = self._leases.lease
        return lease is not None and Resource.NECK in lease.resources and not self._leases.lost

    def tick(self) -> None:
        """定期の確かめ。機能のスレッドから 0.5 秒ごとに呼ぶ（走行の繰り返しからは呼ばない）。

        思わぬ失敗は型の名前だけ残し、走行を止める（12.6）。
        """
        try:
            self._tick()
        except Exception as e:
            logger.warning("定期の確かめで失敗しました: %s", type(e).__name__)
            self._stop_driving()

    def halt(self) -> None:
        """止め方（時間帯の終わり・おやすみ）。走行を止め、ダンスと写真に止める合図を出し、
        首のカメラを返す。話している途中の言葉は、基盤の決まりで言い終える（11.4）。
        """
        self._signal_stop()
        self._stop_driving()
        self._release_neck_camera()

    def drop_operator(self, token: str) -> None:
        """ログインが切れた印を席から外す。操作する端末だったなら走行を止める（3.4）。"""
        if self._slot.drop(token):
            self._stop_driving()

    def on_lost(self) -> None:
        """中断の後片付け（LeaseKeeper の on_lost。基盤の短いスレッド）。

        止める合図を出し、走る命令を消し、首のカメラを返す。
        話さない・待たない・リースの段を変えない（12.2）。走行は基盤が止める。
        """
        self._signal_stop()
        with self._lock:
            self._command = None
        self._release_neck_camera()

    def close(self) -> None:
        """走行の繰り返し・ダンス・写真を止め、繰り返しを待つ。何度呼んでもよい。"""
        self._closing.set()
        self._photo_abort.set()
        with self._lock:
            self._command = None
            if self._dancing:
                self._dance_stop = True
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(JOIN_S)

    # --- 受け付けの順 ---

    def _accept(self, token: str) -> Lease | None:
        """使えるか → 操作する端末か → 操作の段のリース、の順に確かめる。

        受け付けたら操作の段のリースを返す。断ったら None。
        """
        availability = self._availability()
        if availability is not Availability.OK:  # 11.1〜11.3
            self._refuse(token, message_for(availability))
            return None
        # want は _lock を持たずに呼ぶ（止まることがある）
        with self._level_lock:
            if self._slot.claim(token) is ClaimResult.OTHER:  # 3.2
                self._refuse(token, message_for(Availability.OTHER_OPERATOR))
                return None
            if self._leases.want(Level.OPERATE) is not Level.OPERATE:  # 12.3
                self._refuse(token, message_for(Availability.BUSY))
                return None
        lease = self._leases.lease
        if lease is None:
            self._refuse(token, message_for(Availability.BUSY))
            return None
        with self._lock:
            self._notices.pop(token, None)
            if lease is not self._accepted_lease:
                # 借り直した。止まる前の命令を続けない（12.3）
                self._accepted_lease = lease
                self._command = None
            if self._closing.is_set():
                return None
            if self._thread is None and self._threaded:
                self._give_up = False
                self._failures = 0
                self._thread = threading.Thread(target=self._run, name="remote-drive", daemon=True)
                self._thread.start()
        return lease

    def _availability(self) -> Availability:
        clock = self._ctx.clock
        return judge_availability(
            synced=clock.is_synchronized(),
            now=clock.now(),
            quiet=self._ctx.is_quiet,
            hours=self._config.hours,
        )

    def _status_message(self, token: str, operator: str) -> str | None:
        """状態に出す知らせ。使えるか → ほかの端末 → ほかの機能 → 危険（操作する端末だけ）の順。"""
        availability = self._availability()
        if availability is not Availability.OK:
            return message_for(availability)
        if operator == "other":
            return message_for(Availability.OTHER_OPERATOR)
        busy = message_for(Availability.BUSY)
        with self._lock:
            notice = self._notices.get(token)
            danger = self._danger_message
        if self._leases.lost or notice == busy:
            return busy
        if operator == "me":
            return danger
        return None

    def _refuse(self, token: str, message: str | None) -> None:
        with self._lock:
            if message is None:
                self._notices.pop(token, None)
            else:
                self._notices[token] = message

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
        with self._lcd_lock:
            if self._lcd_dirty:
                self._lcd_dirty = False
                self._shown = None
            # ダンスと話す間は、その表示のまま（7.4、10.2）。危険の表示だけは先に出す（5.5）
            if self._overlays and decision.stopped_by is None:
                # 危険が消えたら、ダンスと話す表示に戻す
                if self._shown == _SHOWN_STOP:
                    _, top, top_face = self._overlays[-1]
                    _display(lease, top, top_face)
                    self._shown = None
                return
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

    # --- 定期の確かめと止め方 ---

    def _tick(self) -> None:
        neck = self._hubs.get(Camera.NECK)
        if self._availability() is not Availability.OK:
            # 時間帯の終わり・おやすみ・時刻が確かでない（11.4）
            self.halt()
            with self._level_lock:
                if self._level() is not Level.NONE:
                    self._leases.want(Level.NONE)
        else:
            if self._slot.expire():  # 3.3、3.4
                self._stop_driving()
            with self._level_lock:
                # 首の映像を見る端末がいれば見るだけ、いなければなし（12.4）。
                # 見るだけの借り直しは LeaseKeeper が 5 秒おく
                viewers = neck.viewers() if neck is not None else 0
                level = self._level()
                if self._slot.holder() is None:
                    target = Level.VIEW if viewers > 0 else Level.NONE
                    if level is not target:
                        self._leases.want(target)
                elif level is Level.NONE and viewers > 0:
                    # 操作する端末がいても、取り上げの後は見るだけを借り直す。
                    # 走る段は、次の操作で借り直す
                    self._leases.want(Level.VIEW)
        # 首を持たない（取り上げ・下げた段）なら首のカメラを返す（4.3）
        if self._level() is Level.NONE:
            self._release_neck_camera()
        for hub in self._hubs.values():
            hub.tick()

    def _level(self) -> Level:
        """今持っているリースの段。"""
        lease = self._leases.lease
        if lease is None:
            return Level.NONE
        if Resource.DRIVE in lease.resources:
            return Level.OPERATE
        return Level.VIEW

    def _signal_stop(self) -> None:
        """ダンスと写真に止める合図を出す。"""
        self._photo_abort.set()
        with self._lock:
            if self._dancing:
                self._dance_stop = True

    def _stop_driving(self) -> None:
        """走る命令を STOP にして、すぐ止める。"""
        with self._lock:
            self._command = Command(Move.STOP, self._ctx.clock.monotonic())
        self._stop_quietly(self._last_lease())

    def _release_neck_camera(self) -> None:
        neck = self._hubs.get(Camera.NECK)
        if neck is not None:
            neck.release_neck()

    # --- 写真 ---

    def _photo_go(self, lease: Lease) -> bool:
        """写真を続けてよいか。"""
        return (
            lease.active
            and not lease.preempt_requested.is_set()
            and not self._photo_abort.is_set()
            and not self._closing.is_set()
        )

    def _photo(self, lease: Lease) -> PhotoResult:
        with self._lock:
            self._command = Command(Move.STOP, self._ctx.clock.monotonic())  # 4.3
        self._stop_quietly(lease)
        try:
            self._overlay_on(lease, _OVERLAY_PHOTO, texts.LCD_PHOTO, _FACE_PHOTO)
            self._photo_say(lease, texts.SAY_PHOTO_CHEESE)
            if not self._photo_go(lease):
                return PhotoResult((), Reply.FAILED)
            stills: list[tuple[Literal["neck", "usb"], bytes]] = []
            for camera, name in _PHOTO_CAMERAS:
                hub = self._hubs.get(camera)
                if hub is None:
                    continue
                jpeg = hub.still()
                if jpeg is not None:
                    stills.append((name, jpeg))
            taken_at = self._ctx.clock.now()
            names: list[str] = []
            for name, jpeg in stills:
                try:
                    names.append(self._photos.save(name, jpeg, taken_at=taken_at))
                except Exception as e:
                    logger.warning("写真を保存できませんでした: %s", type(e).__name__)
            if not self._photo_go(lease):
                # 止められた。撮れた写真は返すが、話さない
                return PhotoResult(tuple(names), Reply.OK if names else Reply.FAILED)
            self._photo_say(lease, texts.SAY_PHOTO_DONE if names else texts.SAY_PHOTO_FAILED)
            return PhotoResult(tuple(names), Reply.OK if names else Reply.FAILED)
        except LeaseRevoked:
            return PhotoResult((), Reply.FAILED)
        except Exception as e:
            logger.warning("写真を撮れませんでした: %s", type(e).__name__)
            return PhotoResult((), Reply.FAILED)
        finally:
            self._overlay_off(lease, _OVERLAY_PHOTO)

    def _photo_say(self, lease: Lease, text: str) -> None:
        """写真の決まった言葉を話し終えるまで待つ。ほかの発話の途中なら、その終わりを待つ。

        話している印は、空いていたときだけ自分で立てて下ろす。
        """
        with self._lock:
            mine = not self._speaking
            if mine:
                self._speaking = True
        try:
            lease.voice.say(text, keep=True)
        finally:
            if mine:
                with self._lock:
                    self._speaking = False

    # --- 首・ダンス・話させる ---

    def _angles(self, lease: Lease) -> tuple[float, float]:
        """覚えている首の向き。借り直したリースなら正面から数える（_lock の中で呼ぶ）。"""
        if lease is not self._neck_lease:
            self._neck_lease = lease
            self._pan = self._tilt = 0.0
        return self._pan, self._tilt

    def _dance_go(self, lease: Lease, token: str) -> bool:
        """ダンスを続けてよいか（7.6）。"""
        if not lease.active or lease.preempt_requested.is_set():
            return False
        if self._closing.is_set():
            return False
        with self._lock:
            if self._dance_stop:
                return False
        return self._slot.holder() == token

    def _dance(self, lease: Lease, token: str, back_to: tuple[float, float]) -> None:
        """首だけで踊る（補助のスレッド）。走行の口は渡さない（7.4）。"""

        def look(pan: float, tilt: float) -> None:
            # 動かした向きを覚える（止めたダンスの後も、その向きから数える。7.1）
            lease.neck.look(pan, tilt)
            with self._lock:
                if self._neck_lease is lease:
                    self._pan, self._tilt = pan, tilt

        try:
            self._overlay_on(lease, _OVERLAY_DANCE, texts.LCD_DANCE, texts.FACE_DANCE)
            run_dance(
                look=look,
                go=lambda: self._dance_go(lease, token),
                sleep=self._ctx.clock.sleep,
                back_to=back_to,
            )
        except LeaseRevoked:
            pass
        except Exception as e:
            logger.warning("踊れませんでした: %s", type(e).__name__)
        finally:
            self._overlay_off(lease, _OVERLAY_DANCE)
            with self._lock:
                self._dancing = False
                self._dance_stop = False

    def _talk(self, lease: Lease, words: str) -> None:
        """話させる文字を話す（補助のスレッド）。文字は keep=False で残さない（10.2、10.5）。"""
        try:
            self._overlay_on(lease, _OVERLAY_TALK, texts.LCD_TALK, texts.FACE_TALK)
            lease.voice.say(words, keep=False)
        except LeaseRevoked:
            pass
        except Exception as e:
            logger.warning("話せませんでした: %s", type(e).__name__)
        finally:
            self._overlay_off(lease, _OVERLAY_TALK)
            with self._lock:
                self._speaking = False

    def _overlay_on(self, lease: Lease, name: str, line1: str, face: Face) -> None:
        """走行より先に出す表示を足して出す。"""
        with self._lcd_lock:
            self._overlays = [o for o in self._overlays if o[0] != name]
            self._overlays.append((name, line1, face))
            self._lcd_dirty = True
            _display(lease, line1, face)

    def _overlay_off(self, lease: Lease, name: str) -> None:
        """表示を除く。残りがあればそれを出し、なければ繰り返しが走行の表示に戻す。"""
        with self._lcd_lock:
            self._overlays = [o for o in self._overlays if o[0] != name]
            self._lcd_dirty = True
            if not self._overlays:
                return
            _, line1, face = self._overlays[-1]
            try:
                _display(lease, line1, face)
            except LeaseRevoked:
                pass
            except Exception as e:
                logger.warning("LCD に出せませんでした: %s", type(e).__name__)

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


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    low, high = bounds
    return max(low, min(high, value))


def _display(lease: Lease, line1: str, face: Face) -> None:
    lease.display.show(center(line1), center(face.text()))
