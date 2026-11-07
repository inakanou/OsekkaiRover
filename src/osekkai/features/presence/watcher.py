"""見る流れ（撮る → 見つける → 追従 → 認識 → あいさつ、カメラの貸し出し）。

1.1、1.4、2.1、2.2、2.5、3.8、6.3、7.2〜7.7。

1 本の見るスレッドだけが `run_once` を回し、カメラと首を触る。ほかの機能のカメラの要求
（動きの量・フレーム・静止画）も待ち行列に入れ、見るスレッドが撮って返す（取り合いを避ける）。
貸し出しの状態（今の借り手・2 つ目を断る・止まった借り手）もここが持つ。
カメラを借りる口（api）は、ここの `CameraBusy`・`CameraNotAvailable`・`CameraNotBorrowed` を
そのまま出し直す。

首・目・あいさつの口（`PresenceIO`）は、機能の本体が借りたリースで作り、テストでは偽物にする。
ログには決まった文言・あいさつの種類・数・例外の型の名前だけを出す。
名前・呼び名・あいさつの言葉・LCD の文字は出さない（6.3）。
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Generic, Protocol, TypeVar

from osekkai.core.types import Face
from osekkai.features.presence import texts
from osekkai.features.presence.faces import Voting
from osekkai.features.presence.greeting import (
    LOOK_SECONDS,
    WAIT_RECOGNITION_SECONDS,
    record_key,
)
from osekkai.features.presence.ports import CameraUnavailable
from osekkai.features.presence.tracking import largest

if TYPE_CHECKING:
    import logging
    from datetime import datetime

    from osekkai.core.clock import Clock
    from osekkai.features.presence.config import PresenceConfig
    from osekkai.features.presence.faces import FaceBook
    from osekkai.features.presence.greeting import (
        GreetingDecision,
        GreetingPolicy,
        GreetingRecords,
    )
    from osekkai.features.presence.ports import CameraPort, Frame, VisionPort
    from osekkai.features.presence.tracking import FaceBox, Tracker, TrackOutput

T = TypeVar("T")

RECOGNIZE_INTERVAL: Final = 1.0  # 認識は 1 秒に 1 回まで（2.1）
GREET_RETRY_SECONDS: Final = 2.0  # 声と画面を借りられなかったとき、次に試すまでの秒数
MOTION_HZ: Final = 12.0  # 動きの量を見る回数（1 秒あたり）
MOTION_MAX_SECONDS: Final = 60.0  # 動きの量を求められる秒数の上限
FRAME_MAX_WIDTH: Final = 4096  # フレームの幅の上限の上限

MSG_CAMERA_BUSY = "ほかの機能がカメラを借りています"
MSG_CAMERA_NOT_AVAILABLE = "カメラが使えません"
MSG_CAMERA_NOT_BORROWED = "カメラを借りていません"
MSG_TIMEOUT = "カメラの要求が時間内に終わりませんでした"
MSG_MOTION_SECONDS = f"動きの量の秒数は 0 より大きく {MOTION_MAX_SECONDS:g} 以下にしてください"
MSG_FRAME_WIDTH = f"フレームの幅は 1 から {FRAME_MAX_WIDTH} にしてください"
MSG_FRAME_QUALITY = "フレームの品質は 1 から 100 にしてください"


class CameraBusy(Exception):
    """ほかの機能がカメラを借りている。"""

    def __init__(self) -> None:
        super().__init__(MSG_CAMERA_BUSY)


class CameraNotAvailable(Exception):
    """カメラが使えない（カメラがない・使えなくなった・presence が動いていない。7.8）。"""

    def __init__(self) -> None:
        super().__init__(MSG_CAMERA_NOT_AVAILABLE)


class CameraNotBorrowed(Exception):
    """カメラを借りていない機能の要求（返した後・止まった後を含む。7.6、7.7）。"""

    def __init__(self) -> None:
        super().__init__(MSG_CAMERA_NOT_BORROWED)


class PresenceIO(Protocol):
    """首・目・あいさつの口。機能の本体が借りたリースで作る。"""

    def look(self, pan: float, tilt: float) -> None:
        """首を向ける（度）。"""
        ...

    def set_idle_face(self, face: Face, gaze: float) -> None:
        """待機画面の顔と目の向き（−1〜1）を変える。"""
        ...

    def greet(self, lcd_line1: str, face_text: str, speech: str) -> bool:
        """声と画面をそのときだけ借り、LCD に出してから話す（3.8）。借りられなければ False。"""
        ...


class LoanToken:
    """貸し出しの札。借りた機能だけが持つ。中身は見ない。"""

    __slots__ = ("owner",)

    def __init__(self, owner: str) -> None:
        self.owner = owner


class LoanTicket(Generic[T]):
    """貸し出しの要求の結果を待つ札。見るスレッドが結果か例外を入れる。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._value: T | None = None
        self._error: Exception | None = None

    @property
    def done(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None) -> T:
        """結果を待って返す。時間切れなら `TimeoutError`、失敗ならその例外を出す。"""
        if not self._event.wait(timeout):
            raise TimeoutError(MSG_TIMEOUT)
        if self._error is not None:
            raise self._error
        return self._value  # type: ignore[return-value]

    def _resolve(self, value: T) -> None:
        if not self._event.is_set():
            self._value = value
            self._event.set()

    def _fail(self, error: Exception) -> None:
        if not self._event.is_set():
            self._error = error
            self._event.set()


