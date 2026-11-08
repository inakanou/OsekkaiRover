"""ラジコンの機能（組み立て・待ち受け・定期の確かめ・後始末）のテスト。

要件 1.3、1.4、1.5、9.6、11.4、12.1、12.6、15.2。
偽の文脈・台本のセンサー・偽の USB カメラで組み、Web は本物のポートで待ち受ける。
"""

from __future__ import annotations

import logging
import random
import socket
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
from remote_fakes import FakeClock, FakeContext, FakeUsbCamera, ScriptedSensors

from osekkai.core.config import FeatureConfig
from osekkai.core.events import QuietChanged
from osekkai.core.types import Priority, Resource
from osekkai.features.remote import RemoteFeature
from osekkai.features.remote import feature as feature_module
from osekkai.features.remote.adapters.fake import ClearSensors, NoUsbCamera, UnreadableSensors
from osekkai.features.remote.checks import (
    REASON_NO_FLASK,
    REASON_PIN_UNUSABLE,
    REASON_PORT_UNUSABLE,
    REMOTE_CHECKS,
)
from osekkai.features.remote.config import RemoteConfig
from osekkai.features.remote.drive_rules import Move
from osekkai.features.remote.photos import PhotoStore
from osekkai.features.remote.service import RemoteService, Reply

PIN: Final = "4821"
LOOPBACK: Final = "127.0.0.1"
WAIT_S: Final = 5.0  # 別のスレッドを待つ上限（実際の秒）
STOP_S: Final = 2.0  # 止める合図から run が戻るまでの上限
FEATURE_LOGGER: Final = "osekkai.features.remote"


def _free_port() -> int:
    with socket.create_server(("0.0.0.0", 0)) as sock:
        port: int = sock.getsockname()[1]
    return port


def _wait(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + WAIT_S
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("待ちが終わりませんでした")
        time.sleep(0.01)


def _web_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "remote-web"]


def _serving(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{LOOPBACK}:{port}/app.css", timeout=1.0) as r:
            return bool(r.status == 200)
    except OSError:
        return False


class _Rig:
    """機能を組み、別のスレッドで run を動かす。"""

    def __init__(self, tmp_path: Path, **config: object) -> None:
        self.clock = FakeClock()
        self.ctx = FakeContext(self.clock)
        self.sensors = ScriptedSensors()
        self.usb = FakeUsbCamera(can_open=False)
        self.photo_dir = tmp_path / "photos"
        values: dict[str, object] = {"enabled": True, "pin": PIN, "port": _free_port()}
        values.update(config)
        self.config = RemoteConfig.model_validate(values)
        self.feature = RemoteFeature(
            self.ctx.as_context(),
            self.config,
            sensors=self.sensors,
            usb=self.usb,
            photo_dir=self.photo_dir,
            rng=random.Random(0),
        )
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.feature.run, args=(self.stop,), daemon=True)

    def start(self) -> None:
        self.thread.start()

    def finish(self) -> float:
        """止める合図を出し、run が戻るまでの秒を返す。"""
        started = time.monotonic()
        self.stop.set()
        self.thread.join(WAIT_S)
        assert not self.thread.is_alive(), "run が戻りませんでした"
        return time.monotonic() - started

    def url(self, path: str) -> str:
        return f"http://{LOOPBACK}:{self.config.port}{path}"


@pytest.fixture
def rigs() -> Iterator[list[_Rig]]:
    made: list[_Rig] = []
    yield made
    for rig in made:
        if rig.thread.is_alive():
            rig.finish()


@pytest.fixture
def make(tmp_path: Path, rigs: list[_Rig]) -> Callable[..., _Rig]:
    def factory(**config: object) -> _Rig:
        rig = _Rig(tmp_path, **config)
        rigs.append(rig)
        return rig

    return factory


@pytest.fixture
def fast_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feature_module, "TICK_S", 0.01)


def _count_messages(caplog: pytest.LogCaptureFixture, text: str) -> int:
    return sum(1 for r in caplog.records if text in r.getMessage())


