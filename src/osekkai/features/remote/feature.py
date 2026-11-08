"""ラジコンの機能（RemoteFeature）。登録の申告、部品の組み立て、run の繰り返し。

要件 1.3、1.4、1.5、9.6、11.4、12.1、12.6、13.4、15.2。

- 無効なら何もしない。pin が使えない・Flask がない・ポートを使えないときは、
  理由をログに 1 回だけ出して、止める合図を待つ（ほかの機能は動き続ける）
- 時刻が確かなら、起動のときと日付が変わったときに写真を整理する（9.6）
- おやすみになったら、すぐ止め方を呼ぶ（11.4）
- web は run の中で読む（Flask がなくても、この機能を読み込める）
- ログには、決まった文と例外の型の名前だけを出す。pin・ポート・設定の値は出さない
"""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import ClassVar, Final

from osekkai.core.config import FeatureConfig
from osekkai.core.events import QuietChanged
from osekkai.core.feature import FeatureContext, FeatureSpec
from osekkai.core.paths import Paths
from osekkai.core.types import Priority, Resource

from .adapters.fake import ClearSensors, NoUsbCamera
from .adapters.sensors import HatSensors, sensors_available
from .adapters.usb_camera import UsbCamera, find_usb_camera, usb_camera_available
from .checks import (
    REASON_NO_FLASK,
    REASON_PIN_UNUSABLE,
    REASON_PORT_UNUSABLE,
    REMOTE_CHECKS,
    flask_available,
)
from .config import RemoteConfig
from .leases import LeaseKeeper
from .login import LoginGuard, Sessions
from .media import Camera, FrameHub, NeckSource, UsbSource
from .operator_slot import OperatorSlot
from .photos import PhotoStore, default_photo_dir
from .ports import SensorPort, UsbCameraPort
from .service import RemoteService

__all__ = ["TICK_S", "RemoteFeature"]

logger = logging.getLogger(__name__)

TICK_S: Final = 0.5  # 定期の確かめの間隔（実際の秒）
MSG_NOT_STARTED: Final = "ラジコンを始めません: %s"


@dataclass
class _Parts:
    """組み立てた部品。後始末のために持つ。"""

    service: RemoteService
    slot: OperatorSlot
    leases: LeaseKeeper
    hubs: dict[Camera, FrameHub]
    photos: PhotoStore
    guard: LoginGuard
    sessions: Sessions


