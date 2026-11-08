"""Web の経路・クッキー・確かめの順（要件 1.1、1.2、2.1〜2.4、2.6、2.7、9.7）。

Flask の test client で確かめる。家のアドレスは文書用の私的な値（192.168.1.20）、
家の外は文書用の値（203.0.113.5）を使う。サービスは呼ばれた操作を覚える偽物。
pin の例は 4826。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Final, Literal

import pytest
from flask import Flask
from flask.testing import FlaskClient
from pydantic import SecretStr
from remote_fakes import FakeClock, FakeFrameSource
from werkzeug.test import TestResponse

from osekkai.features.remote import page, texts
from osekkai.features.remote.drive_rules import Move
from osekkai.features.remote.login import LoginGuard, Sessions
from osekkai.features.remote.media import Camera, FrameHub
from osekkai.features.remote.photos import PhotoStore
from osekkai.features.remote.service import PhotoResult, Reply, Status, Way
from osekkai.features.remote.web import (
    COOKIE,
    CSP,
    MAX_BODY_BYTES,
    MAX_SAY_INPUT,
    STREAM_MIMETYPE,
    create_app,
)

PIN: Final = "4826"
WRONG_PIN: Final = "1111"
HOME: Final = "192.168.1.20"
OUTSIDE: Final = "203.0.113.5"
SAME_ORIGIN: Final = {"Origin": "http://localhost"}
JPEG: Final = b"\xff\xd8\xff\xe0jpeg\xff\xd9"
JPEG2: Final = b"\xff\xd8\xff\xe0two\xff\xd9"
PHOTO_NAME: Final = "20261108-102030-00-neck.jpg"


class FakeService:
    """web が使う操作だけを持つ偽のサービス。呼ばれた操作を calls に覚える。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.reply = Reply.OK
        self.message: str | None = None
        self.photo = PhotoResult((), Reply.OK)
        self.streaming = True

    def drive(self, token: str, move: Move) -> Reply:
        self.calls.append(("drive", move))
        return self.reply

    def look(self, token: str, way: Way) -> Reply:
        self.calls.append(("look", way))
        return self.reply

    def horn(self, token: str) -> Reply:
        self.calls.append(("horn",))
        return self.reply

    def dance(self, token: str) -> Reply:
        self.calls.append(("dance",))
        return self.reply

    def say_text(self, token: str, text: str) -> Reply:
        self.calls.append(("say", len(text)))
        return self.reply

    def set_fast(self, token: str, fast: bool) -> Reply:
        self.calls.append(("fast", fast))
        return self.reply

    def take_photo(self, token: str) -> PhotoResult:
        self.calls.append(("photo",))
        return self.photo

    def status(self, token: str) -> Status:
        operator: Literal["me", "other", "none"] = "me"
        cameras: Mapping[Camera, bool] = {Camera.NECK: True, Camera.USB: False}
        return Status(
            message=self.message, operator=operator, fast=False, cameras=cameras, dancing=False
        )

    def may_stream(self, token: str) -> bool:
        return self.streaming


class Rig:
    """偽のサービスと本物のログインの部品で app を作り、test client を持つ。"""

    def __init__(self, tmp_path: Path) -> None:
        self.clock = FakeClock()
        self.service = FakeService()
        self.guard = LoginGuard(SecretStr(PIN), self.clock)
        self.sessions = Sessions(self.clock)
        self.photo_dir = tmp_path / "photos"
        self.photos = PhotoStore(self.photo_dir, self.clock)
        self.source = FakeFrameSource([JPEG, JPEG2, JPEG])
        self.hub = FrameHub(Camera.NECK, self.source, self.clock, run_in_thread=False)
        self.stop_event = threading.Event()
        self.app: Flask = create_app(
            service=self.service,
            guard=self.guard,
            sessions=self.sessions,
            photos=self.photos,
            hubs={Camera.NECK: self.hub},
            stop_event=self.stop_event,
        )
        self.client: FlaskClient = self.app.test_client()
        self.client.environ_base["REMOTE_ADDR"] = HOME

    def login(self) -> TestResponse:
        return self.client.post("/login", data={"pin": PIN}, headers=SAME_ORIGIN)

    def post(self, path: str, body: object) -> TestResponse:
        return self.client.post(path, json=body, headers=SAME_ORIGIN)

    def drive(self, move: str, seq: int, page_id: str = "a1b2c3") -> TestResponse:
        return self.post("/api/drive", {"move": move, "seq": seq, "page": page_id})


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