# --- 申告（12.1、13.4） ---


def test_申告は名前と優先度と4つの資源と設定と_self_check() -> None:
    spec = RemoteFeature.spec
    assert spec.name == "remote"
    assert spec.priority == Priority.REMOTE
    assert spec.resources == frozenset(
        {Resource.DRIVE, Resource.NECK, Resource.VOICE, Resource.DISPLAY}
    )
    assert spec.config_model is RemoteConfig
    assert spec.checks == REMOTE_CHECKS


def test_ラジコンの設定でなければ作らない() -> None:
    with pytest.raises(TypeError):
        RemoteFeature(FakeContext().as_context(), FeatureConfig())


# --- 待ち受けない場合（1.4、1.5） ---


def test_無効なら待ち受けずログも出さず_止めればすぐ戻る(
    make: Callable[..., _Rig], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=FEATURE_LOGGER)
    rig = make(enabled=False)
    rig.start()
    time.sleep(0.2)
    assert _web_threads() == []
    assert not _serving(rig.config.port)
    assert rig.finish() <= STOP_S
    assert caplog.records == []
    assert rig.ctx.calls == []


@pytest.mark.parametrize("pin", ["", "12a4", "123"])
def test_pin_が使えなければ理由を1回だけ出して待ち受けない(
    make: Callable[..., _Rig], caplog: pytest.LogCaptureFixture, pin: str
) -> None:
    caplog.set_level(logging.INFO, logger=FEATURE_LOGGER)
    rig = make(pin=pin)
    rig.start()
    _wait(lambda: _count_messages(caplog, REASON_PIN_UNUSABLE) == 1)
    time.sleep(0.2)
    assert _count_messages(caplog, REASON_PIN_UNUSABLE) == 1
    assert _web_threads() == []
    assert not _serving(rig.config.port)
    assert rig.thread.is_alive()
    assert rig.finish() <= STOP_S
    assert pin == "" or pin not in caplog.text


def test_flask_がなければ理由を1回だけ出して待ち受けない(
    make: Callable[..., _Rig],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.INFO, logger=FEATURE_LOGGER)
    monkeypatch.setattr(feature_module, "flask_available", lambda: False)
    rig = make()
    rig.start()
    _wait(lambda: _count_messages(caplog, REASON_NO_FLASK) == 1)
    time.sleep(0.2)
    assert _count_messages(caplog, REASON_NO_FLASK) == 1
    assert _web_threads() == []
    assert rig.thread.is_alive()
    assert rig.finish() <= STOP_S


def test_ポートを使えなければ理由を1回だけ出して待ち_後始末をする(
    make: Callable[..., _Rig], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=FEATURE_LOGGER)
    rig = make()
    with socket.create_server(("0.0.0.0", rig.config.port)):
        rig.start()
        _wait(lambda: _count_messages(caplog, REASON_PORT_UNUSABLE) == 1)
        time.sleep(0.2)
        assert _count_messages(caplog, REASON_PORT_UNUSABLE) == 1
        assert _web_threads() == []
        assert rig.thread.is_alive()  # ほかの機能を止めない（1.5）
        assert rig.sensors.closed
        assert rig.finish() <= STOP_S
    assert str(rig.config.port) not in caplog.text


# --- 待ち受けと後始末（1.3、15.2） ---


def test_設定したポートで待ち受け_止めればすぐ戻って閉じる(
    make: Callable[..., _Rig], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=FEATURE_LOGGER)
    rig = make()
    rig.start()
    _wait(lambda: _serving(rig.config.port))
    threads = _web_threads()
    assert len(threads) == 1
    assert threads[0].daemon
    assert rig.finish() <= STOP_S
    assert _web_threads() == []
    assert not _serving(rig.config.port)
    assert rig.sensors.closed
    assert PIN not in caplog.text


