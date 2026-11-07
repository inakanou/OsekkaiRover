"""見つけてあいさつの機能の本体（4.1〜4.4、4.6、4.7、8.5）。

本物の調停役と静かな状態を偽のデバイスで組み、偽のカメラと偽の映像の処理で動かす。
機能は別のスレッドで動かし、実際の時間で待つ（待ちの間は短くする）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from presence_fakes import FakeCamera, FakeFrame, FakeVision, embedding

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Arbiter, Lease
from osekkai.core.buttons import ButtonRouter
from osekkai.core.cache import CacheManager
from osekkai.core.config import MotionSettings, VoiceSettings
from osekkai.core.display import DisplayService, LedService
from osekkai.core.events import EventBus
from osekkai.core.feature import CoreFeatureContext, validate_spec
from osekkai.core.idle import IdleScreen
from osekkai.core.lcd_text import center, to_lcd_codes
from osekkai.core.motion import MotionService
from osekkai.core.paths import Paths
from osekkai.core.quiet import QuietMode
from osekkai.core.storage import StateStore
from osekkai.core.types import Face, Priority, Resource
from osekkai.core.voice import VoiceService
from osekkai.features.presence import PresenceFeature, api, texts
from osekkai.features.presence import feature as feature_module
from osekkai.features.presence.adapters.opencv import VisionUnavailable
from osekkai.features.presence.checks import PRESENCE_CHECKS
from osekkai.features.presence.config import PresenceConfig
from osekkai.features.presence.faces import FaceBook, Person
from osekkai.features.presence.ports import Frame
from osekkai.features.presence.tracking import FaceBox
from osekkai.features.presence.watcher import CameraNotAvailable

WAIT_S = 5.0
LOOP_S = 0.01  # 機能の回りの間（実際の秒数）
RETRY_S = 0.05  # 首を借り直す間（実際の秒数）
FACE = FaceBox(0.5, 0.0, 0.3)
HIDDEN_ID = "zzhiddenid"
HIDDEN_NAME = "ひみつのよびな"
EMB = embedding(1)
ROUTINE = "routine"
ROUTINE_PRIORITY = 85


def wait_for(condition: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


def lcd_row(text: str) -> tuple[int, ...]:
    codes = to_lcd_codes(center(text))
    return tuple(codes) + (0x20,) * (16 - len(codes))


def always(face: FaceBox | None, count: int = 100000) -> dict[int, tuple[FaceBox, ...]]:
    return {i: (() if face is None else (face,)) for i in range(count)}


class Bench:
    """基盤の部品を偽のデバイスで組み、見つけてあいさつの機能を動かす一式。"""

    def __init__(self, fakes: FakeDeviceSet, clock: FakeClock) -> None:
        self.fakes = fakes
        self.clock = clock
        self.paths = Paths()
        self.router = ButtonRouter(fakes.buttons, clock)
        self.cache = CacheManager.from_paths(self.paths, 10, clock)
        self.voice = VoiceService(
            fakes.synth, fakes.player, self.cache, VoiceSettings(sink="speaker")
        )
        self.arbiter = Arbiter(
            clock,
            MotionService(fakes.servos, fakes.motors, MotionSettings(), clock),
            voice=self.voice,
            display=DisplayService(fakes.display),
            led=LedService(fakes.led),
            buttons=self.router,
        )
        self.idle = IdleScreen(self.arbiter, clock)
        self.bus = EventBus()
        self.quiet = QuietMode(
            self.idle, self.bus, StateStore.for_feature(self.paths, "core", clock), clock
        )
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.feature: PresenceFeature | None = None

    def context(self) -> CoreFeatureContext:
        return CoreFeatureContext(
            PresenceFeature.spec,
            arbiter=self.arbiter,
            buttons=self.router,
            idle=self.idle,
            quiet=self.quiet,
            bus=self.bus,
            clock=self.clock,
            paths=self.paths,
            cache=self.cache,
        )

    def store(self) -> StateStore:
        return StateStore.for_feature(self.paths, "presence", self.clock)

    def make(
        self,
        camera: FakeCamera | None,
        vision: FakeVision | None,
        config: PresenceConfig | None = None,
    ) -> PresenceFeature:
        self.feature = PresenceFeature(
            self.context(),
            config or PresenceConfig(),
            camera=camera,
            vision=vision,
            neck_retry=RETRY_S,
            loop_interval=LOOP_S,
        )
        return self.feature

    def start(
        self,
        camera: FakeCamera | None,
        vision: FakeVision | None,
        config: PresenceConfig | None = None,
    ) -> PresenceFeature:
        feature = self.make(camera, vision, config)
        self.run(feature)
        return feature

    def run(self, feature: PresenceFeature) -> None:
        self.feature = feature
        self.thread = threading.Thread(target=feature.run, args=(self.stop,), daemon=True)
        self.thread.start()

    def holder(self, resource: Resource = Resource.NECK) -> str | None:
        return self.arbiter.holder(resource)

    def advance_until(self, condition: Callable[[], bool], step: float = 0.1) -> bool:
        """偽の時計を少しずつ進めながら待つ（見続けた時間を数えるため）。"""

        def tick() -> bool:
            if condition():
                return True
            self.clock.advance(step)
            return False

        return wait_for(tick)

    def close(self) -> float:
        """止める旗を立てて、戻るまでの実際の秒数を返す。"""
        begin = time.monotonic()
        self.stop.set()
        if self.thread is not None:
            self.thread.join(WAIT_S)
            assert not self.thread.is_alive(), "止める旗で戻りませんでした"
        return time.monotonic() - begin


@pytest.fixture
def bench(xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock) -> Iterator[Bench]:
    kit = Bench(fakes, clock)
    yield kit
    kit.close()
    kit.router.close()


def _acquire_in_thread(bench: Bench, priority: int, resources: set[Resource]) -> list[Lease]:
    got: list[Lease] = []

    def take() -> None:
        got.append(bench.arbiter.acquire(ROUTINE, priority, resources))

    threading.Thread(target=take, daemon=True).start()
    return got


# --- 申告 ---


def test_申告は見守りの帯で首と声と画面() -> None:
    spec = PresenceFeature.spec
    assert spec.name == "presence"
    assert spec.priority == Priority.BACKGROUND
    assert spec.resources == frozenset({Resource.NECK, Resource.VOICE, Resource.DISPLAY})
    assert spec.config_model is PresenceConfig
    assert spec.checks == PRESENCE_CHECKS
    assert validate_spec(spec) == []


def test_パッケージから遅れて読み込める() -> None:
    import osekkai.features.presence as package

    assert package.PresenceFeature is feature_module.PresenceFeature
    with pytest.raises(AttributeError):
        _ = package.NoSuchName


def test_設定のモデルが違えば断る(bench: Bench) -> None:
    from osekkai.core.config import FeatureConfig

    with pytest.raises(TypeError):
        PresenceFeature(bench.context(), FeatureConfig())


# --- カメラと映像の処理の選び方 ---


def test_ライブラリがなければカメラなし(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feature_module, "picamera_available", lambda: False)
    monkeypatch.setattr(feature_module, "opencv_available", lambda: True)
    feature = PresenceFeature(bench.context(), PresenceConfig())
    assert not feature.has_camera


def test_映像の処理を作れなければカメラなし(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object) -> object:
        raise VisionUnavailable()

    monkeypatch.setattr(feature_module, "picamera_available", lambda: True)
    monkeypatch.setattr(feature_module, "opencv_available", lambda: True)
    monkeypatch.setattr(feature_module, "OpenCvVision", broken)
    feature = PresenceFeature(bench.context(), PresenceConfig())
    assert not feature.has_camera


def test_ライブラリがあれば実機のアダプタを使う(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: dict[str, object] = {}

    class StubCamera(FakeCamera):
        def __init__(self, *, flip: bool) -> None:
            super().__init__()
            made["flip"] = flip

    class StubVision(FakeVision):
        def __init__(self, detection: Path, recognition: Path) -> None:
            super().__init__()
            made["models"] = (detection.name, recognition.name)

    monkeypatch.setattr(feature_module, "picamera_available", lambda: True)
    monkeypatch.setattr(feature_module, "opencv_available", lambda: True)
    monkeypatch.setattr(feature_module, "PiCamera", StubCamera)
    monkeypatch.setattr(feature_module, "OpenCvVision", StubVision)
    feature = PresenceFeature(bench.context(), PresenceConfig(flip=True))
    assert feature.has_camera
    assert made["flip"] is True
    assert made["models"] == (
        "face_detection_yunet_2023mar.onnx",
        "face_recognition_sface_2021dec.onnx",
    )


def test_カメラがなければ資源を1つも借りず止める旗ですぐ戻る(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feature_module, "picamera_available", lambda: False)
    feature = PresenceFeature(
        bench.context(), PresenceConfig(), neck_retry=RETRY_S, loop_interval=LOOP_S
    )
    bench.run(feature)
    time.sleep(0.2)
    assert bench.thread is not None and bench.thread.is_alive()
    for resource in Resource:
        assert bench.holder(resource) is None
    assert bench.close() < 0.5


# --- 首を借りて見る ---


def test_顔を見つけると首を向けて目を変える(bench: Bench, fakes: FakeDeviceSet) -> None:
    camera = FakeCamera()
    bench.start(camera, FakeVision(faces=always(FACE)))
    assert wait_for(lambda: bench.holder() == "presence")
    assert wait_for(lambda: fakes.servos.angles.get("pan", 0.0) > 0.0)
    assert camera.started
    assert wait_for(lambda: bench.idle._face is Face.HAPPY)
    assert bench.idle._gaze > 0.0


def test_優先度85が首を求めると見るのをやめてすぐ返し返されたら借り直す(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    camera = FakeCamera()
    bench.start(camera, FakeVision(faces=always(FACE)))
    assert wait_for(lambda: bench.holder() == "presence" and camera.started)

    begin = time.monotonic()
    got = _acquire_in_thread(bench, ROUTINE_PRIORITY, {Resource.NECK})
    assert wait_for(lambda: bool(got))
    assert time.monotonic() - begin < 1.0  # 取り上げ（猶予の 2 秒）を待たずに返した
    assert "資源を取り上げました" not in caplog.text
    assert bench.holder() == ROUTINE
    assert wait_for(lambda: not camera.started)  # 見るのをやめた

    # ほかの機能が首を使っている間は、顔を探さない（4.3）
    count = camera.capture_count
    time.sleep(0.2)
    assert camera.capture_count == count
    assert bench.holder() == ROUTINE

    # 返されたら借り直して、また見る（4.6）
    got[0].release()
    assert wait_for(lambda: bench.holder() == "presence")
    assert wait_for(lambda: camera.capture_count > count)


def test_静かな状態の間はカメラを止めて首を返す(bench: Bench) -> None:
    camera = FakeCamera()
    bench.start(camera, FakeVision(faces=always(FACE)))
    assert wait_for(lambda: camera.started and bench.holder() == "presence")

    bench.quiet.request_quiet()
    assert wait_for(lambda: not camera.started)
    assert wait_for(lambda: bench.holder() is None)
    count = camera.capture_count
    time.sleep(0.2)
    assert camera.capture_count == count
    assert bench.holder() is None

    bench.quiet.end_quiet()
    assert wait_for(lambda: camera.started and bench.holder() == "presence")
    assert wait_for(lambda: camera.capture_count > count)


def test_静かな状態から始めると首を借りない(bench: Bench) -> None:
    bench.quiet.request_quiet()
    camera = FakeCamera()
    bench.start(camera, FakeVision(faces=always(FACE)))
    time.sleep(0.2)
    assert bench.holder() is None
    assert camera.start_count == 0


def test_首を借りられなければ間をおいて試し続ける(bench: Bench) -> None:
    other = bench.arbiter.acquire(ROUTINE, Priority.SCHEDULED, {Resource.NECK})
    camera = FakeCamera()
    bench.start(camera, FakeVision(faces=always(FACE)))
    time.sleep(0.2)
    assert bench.holder() == ROUTINE
    assert camera.capture_count == 0
    other.release()
    assert wait_for(lambda: bench.holder() == "presence")
    assert wait_for(lambda: camera.capture_count > 0)


# --- あいさつ ---


def test_あいさつは声と画面をそのときだけ借りる(
    bench: Bench, fakes: FakeDeviceSet, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    book = FaceBook(bench.store())
    book.put(Person(HIDDEN_ID, HIDDEN_NAME, (EMB,)))
    book.save()
    vision = FakeVision(faces=always(FACE), embeddings=dict.fromkeys(range(100000), EMB))
    bench.start(FakeCamera(), vision)

    assert bench.advance_until(lambda: bool(fakes.synth.lines))
    # 話し終えたら声と画面を返す
    assert wait_for(lambda: bench.holder(Resource.VOICE) is None)
    expected = texts.fill(texts.MORNING_KNOWN, HIDDEN_NAME)
    assert "".join(fakes.synth.lines).replace(" ", "") == expected.replace(" ", "")
    rows = [c.args for c in fakes.display.calls if c.op == "write_line"]
    assert any(lcd_row(texts.LCD_FOUND) in row for row in rows)
    assert any(lcd_row(Face.HAPPY.text()) in row for row in rows)
    assert bench.holder(Resource.DISPLAY) != "presence"  # 待機画面に戻る
    assert bench.holder(Resource.NECK) == "presence"
    for hidden in (HIDDEN_ID, HIDDEN_NAME, expected, texts.LCD_FOUND):
        assert hidden not in caplog.text


def test_声を借りられなければあいさつしない(
    bench: Bench, fakes: FakeDeviceSet, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    other = bench.arbiter.acquire(ROUTINE, Priority.INTERACTIVE, {Resource.VOICE})
    bench.start(FakeCamera(), FakeVision(faces=always(FACE)))
    assert bench.advance_until(lambda: "声と画面を借りられないので" in caplog.text)
    assert fakes.synth.lines == []
    assert bench.holder(Resource.DISPLAY) != "presence"
    other.release()
    assert bench.advance_until(lambda: bool(fakes.synth.lines))
    assert wait_for(lambda: bench.holder(Resource.VOICE) is None)
    joined = "".join(fakes.synth.lines).replace(" ", "")
    assert joined == texts.MORNING_UNKNOWN.replace(" ", "")


# --- カメラが使えない・回りの例外 ---


def test_カメラが使えなければ見るのを止めてほかは動き続ける(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    camera = FakeCamera(unavailable=True)
    bench.start(camera, FakeVision(faces=always(FACE)))
    assert wait_for(lambda: "CameraUnavailable" in caplog.text)
    assert wait_for(lambda: bench.holder() is None)
    time.sleep(0.2)
    assert bench.holder() is None  # 借り直さない
    assert bench.thread is not None and bench.thread.is_alive()
    # ほかの機能は首を使える
    lease = bench.arbiter.acquire(ROUTINE, Priority.BACKGROUND + 1, {Resource.NECK})
    lease.neck.look(10.0, 0.0)
    lease.release()
    # カメラを借りる口からは使えないと分かる
    watcher = api.running_watcher()
    assert watcher is not None
    with pytest.raises(CameraNotAvailable):
        watcher.start_loan("daruma", lambda: True)
    assert bench.close() < 0.5


def test_回りの例外は型の名前だけを記録して続ける(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    class Flaky(FakeVision):
        def detect(self, frame: Frame) -> tuple[FaceBox, ...]:
            assert isinstance(frame, FakeFrame)
            if frame.index == 3:
                raise RuntimeError("ひみつの中身")
            return super().detect(frame)

    camera = FakeCamera()
    bench.start(camera, Flaky(faces=always(FACE)))
    assert wait_for(lambda: camera.capture_count > 10)
    assert "RuntimeError" in caplog.text
    assert "ひみつの中身" not in caplog.text
    assert bench.holder() == "presence"


def test_同じ回りの例外が続いても記録は最初とまとめだけ(
    bench: Bench, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(feature_module, "FAILURE_LOG_EVERY", 20)
    broken = [True]

    class Broken(FakeVision):
        def detect(self, frame: Frame) -> tuple[FaceBox, ...]:
            if broken[0]:
                raise RuntimeError("ひみつの中身")
            return super().detect(frame)

    camera = FakeCamera()
    bench.start(camera, Broken(faces=always(FACE)))
    assert wait_for(lambda: camera.capture_count > 45)
    failures = [r for r in caplog.records if "見る回りに失敗" in r.getMessage()]
    # 最初の 1 回と、20 回ごとのまとめだけ（毎回は出さない）
    assert 2 <= len(failures) <= 4
    assert all("ひみつの中身" not in r.getMessage() for r in caplog.records)
    broken[0] = False
    assert wait_for(lambda: "見る回りが直りました" in caplog.text)


def test_止まると借りていた機能の要求は使えないと分かる(bench: Bench) -> None:
    bench.start(FakeCamera(), FakeVision())
    assert wait_for(lambda: api.running_watcher() is not None)
    watcher = api.running_watcher()
    assert watcher is not None
    lent = watcher.start_loan("daruma", lambda: True)
    bench.close()
    assert not watcher.loaned
    with pytest.raises(CameraNotAvailable):
        watcher.request_frame(lent)


# --- カメラを借りる口の置き場と貸し出し ---


def test_動いている見る流れを置き場に登録し終わりで外す(bench: Bench) -> None:
    assert api.running_watcher() is None
    bench.start(FakeCamera(), FakeVision())
    assert wait_for(lambda: api.running_watcher() is not None)
    bench.close()
    assert api.running_watcher() is None


def test_静かな状態でも貸し出しの要求には答える(bench: Bench) -> None:
    bench.quiet.request_quiet()
    camera = FakeCamera()
    bench.start(camera, FakeVision())
    assert wait_for(lambda: api.running_watcher() is not None)
    watcher = api.running_watcher()
    assert watcher is not None
    lent = watcher.start_loan("daruma", lambda: True)
    data = watcher.request_frame(lent).wait(WAIT_S)
    assert data.startswith(b"jpeg:")
    assert bench.holder() is None  # 首は借りない
    watcher.end_loan(lent)


def test_貸し出しの間は首を借りても見ない(bench: Bench, fakes: FakeDeviceSet) -> None:
    camera = FakeCamera()
    vision = FakeVision(faces=always(FACE))
    bench.start(camera, vision)
    assert wait_for(lambda: api.running_watcher() is not None)
    watcher = api.running_watcher()
    assert watcher is not None
    lent = watcher.start_loan("daruma", lambda: True)
    time.sleep(0.05)
    detects = len(vision.detect_calls)
    time.sleep(0.2)
    assert len(vision.detect_calls) == detects
    assert watcher.request_still(lent).wait(WAIT_S).startswith(b"still:")
    watcher.end_loan(lent)
    assert wait_for(lambda: len(vision.detect_calls) > detects)


def test_見る回りの途中でも中断の知らせですぐに首を返す(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    entered = threading.Event()
    release = threading.Event()

    class Blocking(FakeVision):
        def detect(self, frame: Frame) -> tuple[FaceBox, ...]:
            assert isinstance(frame, FakeFrame)
            if frame.index == 5:
                entered.set()
                release.wait(3.0)  # 見るスレッドを回りの中で止める
            return super().detect(frame)

    camera = FakeCamera()
    bench.start(camera, Blocking(faces=always(FACE)))
    try:
        assert entered.wait(WAIT_S)
        assert bench.holder() == "presence"
        begin = time.monotonic()
        got = _acquire_in_thread(bench, ROUTINE_PRIORITY, {Resource.NECK})
        assert wait_for(lambda: bool(got), timeout=2.5)
        assert time.monotonic() - begin < 1.0
        assert "資源を取り上げました" not in caplog.text
    finally:
        release.set()
    # 回りから戻ったら、見るのをやめる
    assert wait_for(lambda: not camera.started)
    assert "見る回りに失敗しました" not in caplog.text
    got[0].release()
    assert wait_for(lambda: bench.holder() == "presence")