@pytest.fixture
def logged_in(rig: Rig) -> Rig:
    assert rig.login().status_code == 303
    return rig


def _assert_headers(response: TestResponse) -> None:
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == CSP


# --- 家のアドレスの確かめ（1.1、1.2） ---


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("GET", "/app.js"),
        ("GET", "/app.css"),
        ("POST", "/login"),
        ("GET", "/api/status"),
        ("POST", "/api/drive"),
        ("GET", "/photos/" + PHOTO_NAME),
        ("GET", "/no-such-path"),
    ],
)
def test_家の外からはどの経路も本文なしの403(rig: Rig, method: str, path: str) -> None:
    rig.client.environ_base["REMOTE_ADDR"] = OUTSIDE
    response = rig.client.open(path, method=method, headers=SAME_ORIGIN)
    assert response.status_code == 403
    assert response.data == b""
    _assert_headers(response)


def test_家の外からはログインしていても403(logged_in: Rig) -> None:
    logged_in.client.environ_base["REMOTE_ADDR"] = OUTSIDE
    response = logged_in.client.get("/api/status")
    assert response.status_code == 403
    assert response.data == b""


def test_家のアドレスならログインの画面を出す(rig: Rig) -> None:
    response = rig.client.get("/")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert 'action="/login"' in response.get_data(as_text=True)
    _assert_headers(response)


def test_IPv6のループバックも家として受ける(rig: Rig) -> None:
    rig.client.environ_base["REMOTE_ADDR"] = "::1"
    assert rig.client.get("/").status_code == 200


@pytest.mark.parametrize("path", ["/login", "/api/act"])
def test_家の外の名前のHostは出どころが合っても403(logged_in: Rig, path: str) -> None:
    response = logged_in.client.post(
        path,
        json={"name": "horn"},
        base_url="http://evil.example",
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert response.data == b""
    assert logged_in.service.calls == []
    _assert_headers(response)


@pytest.mark.parametrize(
    "host", ["192.168.1.20:8080", "[fe80::1]:8080", "raspberrypi5:8080", "rover.local:8080"]
)
def test_家のHostなら受ける(rig: Rig, host: str) -> None:
    response = rig.client.post(
        "/login", data={"pin": PIN}, base_url=f"http://{host}", headers={"Origin": f"http://{host}"}
    )
    assert response.status_code == 303


@pytest.mark.parametrize("host", ["203.0.113.5", ""])
def test_家でないHostや空のHostは403(rig: Rig, host: str) -> None:
    response = rig.client.get("/", headers={"Host": host})
    assert response.status_code == 403
    assert response.data == b""
    _assert_headers(response)


# --- 画面の部品 ---


def test_画面のスクリプトとCSSはログインの前でも返す(rig: Rig) -> None:
    js = rig.client.get("/app.js")
    css = rig.client.get("/app.css")
    assert js.status_code == 200
    assert js.headers["Content-Type"] == "application/javascript; charset=utf-8"
    assert js.get_data(as_text=True) == page.APP_JS
    assert css.status_code == 200
    assert css.mimetype == "text/css"
    assert css.get_data(as_text=True) == page.APP_CSS
    _assert_headers(js)
    _assert_headers(css)


# --- ログイン（2.1〜2.4） ---


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/status"),
        ("POST", "/api/drive"),
        ("POST", "/api/look"),
        ("POST", "/api/act"),
        ("POST", "/api/say"),
        ("POST", "/api/photo"),
        ("POST", "/logout"),
        ("GET", "/photos/" + PHOTO_NAME),
    ],
)
def test_ログインの前は401で操作しない(rig: Rig, method: str, path: str) -> None:
    response = rig.client.open(path, method=method, json={}, headers=SAME_ORIGIN)
    assert response.status_code == 401
    assert response.get_json() == {}
    assert rig.service.calls == []
    _assert_headers(response)


def test_知らない印は401(rig: Rig) -> None:
    rig.client.set_cookie(COOKIE, "unknown")
    assert rig.client.get("/api/status").status_code == 401


def test_正しいpinで303とクッキー(rig: Rig) -> None:
    response = rig.login()
    assert response.status_code == 303
    assert response.headers["Location"] == "/"
    _assert_headers(response)
    cookie = rig.client.get_cookie(COOKIE)
    assert cookie is not None
    assert rig.sessions.check(cookie.value)
    assert cookie.http_only
    assert cookie.same_site == "Strict"
    assert cookie.path == "/"
    assert cookie.expires is None
    assert not cookie.secure


