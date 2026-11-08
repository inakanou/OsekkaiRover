"""ラジコンを、ログインから譲る・止めるまで通して確かめる（E2E）。

要件 2.8、5.2、5.6、6.2、8.6、10.5、11.4、12.2、12.5、13.6、15.1、15.2。

本物の基盤（Runtime）を偽のデバイスで起動し、見つけてあいさつ・だるまさん・ラジコンを動かす。
見つけてあいさつには台本のカメラと映像の処理を、ラジコンには台本のセンサー・偽の USB カメラ・
一時の写真の置き場を渡す。基盤は `機能(ctx, config)` で作るので、渡す口を持つ子のクラスを
登録する。
端末は本物の Web のサーバー（127.0.0.1 の空いたポート）に HTTP で要求を送る。

時計は実際の秒で進み、テストが先へ飛ばせる（時間帯の終わり・10 秒の間・席の交代）。
偽の時計（`FakeClock`）は眠ると直ちに時刻を進めるので、0.1 秒ごとの走行の繰り返しと
命令の途切れ（0.5 秒）を実際の HTTP の間隔で確かめられない。

1. ログイン → 前を押し続ける → センサーが物を見る → 0.2 秒の内に止まる。声は 10 秒に 1 回、
   知らせは毎回 → 「はやい」でも後退はゆっくりで、2 秒で止まる（5.2、5.6、6.2、15.2）
2. 首の映像を見て走っている間に「だるまさん しよう」→ ラジコンが止まって首とカメラを返し、
   だるまさんがカメラを借りて遊ぶ。端末には「ほかの ことを しているよ」。終わると、
   見るだけの段を借り直して映像に戻る。首を持つ間、見つけてあいさつは顔を探さない（12.2、12.5）
3. 時間帯の終わりと、おやすみで、走行が止まり、映像が終わり、知らせが出る（8.6、11.4）
4. 通しのログ・標準出力と標準エラー・self-check の出力・残ったファイルに、pin・入れた pin・
   端末のアドレス・ログインの印・話させた文字が出ない（2.8、10.5、13.6）

同期は条件を待つ（実際の秒数の上限つき）。
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import random
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlencode

import pytest
from remote_fakes import FakeUsbCamera, ScriptedSensors

import osekkai.features.presence.api as presence_api
from osekkai import __main__ as cli
from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import JST
from osekkai.adapters.fake.probes import FakeProbes
from osekkai.core.config import FeatureConfig
from osekkai.core.feature import FeatureContext
from osekkai.core.paths import Paths
from osekkai.core.registry import FeatureRegistry
from osekkai.core.runtime import Runtime
from osekkai.core.types import Resource
from osekkai.core.voice import VoiceService
from osekkai.features.daruma import DarumaFeature
from osekkai.features.daruma import feature as daruma_feature
from osekkai.features.daruma import texts as daruma_texts
from osekkai.features.daruma.flow import GameResult
from osekkai.features.daruma.rules import EndReason
from osekkai.features.presence import PresenceFeature
from osekkai.features.presence.ports import Frame
from osekkai.features.presence.tracking import FaceBox
from osekkai.features.remote import RemoteFeature, texts
from osekkai.features.remote import feature as remote_feature
from osekkai.features.remote.ports import SensorReading
from osekkai.features.remote.web import COOKIE
from osekkai.voice import api
from osekkai.voice.intents import Heard, Words

WAIT_S: Final = 5.0  # 別のスレッドを待つ上限（実際の秒）
POLL_S: Final = 0.005  # 条件を見直す間（実際の秒）
HOLD_S: Final = 0.15  # 押し続けている間に、走る命令を送り直す間（実際の秒）
HTTP_TIMEOUT_S: Final = 10.0
STREAM_TIMEOUT_S: Final = 20.0  # だるまさんの間は 1 枚も届かない
STOP_WITHIN_S: Final = 0.2  # 危険を見てから止まるまでの上限（5.2）

HOST: Final = "127.0.0.1"  # 端末のアドレス。ログに出てはいけない
PIN: Final = "48213"  # 外に出てはいけない pin
WRONG_PIN: Final = "13579"  # 入れられたまちがいの pin。ログに出てはいけない
SAID: Final = "hidden4"  # 話させた文字。ログにもファイルにも残ってはいけない
PAGE: Final = "a1"
START: Final = datetime(2026, 1, 1, 10, 0, tzinfo=JST)  # 使える時間帯の中
OUT_OF_HOURS: Final = datetime(2026, 1, 1, 20, 0, 1, tzinfo=JST)  # 時間帯の終わりの後
HEARD_DARUMA: Final = "だるまさん しよう"
SLOW: Final = 20  # 設定の見本の「ゆっくり」
FAST: Final = 35  # 設定の見本の「はやい」

NECK_JPEG: Final = b"\xff\xd8neck\xff\xd9"
STILL_JPEG: Final = b"\xff\xd8still\xff\xd9"
USB_JPEG: Final = b"\xff\xd8usb\xff\xd9"
PART_MARK: Final = b"--frame\r\n"
CLEAR: Final = SensorReading(distance_cm=100.0, floor=(1000, 1000, 1000))
OBSTACLE: Final = SensorReading(distance_cm=10.0, floor=(1000, 1000, 1000))
MOTION: Final = 0.004  # 台本の動きの量（だるまさんのしきい値 0.01 の下）
CHANT_SPEECHES: Final = frozenset(chant.speech for chant in daruma_texts.CHANTS)


def wait_for(condition: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(POLL_S)
    return condition()


def _free_port() -> int:
    with socket.create_server(("0.0.0.0", 0)) as sock:
        port: int = sock.getsockname()[1]
    return port


# --- 時計 ---


class LiveClock:
    """実際の秒で進む時計（`Clock`）。テストは先へ飛ばせる・壁の時刻を変えられる。

    - `monotonic()` は実際の単調な時刻に、飛ばした秒を足したもの
    - `now()` は決めた壁の時刻から、単調な時刻と同じだけ進む
    - `sleep()` は実際に眠る
    """

    def __init__(self, start: datetime) -> None:
        self._lock = threading.Lock()
        self._origin = time.monotonic()
        self._jumped = 0.0
        self._wall = start
        self._wall_at = self._mono()

    def _mono(self) -> float:
        return 1000.0 + time.monotonic() - self._origin + self._jumped

    def monotonic(self) -> float:
        with self._lock:
            return self._mono()

    def now(self) -> datetime:
        with self._lock:
            return self._wall + timedelta(seconds=self._mono() - self._wall_at)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def is_synchronized(self) -> bool:
        return True

    def advance(self, seconds: float) -> None:
        """単調な時刻と壁の時刻を、どちらも `seconds` 秒先へ飛ばす。"""
        with self._lock:
            self._jumped += seconds

    def set_now(self, now: datetime) -> None:
        """壁の時刻だけを変える。"""
        with self._lock:
            self._wall = now
            self._wall_at = self._mono()


# --- 見つけてあいさつの台本のカメラと映像の処理 ---


@dataclass(frozen=True)
class _Frame:
    index: int
    width: int = 640
    height: int = 480


class ScriptCamera:
    """台本のカメラ。撮るたびに番号の続くフレームを返す。"""

    def __init__(self) -> None:
        self.started = False
        self.capture_count = 0

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def capture(self) -> Frame:
        frame = _Frame(self.capture_count)
        self.capture_count += 1
        return frame

    def capture_still(self) -> bytes:
        return STILL_JPEG


class ScriptVision:
    """台本の映像の処理。顔は写らず、動きの量はいつも MOTION。"""

    def __init__(self) -> None:
        self.detect_count = 0
        self.motion_count = 0

    def detect(self, frame: Frame) -> tuple[FaceBox, ...]:
        self.detect_count += 1
        return ()

    def embed(self, frame: Frame, face: FaceBox) -> None:
        return None

    def motion(self, before: Frame, after: Frame) -> float:
        self.motion_count += 1
        return MOTION

    def to_jpeg(self, frame: Frame, max_width: int, quality: int) -> bytes:
        return NECK_JPEG


# --- 話した言葉の記録 ---


class SpeechLog:
    """本物の声の部品が話した文章を順に残す。話し終えるたびに `after_say` を呼ぶ。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._lock = threading.Lock()
        self._said: list[str] = []
        self.after_say: Callable[[str], None] | None = None
        original = VoiceService.speak
        log = self

        def speak(
            voice: VoiceService,
            text: str,
            *,
            keep: bool = True,
            should_continue: Callable[[], bool] | None = None,
        ) -> bool:
            spoke = original(voice, text, keep=keep, should_continue=should_continue)
            with log._lock:
                log._said.append(text)
            hook = log.after_say
            if hook is not None:
                hook(text)
            return spoke

        monkeypatch.setattr(VoiceService, "speak", speak)

    @property
    def said(self) -> list[str]:
        with self._lock:
            return list(self._said)

    def count(self, text: str) -> int:
        return self.said.count(text)