class RemoteFeature:
    """ラジコンの機能。基盤は `RemoteFeature(ctx, config)` だけで作る。

    `sensors`・`usb`・`photo_dir`・`rng` は、テストで差し替える口。
    """

    spec: ClassVar[FeatureSpec] = FeatureSpec(
        name="remote",
        priority=Priority.REMOTE,
        resources=frozenset({Resource.DRIVE, Resource.NECK, Resource.VOICE, Resource.DISPLAY}),
        config_model=RemoteConfig,
        checks=REMOTE_CHECKS,
    )

    def __init__(
        self,
        ctx: FeatureContext,
        config: FeatureConfig,
        *,
        sensors: SensorPort | None = None,
        usb: UsbCameraPort | None = None,
        photo_dir: Path | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if not isinstance(config, RemoteConfig):
            raise TypeError("ラジコンの設定ではありません")
        self._ctx = ctx
        self._config = config
        self._sensors_given = sensors
        self._usb_given = usb
        self._photo_dir = photo_dir
        self._rng = rng if rng is not None else random.Random()
        self._parts: _Parts | None = None
        self._pruned_on: date | None = None

    # --- 基盤から呼ぶ口 ---

    def run(self, stop: threading.Event) -> None:
        """待ち受けを始め、止める合図まで定期の確かめを繰り返す。終わりに後始末をする。"""
        config = self._config
        if not config.enabled:
            stop.wait()
            return
        if not config.pin_usable():  # 1.4
            logger.warning(MSG_NOT_STARTED, REASON_PIN_UNUSABLE)
            stop.wait()
            return
        if not flask_available():
            logger.warning(MSG_NOT_STARTED, REASON_NO_FLASK)
            stop.wait()
            return
        sensors: SensorPort | None = None
        stop_web: Callable[[], None] | None = None
        try:
            try:
                sensors = self._make_sensors()
                parts = self._build(sensors)
                self._prune_if_due()  # 起動のとき（9.6）
                stop_web = self._start_web(parts)
            except Exception as e:  # 12.6。ほかの機能は動き続ける
                logger.warning("ラジコンを組み立てられませんでした: %s", type(e).__name__)
                stop_web = None
            if stop_web is None:
                self._close(sensors, None)  # 待つ前に返す
                sensors = None
                stop.wait()
                return
            self._ctx.subscribe(QuietChanged, self._on_quiet)  # 11.4
            logger.info("ラジコンの待ち受けを始めました")
            while not stop.wait(TICK_S):
                self._tick(parts)
        finally:
            self._close(sensors, stop_web)

    # --- 組み立て ---

    def _make_sensors(self) -> SensorPort:
        """渡されたセンサー、実機のセンサー、ライブラリがなければいつも安全な偽物（PC と CI）。"""
        if self._sensors_given is not None:
            return self._sensors_given
        if sensors_available():
            return HatSensors(clock=self._ctx.clock)
        logger.info("センサーのライブラリがないので、偽のセンサーを使います")
        return ClearSensors()

    def _make_usb(self) -> UsbCameraPort:
        """渡されたカメラ、見つかった C270、なければない USB カメラ。"""
        if self._usb_given is not None:
            return self._usb_given
        device = find_usb_camera(self._config.usb_camera)
        if device is not None and usb_camera_available():
            return UsbCamera(device, clock=self._ctx.clock)
        return NoUsbCamera()

    def _build(self, sensors: SensorPort) -> _Parts:
        ctx, config, clock = self._ctx, self._config, self._ctx.clock
        directory = self._photo_dir if self._photo_dir is not None else default_photo_dir(Paths())
        photos = PhotoStore(directory, clock)
        slot = OperatorSlot(clock)
        leases = LeaseKeeper(ctx, on_lost=self._on_lost)
        hubs = {
            Camera.NECK: FrameHub(Camera.NECK, NeckSource(may_borrow=self._neck_allowed), clock),
            Camera.USB: FrameHub(Camera.USB, UsbSource(self._make_usb()), clock),
        }
        service = RemoteService(ctx, config, sensors, hubs, photos, slot, leases, self._rng)
        sessions = Sessions(clock)
        sessions.on_end(service.drop_operator)  # 3.4
        parts = _Parts(
            service=service,
            slot=slot,
            leases=leases,
            hubs=hubs,
            photos=photos,
            guard=LoginGuard(config.pin, clock),
            sessions=sessions,
        )
        self._parts = parts
        return parts

    def _start_web(self, parts: _Parts) -> Callable[[], None] | None:
        """Web のサーバーを始め、止める口を返す。ポートを使えなければ理由を出して None（1.5）。"""
        from . import web  # Flask はここで初めて読む

        web_stop = threading.Event()
        app = web.create_app(
            service=parts.service,
            guard=parts.guard,
            sessions=parts.sessions,
            photos=parts.photos,
            hubs=parts.hubs,
            stop_event=web_stop,
        )
        server = web.WebServer(app, port=self._config.port, stop_event=web_stop)
        try:
            server.start()
        except OSError:
            logger.warning(MSG_NOT_STARTED, REASON_PORT_UNUSABLE)
            return None
        return server.stop

    # --- 繰り返しと知らせ ---

    def _tick(self, parts: _Parts) -> None:
        try:
            parts.service.tick()
        except Exception as e:  # 12.6
            logger.warning("定期の確かめで失敗しました: %s", type(e).__name__)
        self._prune_if_due()

    def _prune_if_due(self) -> None:
        """時刻が確かで、今日まだ整理していなければ写真を整理する（9.6）。"""
        parts, clock = self._parts, self._ctx.clock
        if parts is None or not clock.is_synchronized():
            return
        now = clock.now()
        if self._pruned_on == now.date():
            return
        self._pruned_on = now.date()
        try:
            parts.photos.prune(now, self._config.photo_days)
        except Exception as e:
            logger.warning("写真を整理できませんでした: %s", type(e).__name__)

    def _on_quiet(self, event: QuietChanged) -> None:
        parts = self._parts
        if event.quiet and parts is not None:
            parts.service.halt()

    def _on_lost(self) -> None:
        parts = self._parts
        if parts is not None:
            parts.service.on_lost()

    def _neck_allowed(self) -> bool:
        parts = self._parts
        return parts is not None and parts.service.neck_allowed()

    # --- 後始末 ---

    def _close(self, sensors: SensorPort | None, stop_web: Callable[[], None] | None) -> None:
        """Web を止め、サービスを閉じ、カメラとリースを返し、センサーを閉じる。"""
        parts, self._parts = self._parts, None
        steps: list[Callable[[], None]] = []
        if stop_web is not None:
            steps.append(stop_web)
        if parts is not None:
            steps.append(parts.service.close)
            steps.extend(hub.close for hub in parts.hubs.values())
            steps.append(parts.leases.release)
        if sensors is not None:
            steps.append(sensors.close)
        for step in steps:
            try:
                step()
            except Exception as e:  # 残りの後始末を続ける
                logger.warning("後始末で失敗しました: %s", type(e).__name__)