def test_クッキーには期限も保存の年数も付けない(rig: Rig) -> None:
    header = rig.login().headers["Set-Cookie"]
    lowered = header.lower()
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert "path=/" in lowered
    assert "expires" not in lowered
    assert "max-age" not in lowered
    assert "secure" not in lowered


def test_ログインの後は操作の画面を出す(logged_in: Rig) -> None:
    response = logged_in.client.get("/")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == page.control_html()


def test_まちがったpinはログインの画面に知らせを出す(rig: Rig) -> None:
    response = rig.client.post("/login", data={"pin": WRONG_PIN}, headers=SAME_ORIGIN)
    assert response.status_code == 200
    assert response.get_data(as_text=True) == page.login_html(texts.MSG_WRONG_PIN)
    assert rig.client.get_cookie(COOKIE) is None
    _assert_headers(response)


def test_pinの欄がなくてもまちがいとして知らせる(rig: Rig) -> None:
    response = rig.client.post("/login", data={}, headers=SAME_ORIGIN)
    assert response.status_code == 200
    assert texts.MSG_WRONG_PIN in response.get_data(as_text=True)


def test_5回まちがえると止めている知らせを出す(rig: Rig) -> None:
    for _ in range(LoginGuard.MAX_FAILURES):
        rig.client.post("/login", data={"pin": WRONG_PIN}, headers=SAME_ORIGIN)
    response = rig.login()
    assert response.status_code == 200
    assert response.get_data(as_text=True) == page.login_html(texts.MSG_LOCKED)
    assert rig.client.get_cookie(COOKIE) is None


def test_おわった後は401で印を消す(logged_in: Rig) -> None:
    cookie = logged_in.client.get_cookie(COOKIE)
    assert cookie is not None
    token = cookie.value
    response = logged_in.post("/logout", {})
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert not logged_in.sessions.check(token)
    assert logged_in.client.get_cookie(COOKIE) is None
    # 古い印を送り直しても入れない（2.7）
    logged_in.client.set_cookie(COOKIE, token)
    assert logged_in.client.get("/api/status").status_code == 401
    assert logged_in.client.get("/").get_data(as_text=True) == page.login_html(None)


def test_ログインし直すと前の印を切る(logged_in: Rig) -> None:
    cookie = logged_in.client.get_cookie(COOKIE)
    assert cookie is not None
    old = cookie.value
    assert logged_in.login().status_code == 303
    new = logged_in.client.get_cookie(COOKIE)
    assert new is not None
    assert new.value != old
    assert not logged_in.sessions.check(old)
    assert logged_in.sessions.check(new.value)


def test_まちがえたログインでは前の印を残す(logged_in: Rig) -> None:
    cookie = logged_in.client.get_cookie(COOKIE)
    assert cookie is not None
    logged_in.client.post("/login", data={"pin": WRONG_PIN}, headers=SAME_ORIGIN)
    assert logged_in.sessions.check(cookie.value)


def test_12時間を過ぎた印は401(logged_in: Rig) -> None:
    logged_in.clock.advance(Sessions.LIFETIME_SECONDS + 1)
    assert logged_in.client.get("/api/status").status_code == 401


# --- 出どころと JSON の確かめ ---