@dataclass(frozen=True)
class _Loan:
    lent: LoanToken
    alive: Callable[[], bool]


@dataclass(frozen=True)
class _Pending:
    lent: LoanToken
    ticket: LoanTicket[Any]
    action: Callable[[], Any]


def _peak_motion(values: list[float]) -> float:
    """直近の 2 つの小さい方の最大（1 回だけの大きな値は数えない）。0〜1 に収める。"""
    if not values:
        return 0.0
    if len(values) == 1:
        peak = values[0]
    else:
        peak = max(min(a, b) for a, b in zip(values, values[1:], strict=False))
    return max(0.0, min(1.0, peak))


class Watcher:
    """見る流れ。`run_once` を見るスレッドから 1 回ずつ回す。

    カメラか映像の処理がなければ（PC など）何もせず、貸し出しもしない。
    カメラが使えなくなったら `CameraUnavailable` を出し、その後は何もしない
    （機能の本体が見ることを止める）。
    """

    def __init__(
        self,
        camera: CameraPort | None,
        vision: VisionPort | None,
        book: FaceBook,
        records: GreetingRecords,
        policy: GreetingPolicy,
        tracker: Tracker,
        config: PresenceConfig,
        clock: Clock,
        io: PresenceIO,
        log: logging.Logger,
    ) -> None:
        self._camera = camera
        self._vision = vision
        self._book = book
        self._records = records
        self._policy = policy
        self._tracker = tracker
        self._config = config
        self._clock = clock
        self._io = io
        self._log = log
        # 貸し出しの状態（ほかのスレッドからも触るので、鍵の中で読み書きする）
        self._lock = threading.Lock()
        self._available = camera is not None and vision is not None
        self._loan: _Loan | None = None
        self._pending: list[_Pending] = []
        # 見るスレッドだけが触る状態
        self._camera_started = False
        self._was_loaned = False
        self._sent_look: tuple[float, float] | None = None
        self._sent_idle: tuple[Face, float] | None = None
        self._voting = Voting()
        self._who: str | None = None
        self._seen_since: float | None = None
        self._recognized_at: float | None = None
        self._greet_retry_at: float | None = None
        self._greeted_in_sighting = False

    # --- 状態 ---

    @property
    def available(self) -> bool:
        """カメラが使えるか。"""
        with self._lock:
            return self._available

    @property
    def loaned(self) -> bool:
        """ほかの機能がカメラを借りているか。"""
        with self._lock:
            return self._loan is not None

    # --- 見るスレッドの回り ---

    def run_once(self) -> None:
        """1 回の回り。借りられていれば要求を行い、そうでなければ見る。"""
        camera, vision = self._camera, self._vision
        if camera is None or vision is None or not self.available:
            return
        self._check_borrower()
        if self.loaned:
            if not self._was_loaned:
                self._was_loaned = True
                self._reset_watch()
            self._serve(camera)
            return
        self._was_loaned = False
        try:
            self._watch(camera, vision)
        except CameraUnavailable:
            self._mark_unavailable()
            raise

    def pause(self) -> None:
        """カメラを止め、見ていた状態を戻す（静かな状態・中断のとき）。次の回りでまた始める。"""
        if self._camera is not None and self._camera_started:
            self._camera_started = False
            self._camera.stop()
            self._log.info("カメラを止めました")
        self._reset_watch()

    def close(self) -> None:
        """機能が止まる。貸し出しを終えて残った要求に使えないと知らせ、カメラを止める。

        閉じた後は、貸し出しも要求も `CameraNotAvailable` で断る。見るスレッドから呼ぶ。
        """
        with self._lock:
            self._available = False
            loan = self._loan
            ended = loan is not None and self._end_locked(loan.lent, CameraNotAvailable)
        if ended:
            self._log.info("見つけてあいさつが止まるので、カメラの貸し出しを終えました")
        self.pause()

    def _ensure_started(self, camera: CameraPort) -> None:
        if not self._camera_started:
            camera.start()
            self._camera_started = True
            self._log.info("カメラを始めました")

    def _watch(self, camera: CameraPort, vision: VisionPort) -> None:
        self._ensure_started(camera)
        frame = camera.capture()
        face = largest(vision.detect(frame))
        mono = self._clock.monotonic()
        out = self._tracker.step(face, mono)
        self._send(out)
        if not out.seen:
            if self._seen_since is not None:
                self._log.debug("顔を見失いました")
                self._reset_session()
            return
        if self._seen_since is None:
            self._log.debug("顔を見つけました")
            self._seen_since = mono
        has_known = bool(self._book.people())
        if face is not None and has_known:
            self._recognize(vision, frame, face, mono)
        self._consider_greeting(mono, has_known)

    def _send(self, out: TrackOutput) -> None:
        look = (out.pan, out.tilt)
        if look != self._sent_look:
            self._io.look(*look)
            self._sent_look = look
        idle = (Face.HAPPY if out.seen else Face.NORMAL, out.gaze)
        if idle != self._sent_idle:
            self._io.set_idle_face(*idle)
            self._sent_idle = idle

    def _recognize(self, vision: VisionPort, frame: Frame, face: FaceBox, mono: float) -> None:
        last = self._recognized_at
        if last is not None and mono - last < RECOGNIZE_INTERVAL:
            return
        self._recognized_at = mono
        emb = vision.embed(frame, face)
        # 認識のモデルがなければ知らない人として 1 票（2.5）
        match = None if emb is None else self._book.best_match(emb, self._config.match_threshold)
        decided = self._voting.add(match)
        if decided is not None and self._who is None:
            self._who = decided  # 一度決まった人は、見失うまで変えない
            self._log.debug("覚えた人と分かりました")

    def _consider_greeting(self, mono: float, has_known: bool) -> None:
        clock = self._clock
        synced = clock.is_synchronized()
        if not synced:
            return  # 時刻が確かでない間は、あいさつも見た記録もしない（4.5）
        now = clock.now()
        seen_since = self._seen_since
        looked_for = 0.0 if seen_since is None else mono - seen_since
        who = self._who
        waiting = looked_for if who is None else 0.0
        if self._greeted_in_sighting:
            # 1 回見つけている間のあいさつは 1 回だけ（知らない人としてあいさつした後に
            # 誰か分かっても、もう一度あいさつしない）。見た記録だけを続ける
            if looked_for >= LOOK_SECONDS:
                self._records.mark_seen(record_key(who), now)
            return
        decision = self._policy.decide(
            now,
            synced=synced,
            who=who,
            looked_for=looked_for,
            waiting_recognition=waiting,
            has_known_people=has_known,
            records=self._records,
        )
        if decision is not None:
            self._greet(decision, mono, now)
            return
        # あいさつの判定が済んだ後（見続けて、誰かが分かったか待ち終えた後）だけ、
        # 見た記録を更新する。
        # 判定の前に更新すると、見えなかった時間が 0 になってあいさつしなくなる
        settled = looked_for >= LOOK_SECONDS and not (
            has_known and who is None and waiting < WAIT_RECOGNITION_SECONDS
        )
        if settled:
            self._records.mark_seen(record_key(who), now)

    def _greet(self, decision: GreetingDecision, mono: float, now: datetime) -> None:
        retry_at = self._greet_retry_at
        if retry_at is not None and mono < retry_at:
            return
        call_name = None
        if decision.who is not None:
            call_name = next(
                (p.call_name for p in self._book.people() if p.person_id == decision.who), None
            )
        speech = self._policy.phrase(decision, call_name)
        if self._io.greet(texts.LCD_FOUND, Face.HAPPY.text(), speech):
            self._greet_retry_at = None
            self._greeted_in_sighting = True
            self._records.mark_greeted(decision, now)
            self._log.info("あいさつしました: %s", decision.kind.value)
        else:
            # 借りられなければ、あいさつも記録もしない。少し後にまた試す
            self._greet_retry_at = mono + GREET_RETRY_SECONDS
            self._log.info("声と画面を借りられないので、あいさつしませんでした")

    def _reset_session(self) -> None:
        self._voting.reset()
        self._who = None
        self._seen_since = None
        self._recognized_at = None
        self._greet_retry_at = None
        self._greeted_in_sighting = False

    def _reset_watch(self) -> None:
        self._reset_session()
        self._sent_look = None  # 止めている間に、ほかの機能が首を動かしたかもしれない
        if self._sent_idle is not None and self._sent_idle != (Face.NORMAL, 0.0):
            self._io.set_idle_face(Face.NORMAL, 0.0)
            self._sent_idle = (Face.NORMAL, 0.0)

    # --- カメラの貸し出し（7.2〜7.7） ---

    def start_loan(self, owner: str, alive: Callable[[], bool]) -> LoanToken:
        """カメラを貸す。借りている間は見ない。

        カメラが使えなければ `CameraNotAvailable`、ほかの機能が借りていれば `CameraBusy`。
        前の借り手が止まっていれば、その貸し出しを終えてから貸す（7.7）。
        """
        with self._lock:
            if not self._available:
                raise CameraNotAvailable()
            current = self._loan
        if current is not None and self._is_alive(current):
            raise CameraBusy()
        with self._lock:
            if not self._available:
                raise CameraNotAvailable()
            if self._loan is not current:
                raise CameraBusy()  # 確かめている間に、ほかの機能が借りた
            if current is not None:
                self._end_locked(current.lent, CameraNotBorrowed)
                self._log.info("カメラを借りた機能が止まったので、貸し出しを終えました")
            lent = LoanToken(owner)
            self._loan = _Loan(lent, alive)
        self._log.info("カメラを貸しました: %s", owner)
        return lent

    def end_loan(self, lent: LoanToken) -> None:
        """カメラを返す。返した後・借りていない札では何もしない。"""
        with self._lock:
            ended = self._end_locked(lent, CameraNotBorrowed)
        if ended:
            self._log.info("カメラが返されました: %s", lent.owner)

    def request_motion(self, lent: LoanToken, seconds: float) -> LoanTicket[float]:
        """`seconds` 秒の間の映像の変化の大きさ（0〜1）を求める（7.3）。"""
        if not (math.isfinite(seconds) and 0.0 < seconds <= MOTION_MAX_SECONDS):
            raise ValueError(MSG_MOTION_SECONDS)
        return self._enqueue(lent, lambda: self._measure_motion(seconds))

    def request_frame(
        self, lent: LoanToken, max_width: int = 640, quality: int = 70
    ) -> LoanTicket[bytes]:
        """いまの映像の 1 枚を JPEG で求める（上下の反転はアダプタが当てる。7.4）。"""
        if isinstance(max_width, bool) or not 1 <= max_width <= FRAME_MAX_WIDTH:
            raise ValueError(MSG_FRAME_WIDTH)
        if isinstance(quality, bool) or not 1 <= quality <= 100:
            raise ValueError(MSG_FRAME_QUALITY)
        return self._enqueue(lent, lambda: self._take_frame(max_width, quality))

    def request_still(self, lent: LoanToken) -> LoanTicket[bytes]:
        """カメラの最大の大きさの静止画を JPEG で求める（保存しない。7.5）。"""
        return self._enqueue(lent, self._take_still)

    def _enqueue(self, lent: LoanToken, action: Callable[[], T]) -> LoanTicket[T]:
        ticket: LoanTicket[T] = LoanTicket()
        with self._lock:
            if not self._available:
                raise CameraNotAvailable()
            if self._loan is None or self._loan.lent is not lent:
                raise CameraNotBorrowed()  # 借りていない機能は断る（7.6）
            self._pending.append(_Pending(lent, ticket, action))
        return ticket

    def _check_borrower(self) -> None:
        with self._lock:
            current = self._loan
        if current is None or self._is_alive(current):
            return
        with self._lock:
            ended = self._loan is current and self._end_locked(current.lent, CameraNotBorrowed)
        if ended:
            self._log.info("カメラを借りた機能が止まったので、貸し出しを終えました")

    def _is_alive(self, loan: _Loan) -> bool:
        try:
            return bool(loan.alive())
        except Exception as error:
            # 確かめられなければ、止まったとする
            self._log.warning(
                "カメラを借りた機能を確かめられませんでした: %s", type(error).__name__
            )
            return False

    def _end_locked(self, lent: LoanToken, error: Callable[[], Exception]) -> bool:
        """鍵の中で呼ぶ。その札の貸し出しを終え、残った要求を断る。終えたら True。"""
        if self._loan is None or self._loan.lent is not lent:
            return False
        self._loan = None
        for pending in self._pending:
            pending.ticket._fail(error())
        self._pending.clear()
        return True

    def _serve(self, camera: CameraPort) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    return
                pending = self._pending.pop(0)
                current = self._loan
            if current is None or current.lent is not pending.lent:
                pending.ticket._fail(CameraNotBorrowed())
                continue
            try:
                self._ensure_started(camera)
                pending.ticket._resolve(pending.action())
            except CameraUnavailable:
                pending.ticket._fail(CameraNotAvailable())
                self._mark_unavailable()
                raise
            except Exception as error:
                self._log.warning("カメラの要求に失敗しました: %s", type(error).__name__)
                pending.ticket._fail(error)

    def _mark_unavailable(self) -> None:
        """カメラが使えなくなった。貸し出しを終え、残った要求に知らせる（7.8）。"""
        with self._lock:
            self._available = False
            loan = self._loan
            if loan is not None:
                self._end_locked(loan.lent, CameraNotAvailable)
        self._camera_started = False
        self._log.warning("カメラが使えなくなったので、見るのを止めます")

    # --- 要求の中身（見るスレッドで行う） ---

    def _require(self) -> tuple[CameraPort, VisionPort]:
        camera, vision = self._camera, self._vision
        if camera is None or vision is None:
            raise CameraNotAvailable()
        return camera, vision

    def _measure_motion(self, seconds: float) -> float:
        camera, vision = self._require()
        clock = self._clock
        deadline = clock.monotonic() + seconds
        before = camera.capture()
        values: list[float] = []
        while True:
            clock.sleep(1.0 / MOTION_HZ)
            after = camera.capture()
            values.append(vision.motion(before, after))
            before = after
            if clock.monotonic() >= deadline:
                return _peak_motion(values)

    def _take_frame(self, max_width: int, quality: int) -> bytes:
        camera, vision = self._require()
        return vision.to_jpeg(camera.capture(), max_width, quality)

    def _take_still(self) -> bytes:
        camera, _ = self._require()
        return camera.capture_still()