class GameLog:
    """だるまさんの 1 回ごとの終わり方を残す。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.results: list[GameResult] = []
        original = daruma_feature.run_game

        def run_game(*args: Any, **kwargs: Any) -> GameResult:
            result = original(*args, **kwargs)
            self.results.append(result)
            return result

        monkeypatch.setattr(daruma_feature, "run_game", run_game)


# --- 端末 ---


class Terminal:
    """1 台の端末。ログインの印をクッキーで持ち、HTTP で要求を送る。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.cookie: str | None = None
        self._lock = threading.Lock()
        self._seq = 0

    def token(self) -> str | None:
        """今持っているログインの印。"""
        return self.cookie

    def headers(self) -> dict[str, str]:
        headers = {"Origin": f"http://{HOST}:{self.port}"}
        if self.cookie is not None:
            headers["Cookie"] = f"{COOKIE}={self.cookie}"
        return headers

    def request(
        self, method: str, path: str, body: bytes | None = None, content_type: str | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection(HOST, self.port, timeout=HTTP_TIMEOUT_S)
        try:
            headers = self.headers()
            if content_type is not None:
                headers["Content-Type"] = content_type
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            found = {name.lower(): value for name, value in response.getheaders()}
            return response.status, found, response.read()
        finally:
            conn.close()

    def login(self, pin: str) -> int:
        form = urlencode({"pin": pin}).encode("ascii")
        status, headers, _ = self.request(
            "POST", "/login", form, "application/x-www-form-urlencoded"
        )
        cookie = headers.get("set-cookie")
        if cookie is not None:
            self.cookie = cookie.split(";", 1)[0].split("=", 1)[1]
        return status

    def post(self, path: str, data: dict[str, object]) -> dict[str, Any]:
        status, _, body = self.request(
            "POST", path, json.dumps(data).encode("utf-8"), "application/json"
        )
        assert status == 200, path
        found: dict[str, Any] = json.loads(body)
        return found

    def drive(self, move: str) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            seq = self._seq
        return self.post("/api/drive", {"move": move, "seq": seq, "page": PAGE})

    def status(self) -> dict[str, Any]:
        status, _, body = self.request("GET", "/api/status")
        assert status == 200
        found: dict[str, Any] = json.loads(body)
        return found


class Holder:
    """ボタンを押し続ける。HOLD_S ごとに同じ走る命令を送り直し、終わりに STOP を送る。"""

    def __init__(self, terminal: Terminal, move: str) -> None:
        self._terminal = terminal
        self._move = move
        self._stop = threading.Event()
        self.replies: list[str] = []
        self.errors: list[BaseException] = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.replies.append(str(self._terminal.drive(self._move).get("reply")))
            except BaseException as e:  # テストの終わりで確かめる
                self.errors.append(e)
                return
            self._stop.wait(HOLD_S)

    def release(self) -> None:
        """ボタンを離す。"""
        self._stop.set()
        self._thread.join(HTTP_TIMEOUT_S)
        assert not self._thread.is_alive(), "押し続ける端末が終わりません"
        assert self.errors == []
        self._terminal.drive("stop")


class StreamWatch:
    """映像を見続ける端末。届いた部分の数を数え、配信が終わると `ended` を立てる。"""

    def __init__(self, terminal: Terminal, cam: str = "neck") -> None:
        self._conn = http.client.HTTPConnection(HOST, terminal.port, timeout=STREAM_TIMEOUT_S)
        self._path = f"/stream/{cam}"
        self._headers = terminal.headers()
        self._lock = threading.Lock()
        self._frames = 0
        self.status: int | None = None
        self.error: BaseException | None = None
        self.ended = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def frames(self) -> int:
        with self._lock:
            return self._frames

    def _run(self) -> None:
        try:
            self._conn.request("GET", self._path, headers=self._headers)
            response = self._conn.getresponse()
            self.status = response.status
            tail = b""
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                data = tail + chunk
                with self._lock:
                    self._frames += data.count(PART_MARK)
                tail = data[-(len(PART_MARK) - 1) :]
        except BaseException as e:  # 閉じたときの誤りも含む。テストが確かめる
            self.error = e
        finally:
            self.ended.set()

    def close(self) -> None:
        sock = self._conn.sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        self._conn.close()
        self._thread.join(WAIT_S)


# --- 一式 ---


class Rover:
    """本物の基盤を偽のデバイスで起動し、見つけてあいさつ・だるまさん・ラジコンを動かす。"""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.clock = LiveClock(START)
        self.fakes = FakeDeviceSet.create(self.clock)
        self.sensors = ScriptedSensors([CLEAR])
        self.usb = FakeUsbCamera([USB_JPEG])
        self.photo_dir = tmp_path / "photos"
        self.camera = ScriptCamera()
        self.vision = ScriptVision()
        self.speech = SpeechLog(monkeypatch)
        self.games = GameLog(monkeypatch)
        self.port = _free_port()
        self.paths = Paths()
        self._place_config()
        rover = self

        class JourneyPresence(PresenceFeature):
            def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
                super().__init__(
                    ctx,
                    config,
                    camera=rover.camera,
                    vision=rover.vision,
                    neck_retry=0.05,
                    loop_interval=0.005,
                )

        class JourneyDaruma(DarumaFeature):
            def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
                super().__init__(ctx, config, poll_s=0.01)

        class JourneyRemote(RemoteFeature):
            def __init__(self, ctx: FeatureContext, config: FeatureConfig) -> None:
                super().__init__(
                    ctx,
                    config,
                    sensors=rover.sensors,
                    usb=rover.usb,
                    photo_dir=rover.photo_dir,
                    rng=random.Random(0),
                )

        registry = FeatureRegistry([JourneyPresence, JourneyDaruma, JourneyRemote])
        self.runtime = Runtime(self.fakes.to_devices(), self.paths, self.clock, registry)

    def _place_config(self) -> None:
        config = self.paths.config_file
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "presence: {}\n"
            "daruma:\n"
            "  watch_seconds: 1.0\n"
            "  max_rounds: 3\n"
            "remote:\n"
            "  enabled: true\n"
            f'  pin: "{PIN}"\n'
            f"  port: {self.port}\n"
            '  hours: ["07:00", "20:00"]\n',
            encoding="utf-8",
        )

    def start(self) -> None:
        assert self.runtime.start() is None
        assert self.runtime.report is not None
        assert self.runtime.report.feature_errors == {}
        assert self.runtime.started_features == ["presence", "daruma", "remote"]
        assert wait_for(self._serving), "ラジコンが待ち受けません"
        assert wait_for(lambda: api.registry().owner_alive("daruma"))
        assert wait_for(lambda: self.holder(Resource.NECK) == "presence" and self.camera.started)

    def _serving(self) -> bool:
        try:
            status, _, _ = Terminal(self.port).request("GET", "/app.css")
        except OSError:
            return False
        return status == 200

    def holder(self, resource: Resource) -> str | None:
        arbiter = self.runtime.arbiter
        assert arbiter is not None
        return arbiter.holder(resource)

    def neck_resources(self) -> frozenset[Resource] | None:
        arbiter = self.runtime.arbiter
        assert arbiter is not None
        lease = arbiter.lease_for(Resource.NECK)
        return None if lease is None else lease.resources

    def speeds(self) -> tuple[int, int]:
        return self.fakes.motors.speeds

    def motor_calls(self, since: float) -> list[tuple[float, tuple[object, ...]]]:
        """`since` より後の set_speeds の (時刻, 引数) の並び。"""
        calls = list(self.fakes.motors.calls)
        return [(c.at, c.args) for c in calls if c.op == "set_speeds" and c.at >= since]

    def say_daruma(self) -> None:
        """入口が聞き取ったのと同じに、始める語句をプロセスの中の登録表から渡す。"""
        route = api.registry().route(HEARD_DARUMA)
        assert isinstance(route, Words)
        assert route.registration.owner == "daruma"
        route.registration.handler(Heard(HEARD_DARUMA, follow_up=False))

    def close(self) -> None:
        self.runtime.shutdown(budget=3.0)