def test_止めるとリースを返す(make: Callable[..., _Rig], fast_tick: None) -> None:
    rig = make()
    rig.start()
    _wait(lambda: _serving(rig.config.port))
    parts = rig.feature._parts
    assert parts is not None
    token = parts.sessions.start()
    assert parts.service.drive(token, Move.FWD) is Reply.OK
    _wait(lambda: parts.leases.lease is not None)
    rig.finish()
    assert parts.leases.lease is None
    assert all(not lease.active for lease in rig.ctx.leases)


def test_定期の確かめを繰り返し呼ぶ(
    make: Callable[..., _Rig], fast_tick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks: list[int] = []
    monkeypatch.setattr(RemoteService, "tick", lambda self: ticks.append(1))
    rig = make()
    rig.start()
    _wait(lambda: len(ticks) >= 3)
    rig.finish()


def test_定期の確かめで思わぬ誤りが出ても止まらない(
    make: Callable[..., _Rig],
    fast_tick: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=FEATURE_LOGGER)
    ticks: list[int] = []

    def broken(self: RemoteService) -> None:
        ticks.append(1)
        raise RuntimeError("hidden")

    monkeypatch.setattr(RemoteService, "tick", broken)
    rig = make()
    rig.start()
    _wait(lambda: len(ticks) >= 3)
    assert rig.thread.is_alive()
    assert _serving(rig.config.port)
    assert "RuntimeError" in caplog.text
    assert "hidden" not in caplog.text
    assert rig.finish() <= STOP_S


def test_組み立てで思わぬ誤りが出たら型の名前だけ出して待つ(
    make: Callable[..., _Rig],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=FEATURE_LOGGER)

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("hidden")

    monkeypatch.setattr(feature_module, "OperatorSlot", broken)
    rig = make()
    rig.start()
    _wait(lambda: "RuntimeError" in caplog.text)
    assert rig.thread.is_alive()
    assert "hidden" not in caplog.text
    assert _web_threads() == []
    assert rig.finish() <= STOP_S
    assert rig.sensors.closed


# --- おやすみ（11.4） ---


def test_おやすみになったら止め方を呼ぶ(
    make: Callable[..., _Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    halts: list[int] = []
    monkeypatch.setattr(RemoteService, "halt", lambda self: halts.append(1))
    rig = make()
    rig.start()
    _wait(lambda: _serving(rig.config.port))
    rig.ctx.publish(QuietChanged(quiet=False))
    assert halts == []
    rig.ctx.publish(QuietChanged(quiet=True))
    assert halts == [1]
    rig.finish()


# --- ログインが切れたとき（3.4） ---


def test_ログインが切れたら操作する席を空けて走行を止める(
    make: Callable[..., _Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    dropped: list[str] = []
    monkeypatch.setattr(RemoteService, "drop_operator", lambda self, t: dropped.append(t))
    rig = make()
    rig.start()
    _wait(lambda: _serving(rig.config.port))
    parts = rig.feature._parts
    assert parts is not None
    token = parts.sessions.start()
    parts.sessions.end(token)
    assert dropped == [token]
    rig.finish()


# --- 写真の整理（9.6） ---


class _Prunes:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[datetime, int]] = []

        def prune(store: PhotoStore, now: datetime, days: int) -> int:
            self.calls.append((now, days))
            return 0

        monkeypatch.setattr(PhotoStore, "prune", prune)


def test_時刻が確かなら起動のときと日付が変わったときに写真を整理する(
    make: Callable[..., _Rig], fast_tick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    prunes = _Prunes(monkeypatch)
    rig = make(photo_days=7)
    rig.clock.set_synchronized(True)
    start = rig.clock.now()
    rig.start()
    _wait(lambda: len(prunes.calls) == 1)
    assert prunes.calls == [(start, 7)]
    time.sleep(0.1)
    assert len(prunes.calls) == 1  # 同じ日には 1 回だけ
    rig.clock.advance(timedelta(days=1).total_seconds())
    _wait(lambda: len(prunes.calls) == 2)
    assert prunes.calls[1][0].date() == (start + timedelta(days=1)).date()
    time.sleep(0.1)
    assert len(prunes.calls) == 2
    rig.finish()


def test_時刻が確かでなければ写真を整理せず_確かになったら整理する(
    make: Callable[..., _Rig], fast_tick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    prunes = _Prunes(monkeypatch)
    rig = make()
    rig.clock.set_synchronized(False)
    rig.start()
    _wait(lambda: _serving(rig.config.port))
    time.sleep(0.1)
    assert prunes.calls == []
    rig.clock.set_synchronized(True)
    _wait(lambda: len(prunes.calls) == 1)
    rig.finish()


def test_写真の整理で誤りが出ても止まらない(
    make: Callable[..., _Rig],
    fast_tick: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=FEATURE_LOGGER)

    def broken(store: PhotoStore, now: datetime, days: int) -> int:
        raise OSError("hidden")

    monkeypatch.setattr(PhotoStore, "prune", broken)
    rig = make()
    rig.clock.set_synchronized(True)
    rig.start()
    _wait(lambda: "OSError" in caplog.text)
    _wait(lambda: _serving(rig.config.port))
    assert "hidden" not in caplog.text
    rig.finish()


# --- 部品の選び方（15.2） ---


def _bare(tmp_path: Path) -> RemoteFeature:
    return RemoteFeature(
        FakeContext().as_context(),
        RemoteConfig.model_validate({"enabled": True, "pin": PIN}),
        photo_dir=tmp_path,
    )


def test_センサーのライブラリがなければいつも安全な偽のセンサー(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feature_module, "sensors_available", lambda: False)
    monkeypatch.setattr(feature_module, "on_raspberry_pi", lambda: False)
    assert isinstance(_bare(tmp_path)._make_sensors(), ClearSensors)


def test_Pi_でセンサーのライブラリがなければいつも読めないセンサーで前に進ませない(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(feature_module, "sensors_available", lambda: False)
    monkeypatch.setattr(feature_module, "on_raspberry_pi", lambda: True)
    with caplog.at_level(logging.WARNING, logger="osekkai.features.remote"):
        made = _bare(tmp_path)._make_sensors()
    assert isinstance(made, UnreadableSensors)
    reading = made.read()
    assert reading.distance_cm is None and not reading.far
    assert reading.floor == (None, None, None)
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "Raspberry Pi なのにセンサーの部品（smbus2・lgpio）がないので、前には進ませません"
    ]


def test_センサーのライブラリがあれば実機のセンサーを時計つきで作る(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[dict[str, object]] = []

    class FakeHat:
        def __init__(self, **kwargs: object) -> None:
            made.append(kwargs)

    monkeypatch.setattr(feature_module, "sensors_available", lambda: True)
    monkeypatch.setattr(feature_module, "HatSensors", FakeHat)
    feature = _bare(tmp_path)
    assert isinstance(feature._make_sensors(), FakeHat)
    assert made == [{"clock": feature._ctx.clock}]


def test_USB_カメラが見つからないかライブラリがなければない_カメラ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feature_module, "find_usb_camera", lambda configured: None)
    monkeypatch.setattr(feature_module, "usb_camera_available", lambda: True)
    assert isinstance(_bare(tmp_path)._make_usb(), NoUsbCamera)
    monkeypatch.setattr(feature_module, "find_usb_camera", lambda configured: "/dev/video9")
    monkeypatch.setattr(feature_module, "usb_camera_available", lambda: False)
    assert isinstance(_bare(tmp_path)._make_usb(), NoUsbCamera)


def test_USB_カメラが見つかれば実機のカメラを作る(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[tuple[str, object]] = []

    class FakeUsb:
        def __init__(self, device: str, *, clock: object = None) -> None:
            made.append((device, clock))

    monkeypatch.setattr(feature_module, "find_usb_camera", lambda configured: "/dev/video9")
    monkeypatch.setattr(feature_module, "usb_camera_available", lambda: True)
    monkeypatch.setattr(feature_module, "UsbCamera", FakeUsb)
    feature = _bare(tmp_path)
    assert isinstance(feature._make_usb(), FakeUsb)
    assert made == [("/dev/video9", feature._ctx.clock)]
