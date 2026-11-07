"""見つけてあいさつの機能の本体（4.1〜4.4、4.6、4.7、8.5）。

- 優先度は見守りの帯（ほかのどの機能よりも低い）で、首・声・画面を使う（4.1）
- 首は待たずに借りる。借りられなければ、間をおいて（既定は 2 秒）また試す。
  中断を知らされたら、知らせのスレッドで直ちに首を返し、見るスレッドは見るのをやめて
  カメラを止める（4.2、4.3、4.6）。回りの途中で止まっていても、取り上げを待たない
- 首は借りている間だけ動かす（4.7）。静かな状態の間は、カメラを止めて首を返す（4.4）
- あいさつの声と画面は、そのときだけ待たずに借りる。借りられなければあいさつしない
- ほかの機能がカメラを借りている間は、首を借りていなくても見る流れを回す
  （貸し出しの要求は見るスレッドが行うため。静かな状態の間も答える）
- 1 回の回りの例外は、型の名前だけを記録して続ける。カメラが使えなくなったら、
  首を返して見ることを止め、止める旗を待つ（8.5）
- カメラのライブラリかモデルがなければ（PC など）、資源を 1 つも借りずに止める旗を待つ
- 動いている見る流れを、カメラを借りる口（api）の置き場に、始めに置き、終わりに外す
- ログには決まった文言と例外の型の名前だけを出す。呼び名・あいさつの言葉・LCD の文字は出さない
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import ClassVar, Final

from osekkai.core.arbiter import Lease
from osekkai.core.config import FeatureConfig
from osekkai.core.feature import FeatureContext, FeatureSpec
from osekkai.core.lcd_text import center
from osekkai.core.paths import Paths
from osekkai.core.types import (
    DeviceUnavailable,
    Face,
    LeaseRevoked,
    Priority,
    Resource,
    ResourceBusy,
    SpeechFailed,
)
from osekkai.features.presence import api
from osekkai.features.presence.adapters.opencv import (
    OpenCvVision,
    VisionUnavailable,
    opencv_available,
)
from osekkai.features.presence.adapters.picamera import PiCamera, picamera_available
from osekkai.features.presence.checks import PRESENCE_CHECKS
from osekkai.features.presence.config import PresenceConfig, model_paths
from osekkai.features.presence.faces import FaceBook
from osekkai.features.presence.greeting import GreetingPolicy, GreetingRecords
from osekkai.features.presence.ports import CameraPort, CameraUnavailable, VisionPort
from osekkai.features.presence.tracking import Tracker
from osekkai.features.presence.watcher import Watcher

__all__ = ["LeasePresenceIO", "PresenceFeature", "choose_devices"]

RESOURCES: Final = frozenset({Resource.NECK, Resource.VOICE, Resource.DISPLAY})
GREET_RESOURCES: Final = frozenset({Resource.VOICE, Resource.DISPLAY})
NECK_RETRY_S: Final = 2.0  # 首を借りられなかったとき、次に試すまでの秒数
LOOP_INTERVAL_S: Final = 0.1  # 見る流れの回りの間（秒）
FAILURE_LOG_EVERY: Final = 600  # 同じ失敗が続くとき、まとめて記録する回り（0.1 秒ごとで約 1 分）

MSG_CONFIG_TYPE = "設定のモデルは PresenceConfig にしてください"


def choose_devices(config: PresenceConfig) -> tuple[CameraPort | None, VisionPort | None]:
    """カメラと映像の処理を選ぶ。

    picamera2 と cv2 を読み込め、顔の検出のモデルがあれば実機のアダプタ。
    そうでなければ、どちらも None（カメラなし。何もしない）。
    """
    if not (picamera_available() and opencv_available()):
        return None, None
    try:
        vision = OpenCvVision(*model_paths(Paths()))
    except VisionUnavailable:
        return None, None
    return PiCamera(flip=config.flip), vision


def _acquire_releasing(ctx: FeatureContext, resources: Iterable[Resource]) -> Lease:
    """待たずに借りる。中断を知らされたら、知らせのスレッドで直ちに返す（4.2）。

    見るスレッドが回りの途中（あいさつの発話・カメラの貸し出しの要求）で止まっていても、
    猶予の後に取り上げられる前に返せる。見るスレッドは、次の回りでリースが終わったことに気づく。
    借りが返ってくる前に知らせが来たときは、返ってきたところで返す。
    `ResourceBusy` はそのまま出す。
    """
    cell: list[Lease] = []
    lock = threading.Lock()

    def on_preempt() -> None:
        with lock:
            leases = list(cell)
        for lease in leases:
            lease.release()

    lease = ctx.acquire(resources, wait=0.0, on_preempt=on_preempt)
    with lock:
        cell.append(lease)
    if lease.preempt_requested.is_set():
        lease.release()
    return lease


class LeasePresenceIO:
    """首・目・あいさつの口（`PresenceIO`）を、機能が借りたリースで作る。

    首は、機能の本体が渡した首のリースが有効で、中断を知らされていない間だけ動かす（4.2、4.7）。
    あいさつは、声と画面をそのときだけ待たずに借り、LCD に出してから話して、すぐに返す（3.8）。
    どちらのリースも、中断を知らされたら直ちに返す（話す前なら、あいさつしない）。
    話せたときだけ True を返す。借りられない・話せない・取り上げられたときは False
    （見る流れは、あいさつも記録もせず、少し後にまた試す）。
    """

    def __init__(self, ctx: FeatureContext) -> None:
        self._ctx = ctx
        self._log = ctx.log
        self._neck: Lease | None = None

    def bind_neck(self, lease: Lease | None) -> None:
        """首のリースを渡す（返したときは None）。"""
        self._neck = lease

    def look(self, pan: float, tilt: float) -> None:
        lease = self._neck
        if lease is None or not lease.active or lease.preempt_requested.is_set():
            return  # 首を借りていない・返す途中のときは動かさない
        try:
            lease.neck.look(pan, tilt)
        except LeaseRevoked:
            # 確かめた後に、中断の知らせで首を返した。動かさないでよい
            self._log.debug("首を返した後なので、動かしませんでした")

    def set_idle_face(self, face: Face, gaze: float) -> None:
        self._ctx.set_idle_face(face, gaze)

    def greet(self, lcd_line1: str, face_text: str, speech: str) -> bool:
        try:
            lease = _acquire_releasing(self._ctx, GREET_RESOURCES)
        except ResourceBusy:
            return False
        try:
            lease.display.show(center(lcd_line1), center(face_text))
            lease.voice.say(speech)
            return True
        except (SpeechFailed, DeviceUnavailable, LeaseRevoked) as error:
            self._log.warning("あいさつできませんでした: %s", type(error).__name__)
            return False
        finally:
            lease.release()


class PresenceFeature:
    """顔を見つけて首で追い、あいさつする機能。"""

    spec: ClassVar[FeatureSpec] = FeatureSpec(
        name="presence",
        priority=Priority.BACKGROUND,
        resources=RESOURCES,
        config_model=PresenceConfig,
        checks=PRESENCE_CHECKS,
    )

    def __init__(
        self,
        ctx: FeatureContext,
        config: FeatureConfig,
        *,
        camera: CameraPort | None = None,
        vision: VisionPort | None = None,
        neck_retry: float = NECK_RETRY_S,
        loop_interval: float = LOOP_INTERVAL_S,
    ) -> None:
        """`camera` と `vision` をどちらも渡さなければ、`choose_devices` で選ぶ。

        `neck_retry`（首を借り直す秒数）と `loop_interval`（回りの間の秒数）は実際の秒数で、
        テストで短くするためのもの。
        """
        if not isinstance(config, PresenceConfig):
            raise TypeError(MSG_CONFIG_TYPE)
        self._ctx = ctx
        self._config = config
        self._log = ctx.log
        if camera is None and vision is None:
            camera, vision = choose_devices(config)
        self._has_camera = camera is not None and vision is not None
        self._retry = neck_retry
        self._interval = loop_interval
        self._io = LeasePresenceIO(ctx)
        self._neck: Lease | None = None
        self._watcher = Watcher(
            camera,
            vision,
            FaceBook.load(ctx.store),
            GreetingRecords.load(ctx.store),
            GreetingPolicy(config),
            Tracker(
                pan_reverse=config.pan_reverse,
                tilt_reverse=config.tilt_reverse,
                gaze_reverse=config.gaze_reverse,
            ),
            config,
            ctx.clock,
            self._io,
            self._log,
        )

    @property
    def has_camera(self) -> bool:
        """カメラと映像の処理があるか（なければ何もしない）。"""
        return self._has_camera

    def run(self, stop: threading.Event) -> None:
        watcher = self._watcher
        api.register_watcher(watcher)
        try:
            if not self._has_camera:
                self._log.info("カメラがないので、見つけてあいさつは何もしません")
                stop.wait()
                return
            self._loop(stop)
        finally:
            self._safely_close()
            self._release_neck()
            api.clear_watcher(watcher)

    # --- 内側（見るスレッド） ---

    def _loop(self, stop: threading.Event) -> None:
        retry_left = 0.0
        failures = 0  # 続けて失敗した回り
        while not stop.is_set():
            try:
                retry_left = self._step(retry_left)
                if failures:
                    self._log.info("見る回りが直りました（続けて失敗した回り: %d）", failures)
                    failures = 0
            except CameraUnavailable as error:
                self._log.warning(
                    "カメラが使えないので、見つけてあいさつを止めます: %s", type(error).__name__
                )
                self._release_neck()
                stop.wait()
                return
            except Exception as error:
                failures += 1
                # 同じ失敗が続いても、最初の 1 回と決まった回数ごとのまとめだけを記録する
                if failures == 1 or failures % FAILURE_LOG_EVERY == 0:
                    self._log.warning(
                        "見る回りに失敗しました: %s（続けて %d 回）", type(error).__name__, failures
                    )
            stop.wait(self._interval)

    def _step(self, retry_left: float) -> float:
        """1 回の回り。次に首を借りに行くまでの残りの秒数を返す。"""
        watcher = self._watcher
        lease = self._neck
        if lease is not None and (not lease.active or lease.preempt_requested.is_set()):
            # 中断の知らせで返した（か取り上げられた）: 見るのをやめて、カメラを止める（4.2）
            watcher.pause()
            self._release_neck()
            self._log.info("中断を知らされたので、見るのをやめました")
            lease = None
            retry_left = self._retry
        if watcher.loaned:
            watcher.run_once()  # 貸し出しの要求を行う（首はいらない）
            return retry_left
        if self._ctx.is_quiet:
            watcher.pause()  # カメラを止める（4.4）
            self._release_neck()
            return retry_left
        if lease is None:
            if retry_left > 0.0:
                watcher.pause()
                return retry_left - self._interval
            if not self._acquire_neck():
                watcher.pause()
                return self._retry
        watcher.run_once()
        return 0.0

    def _acquire_neck(self) -> bool:
        try:
            lease = _acquire_releasing(self._ctx, {Resource.NECK})
        except ResourceBusy:
            return False
        self._neck = lease
        self._io.bind_neck(lease)
        self._log.info("首を借りました")
        return True

    def _release_neck(self) -> None:
        lease, self._neck = self._neck, None
        self._io.bind_neck(None)
        if lease is not None:
            lease.release()

    def _safely_close(self) -> None:
        try:
            self._watcher.close()
        except Exception as error:
            self._log.warning("カメラを止められませんでした: %s", type(error).__name__)