def _first_stop_after(rover: Rover, since: float) -> float | None:
    for at, args in rover.motor_calls(since):
        if args == (0, 0):
            return at
    return None


def _watcher_loaned() -> bool:
    watcher = presence_api.running_watcher()
    return watcher is not None and watcher.loaned


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    api._reset_for_tests()
    monkeypatch.setattr(remote_feature, "TICK_S", 0.05)  # 定期の確かめを速く回す
    monkeypatch.setattr(cli, "_build_probes", lambda fake_hardware: FakeProbes())
    yield
    api._reset_for_tests()


# --- 通し ---


def test_ログインから走る_譲る_止めるまで通し_秘密の値はどこにも出ない(
    xdg_dirs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    rover = Rover(tmp_path, monkeypatch)
    terminal = Terminal(rover.port)
    holders: list[Holder] = []
    streams: list[StreamWatch] = []

    def hold(move: str) -> Holder:
        holder = Holder(terminal, move)
        holders.append(holder)
        return holder

    def watch() -> StreamWatch:
        stream = StreamWatch(terminal)
        streams.append(stream)
        return stream

    try:
        rover.start()

        # --- 0. ログイン。まちがいの pin では印が出ず、正しい pin で操作の画面が出る（15.2） ---
        assert terminal.login(WRONG_PIN) == 200
        assert terminal.token() is None
        assert terminal.login(PIN) == 303
        assert terminal.token() is not None
        status, _, body = terminal.request("GET", "/")
        assert status == 200
        assert b"/app.js" in body

        # --- 1. 前を押し続ける → 物を見る → すぐ止まる（5.2）。声は 10 秒に 1 回（5.6） ---
        forward = hold("fwd")
        assert wait_for(lambda: rover.speeds() == (SLOW, SLOW)), "前に走りません"
        assert rover.holder(Resource.DRIVE) == "remote"
        seen_at = rover.clock.monotonic()
        rover.sensors.set(OBSTACLE)
        assert wait_for(lambda: _first_stop_after(rover, seen_at) is not None)
        stopped_at = _first_stop_after(rover, seen_at)
        assert stopped_at is not None
        assert stopped_at - seen_at <= STOP_WITHIN_S
        # 押し続けても、危険の間は走らず、声は 1 回だけ。知らせは毎回出る
        time.sleep(1.0)
        assert all(args == (0, 0) for _, args in rover.motor_calls(stopped_at))
        assert rover.speech.count(texts.SAY_DANGER_OBSTACLE) == 1
        assert terminal.status()["message"] == texts.MSG_DANGER_OBSTACLE
        # 10 秒たつと、また 1 回だけ話す
        rover.clock.advance(10.0)
        assert wait_for(lambda: rover.speech.count(texts.SAY_DANGER_OBSTACLE) == 2)
        time.sleep(0.5)
        assert rover.speech.count(texts.SAY_DANGER_OBSTACLE) == 2
        forward.release()
        rover.sensors.set(CLEAR)
        assert "ok" in forward.replies

        # 話させる文字は話すが、ログにも記録にも残さない（10.5。最後に確かめる）
        assert terminal.post("/api/say", {"text": SAID})["reply"] == "ok"
        assert wait_for(lambda: SAID in rover.speech.said)
        # 写真を 1 枚ずつ撮って残す（残ったファイルを最後に確かめる）
        photo = terminal.post("/api/photo", {})
        assert photo["reply"] == "ok"
        assert len(photo["photos"]) == 2

        # --- 「はやい」でも後退はゆっくりで、2 秒で止まり、離すまで後退しない（6.2） ---
        assert terminal.post("/api/act", {"name": "fast"})["fast"] is True
        back_from = rover.clock.monotonic()
        back = hold("back")
        assert wait_for(lambda: rover.speeds() == (-SLOW, -SLOW)), "後ろに走りません"
        backs = [at for at, args in rover.motor_calls(back_from) if args == (-SLOW, -SLOW)]
        assert wait_for(lambda: _first_stop_after(rover, backs[0]) is not None, timeout=4.0)
        back_stop = _first_stop_after(rover, backs[0])
        assert back_stop is not None
        assert 1.8 <= back_stop - backs[0] <= 2.4
        time.sleep(0.5)  # まだ押し続けている
        assert all(args == (0, 0) for _, args in rover.motor_calls(back_stop))
        assert all(
            args in {(0, 0), (-SLOW, -SLOW)} for _, args in rover.motor_calls(back_from)
        )  # はやいの速さで下がらない
        back.release()
        assert terminal.post("/api/act", {"name": "slow"})["fast"] is False

        # --- 2. 首の映像を見て走っている間に、だるまさんが始まる（12.2、12.5） ---
        stream = watch()
        assert wait_for(lambda: stream.frames >= 1), "首の映像が届きません"
        assert stream.status == 200
        # 首を持つ間、見つけてあいさつは顔を探さず、首も動かさない（12.5）
        assert rover.holder(Resource.NECK) == "remote"
        detects = rover.vision.detect_count
        time.sleep(0.3)
        assert rover.vision.detect_count == detects

        forward = hold("fwd")
        assert wait_for(lambda: rover.speeds() == (SLOW, SLOW))
        in_game: dict[str, object] = {}
        chants = 0

        def during_game(text: str) -> None:
            nonlocal chants
            if text not in CHANT_SPEECHES:
                return
            chants += 1
            if chants == 1:
                in_game["neck"] = rover.holder(Resource.NECK)
                in_game["drive"] = rover.holder(Resource.DRIVE)
                in_game["speeds"] = rover.speeds()
                in_game["frames"] = stream.frames
                in_game["message"] = terminal.status()["message"]
            elif chants == 2:
                in_game["frames_late"] = stream.frames
                in_game["motions"] = rover.vision.motion_count
                rover.clock.advance(1.0)  # ボタンのチャタリングの除去の間をあける
                rover.fakes.buttons.press()  # 唱えている間の押下で勝ち

        rover.speech.after_say = during_game
        motions = rover.vision.motion_count
        rover.say_daruma()
        assert wait_for(lambda: len(rover.games.results) == 1, timeout=10.0), "遊びが終わりません"
        rover.speech.after_say = None
        forward.release()

        assert rover.games.results[0].reason is EndReason.WIN
        # ラジコンは止まり、首とカメラを返し、端末に知らせが出た（12.2）
        assert in_game["neck"] == "daruma"
        assert in_game["drive"] is None
        assert in_game["speeds"] == (0, 0)
        assert in_game["message"] == texts.MSG_BUSY
        assert in_game["frames_late"] == in_game["frames"]  # 遊びの間、首の映像は届かない
        # だるまさんは、見つけてあいさつから借りたカメラで動きを見た
        assert isinstance(in_game["motions"], int) and in_game["motions"] > motions
        assert daruma_texts.SAY_NO_CAMERA not in rover.speech.said
        assert daruma_texts.SAY_CAMERA_LOST not in rover.speech.said
        assert wait_for(lambda: not _watcher_loaned()), "だるまさんがカメラを返しません"
        assert not stream.ended.is_set()  # 映像の枠は開いたまま

        # 終わった後: 操作の席が空くと、見るだけの段を借り直して映像に戻る
        frames = stream.frames
        rover.clock.advance(10.5)  # 席が空く 10 秒と、借り直しを待つ 5 秒を飛ばす
        assert wait_for(lambda: rover.neck_resources() == frozenset({Resource.NECK}))
        assert rover.holder(Resource.NECK) == "remote"
        assert wait_for(lambda: stream.frames > frames), "映像に戻りません"
        assert terminal.status()["operator"] == "none"

        # --- 3. 時間帯の終わりで、走行と映像が止まり、知らせが出る（8.6、11.4） ---
        forward = hold("fwd")
        assert wait_for(lambda: rover.speeds() == (SLOW, SLOW))
        assert wait_for(lambda: stream.frames > frames + 1)
        rover.clock.set_now(OUT_OF_HOURS)
        assert wait_for(lambda: rover.speeds() == (0, 0), timeout=1.0), "時間帯の後も走ります"
        assert stream.ended.wait(WAIT_S), "時間帯の後も映像が続きます"
        assert stream.error is None
        assert terminal.status()["message"] == texts.MSG_OUT_OF_HOURS
        assert wait_for(lambda: rover.holder(Resource.DRIVE) is None)
        forward.release()
        assert terminal.drive("fwd")["reply"] == "not_now"
        assert rover.speeds() == (0, 0)

        # --- おやすみでも、走行と映像が止まり、知らせが出る（8.6、11.4） ---
        rover.clock.set_now(START)
        forward = hold("fwd")
        assert wait_for(lambda: rover.speeds() == (SLOW, SLOW))
        stream = watch()
        assert wait_for(lambda: stream.frames >= 1)
        quiet = rover.runtime.quiet
        assert quiet is not None
        quiet.request_quiet()
        assert wait_for(lambda: rover.speeds() == (0, 0), timeout=1.0), "おやすみでも走ります"
        assert stream.ended.wait(WAIT_S), "おやすみでも映像が続きます"
        assert stream.error is None
        assert terminal.status()["message"] == texts.MSG_QUIET
        forward.release()
        quiet.end_quiet()
    finally:
        rover.speech.after_say = None
        for holder in holders:
            holder._stop.set()
        for stream in streams:
            stream.close()
        rover.close()

    # --- 4. 秘密の値はどこにも出ない（2.8、10.5、13.6） ---
    token = terminal.token()
    assert token is not None
    hidden = (PIN, WRONG_PIN, HOST, SAID, token)
    during = capfd.readouterr()  # 通しの間の標準出力と標準エラー
    assert cli.main(["--self-check", "--fake-hardware", "--json"]) in (0, 1)
    data = json.loads(capfd.readouterr().out)
    assert [row["name"] for row in data["results"] if row["name"].startswith("remote.")] == [
        "remote.setup",
        "remote.sensors",
        "remote.usb_camera",
    ]
    cli.main(["--self-check", "--fake-hardware"])
    out, err = capfd.readouterr()
    shown = [f"{r.getMessage()} {r.args!r}" for r in caplog.records]
    shown.extend([during.out, during.err, json.dumps(data, ensure_ascii=False), out, err])
    for text in shown:
        for value in hidden:
            assert value not in text

    # 残ったファイル（写真・状態・言葉の置き場）にも出ない。設定のファイルは除く
    saved = [
        path for path in tmp_path.rglob("*") if path.is_file() and path != rover.paths.config_file
    ]
    assert len([path for path in saved if path.parent == rover.photo_dir]) == 2
    for path in saved:
        content = path.read_bytes()
        for value in hidden:
            assert value not in path.name
            assert value.encode("utf-8") not in content
