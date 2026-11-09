"""Web のサーバーの起動・停止と、要求のログを出さないこと（要件 1.5、2.8、8.6）。

本物のポート（127.0.0.1 の空いたポート）で確かめる。サービスは偽物、ログインの部品は本物。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from pydantic import SecretStr
from remote_fakes import FakeClock, FakeFrameSource
from test_remote_web import PIN, FakeService

from osekkai.features.remote.login import LoginGuard, Sessions
from osekkai.features.remote.media import Camera, FrameHub
from osekkai.features.remote.photos import PhotoStore
from osekkai.features.remote.web import (
    COOKIE,
    SOCKET_TIMEOUT_S,
    QuietHandler,
    WebServer,
    create_app,
)

LOOPBACK: Final = "127.0.0.1"
WAIT_S: Final = 3.0
JPEG: Final = b"\xff\xd8\xff\xe0jpeg\xff\xd9"


class Served:
    """本物のポートで待ち受けるサーバーと、その部品。"""

    def __init__(self, tmp_path: Path) -> None:
        clock = FakeClock()
        self.sessions = Sessions(clock)
        self.hub = FrameHub(Camera.NECK, FakeFrameSource([JPEG] * 1000), clock)
        self.stop_event = threading.Event()
        app = create_app(
            service=FakeService(),
            guard=LoginGuard(SecretStr(PIN), clock),
            sessions=self.sessions,
            photos=PhotoStore(tmp_path / "photos", clock),
            hubs={Camera.NECK: self.hub},
            stop_event=self.stop_event,
        )
        self.server = WebServer(app, host=LOOPBACK, port=0, stop_event=self.stop_event)

    def url(self, path: str) -> str:
        return f"http://{LOOPBACK}:{self.server.port}{path}"


def _web_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "remote-web"]


@pytest.fixture
def served(tmp_path: Path) -> Iterator[Served]:
    s = Served(tmp_path)
    s.server.start()
    yield s
    s.server.stop()
    s.hub.close()


def test_補助のスレッドで待ち受けてCSSを返す(served: Served) -> None:
    threads = _web_threads()
    assert len(threads) == 1
    assert threads[0].daemon
    with urllib.request.urlopen(served.url("/app.css"), timeout=WAIT_S) as response:
        assert response.status == 200


def test_止めるとスレッドが終わり何度止めてもよい(served: Served) -> None:
    served.server.stop()
    served.server.stop()
    assert _web_threads() == []
    assert served.stop_event.is_set()


def test_配信の途中でもすぐ止まり配信も終わる(served: Served) -> None:
    token = served.sessions.start()
    with socket.create_connection((LOOPBACK, served.server.port), timeout=WAIT_S) as sock:
        request = (
            f"GET /stream/neck HTTP/1.1\r\nHost: {LOOPBACK}\r\nCookie: {COOKIE}={token}\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        head = sock.recv(4096)
        assert head.startswith(b"HTTP/1.1 200")
        started = time.monotonic()
        served.server.stop()
        assert time.monotonic() - started <= WAIT_S
        assert _web_threads() == []
        deadline = time.monotonic() + WAIT_S
        while served.hub.viewers() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert served.hub.viewers() == 0  # 止める合図で配信が終わり、席を返す


def test_使えないポートはOSErrorを返す(
    served: Served, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    other = WebServer(
        create_app(
            service=FakeService(),
            guard=LoginGuard(SecretStr(PIN), clock),
            sessions=Sessions(clock),
            photos=PhotoStore(tmp_path / "other", clock),
        ),
        host=LOOPBACK,
        port=served.server.port,
        stop_event=threading.Event(),
    )
    with pytest.raises(OSError):
        other.start()
    assert len(_web_threads()) == 1
    assert capsys.readouterr().err == ""  # werkzeug の案内も出さない


def test_要求のログに端末のアドレスと経路を出さない(
    served: Served, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    with urllib.request.urlopen(served.url("/app.css"), timeout=WAIT_S) as response:
        response.read()
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(served.url("/no-such-path"), timeout=WAIT_S)
    served.server.stop()
    captured = capsys.readouterr()
    for text in (caplog.text, captured.out, captured.err):
        assert LOOPBACK not in text
        assert "/app.css" not in text
        assert "no-such-path" not in text


# --- 読まない端末（電波の外・隠れた画面） ---


def test_送る待ちの上限は決まった値() -> None:
    assert QuietHandler.timeout == SOCKET_TIMEOUT_S


def test_読まない端末への配信は送る待ちの上限で終わり席を返す(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(QuietHandler, "timeout", 1.0)
    clock = FakeClock()
    sessions = Sessions(clock)
    big = b"\xff\xd8" + b"a" * 60000 + b"\xff\xd9"
    hub = FrameHub(Camera.NECK, FakeFrameSource([big] * 100000), clock)
    stop_event = threading.Event()
    app = create_app(
        service=FakeService(),
        guard=LoginGuard(SecretStr(PIN), clock),
        sessions=sessions,
        photos=PhotoStore(tmp_path / "photos", clock),
        hubs={Camera.NECK: hub},
        stop_event=stop_event,
    )
    server = WebServer(app, host=LOOPBACK, port=0, stop_event=stop_event)
    server.start()
    ticking = threading.Event()

    def advance() -> None:  # 偽の時計を進めて、新しい 1 枚を読ませ続ける
        while not ticking.is_set():
            clock.advance(0.1)
            time.sleep(0.02)

    ticker = threading.Thread(target=advance, daemon=True)
    ticker.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sock.settimeout(WAIT_S)
            sock.connect((LOOPBACK, server.port))
            token = sessions.start()
            request = (
                f"GET /stream/neck HTTP/1.1\r\nHost: {LOOPBACK}\r\nCookie: {COOKIE}={token}\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            assert sock.recv(64).startswith(b"HTTP/1.1 200")
            deadline = time.monotonic() + 1.0 + 4.0  # 待ちの上限と余裕
            while hub.viewers() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert hub.viewers() == 0  # 読まないまま、席が返る
            started = time.monotonic()
            server.stop()
            assert time.monotonic() - started <= WAIT_S
            assert _web_threads() == []
    finally:
        ticking.set()
        ticker.join(WAIT_S)
        server.stop()
        hub.close()
    assert LOOPBACK not in capsys.readouterr().err


def test_要求の処理の思わぬ誤りでも端末のアドレスを出さない(
    served: Served, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(self: QuietHandler) -> None:
        raise RuntimeError("hidden")

    monkeypatch.setattr(QuietHandler, "handle", broken)
    with socket.create_connection((LOOPBACK, served.server.port), timeout=WAIT_S) as sock:
        sock.sendall(f"GET /app.css HTTP/1.1\r\nHost: {LOOPBACK}\r\n\r\n".encode("ascii"))
        assert sock.recv(64) == b""  # 応えずに閉じる
    served.server.stop()
    err = capsys.readouterr().err
    assert LOOPBACK not in err
    assert "hidden" not in err