def test_出どころの違うPOSTは403で操作しない(logged_in: Rig) -> None:
    response = logged_in.client.post(
        "/api/drive",
        json={"move": "fwd", "seq": 1, "page": "a1"},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert logged_in.service.calls == []
    _assert_headers(response)


def test_出どころのないPOSTは同じサイトの印がなければ403(logged_in: Rig) -> None:
    body = {"move": "fwd", "seq": 1, "page": "a1"}
    assert logged_in.client.post("/api/drive", json=body).status_code == 403
    cross = {"Sec-Fetch-Site": "cross-site"}
    assert logged_in.client.post("/api/drive", json=body, headers=cross).status_code == 403
    same = {"Sec-Fetch-Site": "same-origin"}
    assert logged_in.client.post("/api/drive", json=body, headers=same).status_code == 200
    assert logged_in.service.calls == [("drive", Move.FWD)]


def test_出どころの違うログインは403(rig: Rig) -> None:
    response = rig.client.post(
        "/login", data={"pin": PIN}, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403
    assert rig.client.get_cookie(COOKIE) is None


def test_出どころの違うおわるは403で印を残す(logged_in: Rig) -> None:
    response = logged_in.client.post("/logout", json={}, headers={"Origin": "null"})
    assert response.status_code == 403
    assert logged_in.client.get("/api/status").status_code == 200


def test_APIのPOSTはJSONでなければ403(logged_in: Rig) -> None:
    response = logged_in.client.post(
        "/api/act", data="name=horn", headers=SAME_ORIGIN, content_type="text/plain"
    )
    assert response.status_code == 403
    form = logged_in.client.post("/api/act", data={"name": "horn"}, headers=SAME_ORIGIN)
    assert form.status_code == 403
    assert logged_in.service.calls == []


# --- 状態 ---


def test_状態は決まった見出しで返す(logged_in: Rig) -> None:
    logged_in.service.message = texts.MSG_BUSY
    response = logged_in.client.get("/api/status")
    assert response.status_code == 200
    assert response.get_json() == {
        "message": texts.MSG_BUSY,
        "operator": "me",
        "fast": False,
        "cameras": {"neck": True, "usb": False},
        "dancing": False,
    }
    _assert_headers(response)


# --- 走行 ---


def test_走る命令を渡して返事と状態を返す(logged_in: Rig) -> None:
    response = logged_in.drive("fwd", 1)
    assert response.status_code == 200
    body = response.get_json()
    assert body is not None
    assert body["reply"] == "ok"
    assert set(body) == set(page.STATUS_KEYS)
    assert logged_in.service.calls == [("drive", Move.FWD)]


def test_遅れて届いた小さいseqの命令は捨てる(logged_in: Rig) -> None:
    assert logged_in.drive("fwd", 5).status_code == 200
    assert logged_in.drive("stop", 6).status_code == 200
    late = logged_in.drive("fwd", 4)
    same = logged_in.drive("fwd", 6)
    assert late.status_code == 200
    assert late.get_json() is not None
    assert late.get_json()["reply"] == "ignored"
    assert same.get_json()["reply"] == "ignored"
    assert logged_in.service.calls == [("drive", Move.FWD), ("drive", Move.STOP)]


def test_大きいseqの止める命令は受ける(logged_in: Rig) -> None:
    logged_in.drive("fwd", 7)
    logged_in.drive("stop", 8)
    logged_in.drive("fwd", 9)
    assert logged_in.service.calls == [
        ("drive", Move.FWD),
        ("drive", Move.STOP),
        ("drive", Move.FWD),
    ]


def test_seqは画面ごとに数える(logged_in: Rig) -> None:
    logged_in.drive("fwd", 50, "aaaa")
    logged_in.drive("fwd", 1, "bbbb")
    assert logged_in.service.calls == [("drive", Move.FWD), ("drive", Move.FWD)]


def test_ほかの端末が画面を増やしても操作する端末の表を消さない(logged_in: Rig) -> None:
    logged_in.drive("stop", 10, "f0")
    other = logged_in.app.test_client()
    other.environ_base["REMOTE_ADDR"] = HOME
    other.post("/login", data={"pin": PIN}, headers=SAME_ORIGIN)
    for i in range(1, 40):
        body = {"move": "fwd", "seq": 1, "page": f"c{i:x}"}
        other.post("/api/drive", json=body, headers=SAME_ORIGIN)
    logged_in.service.calls.clear()
    # 遅れて届いた小さい seq は、まだ捨てられる
    assert logged_in.drive("fwd", 9, "f0").get_json()["reply"] == "ignored"
    assert logged_in.service.calls == []


def test_seqの表は小さく古い画面から忘れる(logged_in: Rig) -> None:
    logged_in.drive("fwd", 10, "f0")
    for i in range(1, 40):
        logged_in.drive("fwd", 1, f"c{i:x}")
    # 忘れた画面は初めからやり直せる（表は上限のある大きさ）
    logged_in.service.calls.clear()
    logged_in.drive("fwd", 1, "f0")
    assert logged_in.service.calls == [("drive", Move.FWD)]


@pytest.mark.parametrize(
    "body",
    [
        {"move": "jump", "seq": 1, "page": "a1"},
        {"move": "fwd"},
        {"move": "fwd", "seq": -1, "page": "a1"},
        {"move": "fwd", "seq": True, "page": "a1"},
        {"move": "fwd", "seq": "1", "page": "a1"},
        {"move": "fwd", "seq": 1, "page": ""},
        {"move": "fwd", "seq": 1, "page": "XYZ"},
        {"move": "fwd", "seq": 1, "page": "a" * 33},
        {"move": 1, "seq": 1, "page": "a1"},
        ["fwd"],
        "fwd",
    ],
)
def test_形の違う走る命令は止めるとして扱い400(logged_in: Rig, body: object) -> None:
    response = logged_in.post("/api/drive", body)
    assert response.status_code == 400
    assert logged_in.service.calls == [("drive", Move.STOP)]
    _assert_headers(response)


def test_JSONとして読めない走る命令も止めるとして扱う(logged_in: Rig) -> None:
    response = logged_in.client.post(
        "/api/drive", data="{", headers=SAME_ORIGIN, content_type="application/json"
    )
    assert response.status_code == 400
    assert logged_in.service.calls == [("drive", Move.STOP)]


# --- 首・動き ---


@pytest.mark.parametrize("way", ["up", "down", "left", "right", "center"])
def test_首の向きを渡す(logged_in: Rig, way: str) -> None:
    response = logged_in.post("/api/look", {"way": way})
    assert response.status_code == 200
    assert logged_in.service.calls == [("look", way)]


@pytest.mark.parametrize("body", [{"way": "behind"}, {}, {"way": 1}, []])
def test_知らない首の向きは400(logged_in: Rig, body: object) -> None:
    assert logged_in.post("/api/look", body).status_code == 400
    assert logged_in.service.calls == []


@pytest.mark.parametrize(
    ("name", "call"),
    [
        ("horn", ("horn",)),
        ("dance", ("dance",)),
        ("fast", ("fast", True)),
        ("slow", ("fast", False)),
    ],
)
def test_動きを渡す(logged_in: Rig, name: str, call: tuple[object, ...]) -> None:
    response = logged_in.post("/api/act", {"name": name})
    assert response.status_code == 200
    assert logged_in.service.calls == [call]


def test_知らない動きは400(logged_in: Rig) -> None:
    assert logged_in.post("/api/act", {"name": "fly"}).status_code == 400
    assert logged_in.service.calls == []


def test_断られたら状態の知らせを返す(logged_in: Rig) -> None:
    logged_in.service.reply = Reply.NOT_NOW
    logged_in.service.message = texts.MSG_OTHER_OPERATOR
    body = logged_in.post("/api/act", {"name": "horn"}).get_json()
    assert body is not None
    assert body["reply"] == "not_now"
    assert body["message"] == texts.MSG_OTHER_OPERATOR


# --- 話させる ---


def test_話させる文字を渡す(logged_in: Rig) -> None:
    response = logged_in.post("/api/say", {"text": "こんにちは"})
    assert response.status_code == 200
    assert logged_in.service.calls == [("say", 5)]


def test_空の文字の断りの文を返す(logged_in: Rig) -> None:
    logged_in.service.reply = Reply.EMPTY
    body = logged_in.post("/api/say", {"text": "  "}).get_json()
    assert body is not None
    assert body["reply"] == "empty"
    assert body["message"] == texts.MSG_TEXT_EMPTY


def test_話している途中の断りの文を返す(logged_in: Rig) -> None:
    logged_in.service.reply = Reply.BUSY_SPEAKING
    body = logged_in.post("/api/say", {"text": "あ"}).get_json()
    assert body is not None
    assert body["message"] == texts.MSG_TALKING


def test_2000文字までは受ける(logged_in: Rig) -> None:
    response = logged_in.post("/api/say", {"text": "あ" * MAX_SAY_INPUT})
    assert response.status_code == 200
    assert logged_in.service.calls == [("say", MAX_SAY_INPUT)]


def test_2000文字を超える文字は413(logged_in: Rig) -> None:
    response = logged_in.post("/api/say", {"text": "あ" * (MAX_SAY_INPUT + 1)})
    assert response.status_code == 413
    assert logged_in.service.calls == []
    _assert_headers(response)


@pytest.mark.parametrize("body", [{}, {"text": 1}, ["text"]])
def test_形の違う話させるは400(logged_in: Rig, body: object) -> None:
    assert logged_in.post("/api/say", body).status_code == 400
    assert logged_in.service.calls == []


# --- 写真（9.7） ---


def test_写真の返事は写真の経路だけを返す(logged_in: Rig) -> None:
    logged_in.service.photo = PhotoResult((PHOTO_NAME, "20261108-102030-00-usb.jpg"), Reply.OK)
    response = logged_in.post("/api/photo", {})
    assert response.status_code == 200
    assert response.get_json() == {
        "reply": "ok",
        "message": None,
        "photos": ["/photos/" + PHOTO_NAME, "/photos/20261108-102030-00-usb.jpg"],
    }


def test_撮れないときは知らせを返す(logged_in: Rig) -> None:
    logged_in.service.photo = PhotoResult((), Reply.NOT_NOW)
    logged_in.service.message = texts.MSG_OUT_OF_HOURS
    body = logged_in.post("/api/photo", {}).get_json()
    assert body == {"reply": "not_now", "message": texts.MSG_OUT_OF_HOURS, "photos": []}


def test_1_枚も撮れなかったときは撮れなかったことを出す(logged_in: Rig) -> None:
    logged_in.service.photo = PhotoResult((), Reply.FAILED)
    body = logged_in.post("/api/photo", {}).get_json()
    assert body == {"reply": "failed", "message": texts.MSG_PHOTO_FAILED, "photos": []}  # 9.3


def test_首の失敗では写真の撮れなかった文を出さない(logged_in: Rig) -> None:
    logged_in.service.reply = Reply.FAILED
    body = logged_in.post("/api/look", {"way": "up"}).get_json()
    assert body["message"] != texts.MSG_PHOTO_FAILED


def test_撮った写真をJPEGで返す(logged_in: Rig) -> None:
    logged_in.photo_dir.mkdir()
    (logged_in.photo_dir / PHOTO_NAME).write_bytes(JPEG)
    response = logged_in.client.get("/photos/" + PHOTO_NAME)
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert response.data == JPEG
    _assert_headers(response)
    response.close()


@pytest.mark.parametrize(
    "name",
    ["20261108-102030-00-neck.jpg", "notes.txt", "..%2Fsecret.jpg", "20261108-102030-00-x.jpg"],
)
def test_写真でないものや無いものは404(logged_in: Rig, name: str) -> None:
    logged_in.photo_dir.mkdir()
    (logged_in.photo_dir / "notes.txt").write_text("x")
    response = logged_in.client.get("/photos/" + name)
    assert response.status_code == 404
    _assert_headers(response)


def test_写真の置き場の外のファイルは返さない(logged_in: Rig, tmp_path: Path) -> None:
    logged_in.photo_dir.mkdir()
    outside = tmp_path / PHOTO_NAME
    outside.write_bytes(JPEG)
    (logged_in.photo_dir / PHOTO_NAME).symlink_to(outside)
    assert logged_in.client.get("/photos/" + PHOTO_NAME).status_code == 404


# --- 見出し ---


def test_CSPは設計のとおり() -> None:
    assert CSP == (
        "default-src 'self'; img-src 'self'; style-src 'self'; "
        "script-src 'self'; frame-ancestors 'none'"
    )


def test_知らない経路と違うメソッドにも見出しを付ける(logged_in: Rig) -> None:
    missing = logged_in.client.get("/no-such-path")
    wrong = logged_in.client.get("/api/drive")
    assert missing.status_code == 404
    assert wrong.status_code == 405
    _assert_headers(missing)
    _assert_headers(wrong)


def test_大きすぎる走る命令も止めるとして扱い413(logged_in: Rig) -> None:
    body = {"move": "fwd", "seq": 1, "page": "a1", "pad": "x" * MAX_BODY_BYTES}
    response = logged_in.post("/api/drive", body)
    assert response.status_code == 413
    assert response.data == b""
    assert logged_in.service.calls == [("drive", Move.STOP)]
    _assert_headers(response)


# --- 映像の配信（8.1、8.2、8.6） ---


def _part(jpeg: bytes) -> bytes:
    head = f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n"
    return head.encode("ascii") + jpeg + b"\r\n"


def _open_stream(rig: Rig, cam: str = "neck") -> TestResponse:
    return rig.client.get(f"/stream/{cam}", buffered=False)


def _chunks(response: TestResponse) -> Iterator[bytes]:
    """空でない部分だけ（最初の空の部分は見出しを先に送るためのもの）。"""
    return (chunk for chunk in response.iter_encoded() if chunk)


def _push(rig: Rig) -> None:
    """新しい 1 枚を読ませる。"""
    rig.clock.advance(0.1)
    rig.hub._step()


def test_配信は新しい1枚を長さ付きの部分で送る(logged_in: Rig) -> None:
    response = _open_stream(logged_in)
    assert response.status_code == 200
    assert response.headers["Content-Type"] == STREAM_MIMETYPE
    _assert_headers(response)
    chunks = _chunks(response)
    _push(logged_in)
    assert next(chunks) == _part(JPEG)
    _push(logged_in)
    assert next(chunks) == _part(JPEG2)
    response.close()


def test_配信の間は席を持ち閉じると返す(logged_in: Rig) -> None:
    response = _open_stream(logged_in)
    assert logged_in.hub.viewers() == 1
    chunks = _chunks(response)
    _push(logged_in)
    next(chunks)
    response.close()
    assert logged_in.hub.viewers() == 0


def test_最初の1枚の前に閉じても席を返す(logged_in: Rig) -> None:
    response = _open_stream(logged_in)
    assert logged_in.hub.viewers() == 1
    response.close()
    assert logged_in.hub.viewers() == 0


def test_送り始める前に応答を閉じても席を返す(logged_in: Rig) -> None:
    cookie = logged_in.client.get_cookie(COOKIE)
    assert cookie is not None
    with logged_in.app.test_request_context(
        "/stream/neck",
        headers={"Cookie": f"{COOKIE}={cookie.value}"},
        environ_base={"REMOTE_ADDR": HOME},
    ):
        response = logged_in.app.full_dispatch_request()
    assert response.status_code == 200
    assert logged_in.hub.viewers() == 1
    response.close()  # 生成を 1 度も進めずに閉じる
    assert logged_in.hub.viewers() == 0


def test_4つ目の端末は503で席は増えない(logged_in: Rig) -> None:
    opened = [_open_stream(logged_in) for _ in range(FrameHub.MAX_VIEWERS)]
    assert all(r.status_code == 200 for r in opened)
    full = _open_stream(logged_in)
    assert full.status_code == 503
    assert full.data == b""
    _assert_headers(full)
    assert logged_in.hub.viewers() == FrameHub.MAX_VIEWERS
    for r in opened:
        r.close()
    assert logged_in.hub.viewers() == 0


@pytest.mark.parametrize("cam", ["usb", "front", ""])
def test_ないカメラの配信は404(logged_in: Rig, cam: str) -> None:
    response = logged_in.client.get(f"/stream/{cam}")
    assert response.status_code == 404


def test_ログインしていない配信は401で席を取らない(rig: Rig) -> None:
    response = _open_stream(rig)
    assert response.status_code == 401
    assert rig.hub.viewers() == 0


def test_時間帯の外になると配信を終えて席を返す(logged_in: Rig) -> None:
    response = _open_stream(logged_in)
    chunks = _chunks(response)
    _push(logged_in)
    assert next(chunks) == _part(JPEG)
    logged_in.service.streaming = False
    _push(logged_in)
    with pytest.raises(StopIteration):
        next(chunks)
    assert logged_in.hub.viewers() == 0
    response.close()


def test_ログアウトすると配信を終える(logged_in: Rig) -> None:
    response = _open_stream(logged_in)
    chunks = _chunks(response)
    _push(logged_in)
    assert next(chunks) == _part(JPEG)
    assert logged_in.post("/logout", {}).status_code == 200
    _push(logged_in)
    with pytest.raises(StopIteration):
        next(chunks)
    assert logged_in.hub.viewers() == 0
    response.close()


def test_止める合図で配信を終える(logged_in: Rig) -> None:
    response = _open_stream(logged_in)
    chunks = _chunks(response)
    _push(logged_in)
    assert next(chunks) == _part(JPEG)
    logged_in.stop_event.set()
    _push(logged_in)
    with pytest.raises(StopIteration):
        next(chunks)
    assert logged_in.hub.viewers() == 0
    response.close()


def test_新しい1枚がなければ待って続けてよいかを確かめ直す(logged_in: Rig) -> None:
    response = _open_stream(logged_in)
    chunks = _chunks(response)
    timer = threading.Timer(0.2, logged_in.stop_event.set)  # 1 枚もないまま待つ間に止める合図
    timer.start()
    started = time.monotonic()
    with pytest.raises(StopIteration):
        next(chunks)
    timer.join()
    assert time.monotonic() - started < 2.0  # 0.5 秒ごとに確かめ直す
    assert logged_in.hub.viewers() == 0
    response.close()
