"""Web の経路・クッキー・確かめの順（要件 1.1、1.2、2.1〜2.4、2.6、2.7、9.7）。

- Flask はこのモジュールだけが読む。このモジュールは本体（feature）の関数の中で読まれる
- 要求ごとに、家のアドレスか → ログインの印 → 状態を変える要求の出どころと JSON、の順で確かめる
  - 家の外は本文なしの 403（1.2）。知らない経路も先にここで断る
  - Host が家のアドレスか家の名前（localhost・点のない名前・.local）でなければ 403
    （DNS の付け替えで外のサイトの名前から届く要求を断る）
  - 印がないかだめなら 401（JSON `{}`）。`/`・`/login`・`/app.js`・`/app.css` は印なしで受ける
  - POST は Origin が要求の Host と同じこと（Origin がなければ `Sec-Fetch-Site: same-origin`）。
    `/api/*` の POST は `application/json` だけ。だめなら 403
- 意味は RemoteService・LoginGuard・Sessions に任せる。ここは形を確かめて渡すだけ
- 形の違う走る命令は、止める命令として渡してから 400 を返す
- 走る命令は画面（page）ごとに見た一番大きい seq を小さな表で覚え、それ以下の命令を捨てる
  （遅れて届いた命令が STOP を越えないように。4.3）。表は印ごとに分け、ほかの端末が画面を
  増やしても、操作する端末の画面を忘れない
- ログインし直すと、まだ生きている前の印を切ってから新しい印を出す
- まちがった pin と止めている間は、ログインの画面に知らせを出して 200 で返す（画面を出し直す）
- すべての応答に、保存しない・枠に入れない・型を推し量らない・CSP の見出しを付ける
- 印・pin・入力・page・seq・写真の名前・端末のアドレスをログに出さない。要求のログもここでは出さない
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Final, Protocol

from flask import Flask, Response, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.wrappers import Response as BaseResponse

from . import page, texts
from .access import is_home_address, is_home_host
from .drive_rules import Move
from .login import LoginGuard, LoginResult, Sessions
from .media import Camera, FrameHub
from .photos import PhotoStore
from .service import PhotoResult, Reply, Status, Way

__all__ = [
    "COOKIE",
    "CSP",
    "MAX_SAY_INPUT",
    "OperatorService",
    "create_app",
]

logger = logging.getLogger(__name__)

COOKIE: Final = "remote_session"
CSP: Final = (
    "default-src 'self'; img-src 'self'; style-src 'self'; "
    "script-src 'self'; frame-ancestors 'none'"
)
MAX_SAY_INPUT: Final = 2000  # 話させる文字は 2000 文字まで受けて、サービスが 120 に切る
MAX_BODY_BYTES: Final = 64 * 1024  # 要求の本文の上限（2000 文字を \uXXXX で送っても収まる）
MAX_SEQ: Final = 2**53  # JavaScript の整数で数えられる上限
MAX_PAGES_PER_TOKEN: Final = 4  # 印ごとに seq を覚える画面の数（古い画面から忘れる）
MAX_TOKENS: Final = 64  # seq を覚える印の数（古く使った印から忘れる）
_PAGE_ID: Final = re.compile(r"[0-9a-f]{1,32}")

# 印なしで受ける経路（ログインの画面と、その部品）
_OPEN_PATHS: Final = frozenset({"/", "/login", "/app.js", "/app.css"})
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_MOVES: Final[Mapping[str, Move]] = {move.value: move for move in Move}
_WAYS: Final[Mapping[str, Way]] = {
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "center": "center",
}
_ACTS: Final = frozenset({"horn", "dance", "fast", "slow"})
# 断りの返事の文。ないものは状態の知らせ（使えるか・ほかの端末など）を使う
_REPLY_MESSAGES: Final[Mapping[Reply, str]] = {
    Reply.EMPTY: texts.MSG_TEXT_EMPTY,  # 10.3
    Reply.BUSY_SPEAKING: texts.MSG_TALKING,  # 10.4
}
_LOGIN_MESSAGES: Final[Mapping[LoginResult, str]] = {
    LoginResult.WRONG: texts.MSG_WRONG_PIN,  # 2.3
    LoginResult.LOCKED: texts.MSG_LOCKED,  # 2.4
}
_HEADERS: Final[Mapping[str, str]] = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": CSP,
}


class OperatorService(Protocol):
    """web が使うサービスの操作（RemoteService が満たす）。"""

    def drive(self, token: str, move: Move) -> Reply: ...

    def look(self, token: str, way: Way) -> Reply: ...

    def horn(self, token: str) -> Reply: ...

    def dance(self, token: str) -> Reply: ...

    def say_text(self, token: str, text: str) -> Reply: ...

    def set_fast(self, token: str, fast: bool) -> Reply: ...

    def take_photo(self, token: str) -> PhotoResult: ...

    def status(self, token: str) -> Status: ...


class _SeqTable:
    """印ごと・画面ごとに、見た一番大きい seq を覚える。

    画面は印の中で古いものから忘れる（印ごとに MAX_PAGES_PER_TOKEN まで）。ほかの印が
    画面を増やしても、自分の画面は消えない。印は古く使ったものから忘れる（MAX_TOKENS まで）。
    """

    def __init__(self) -> None:
        self._tokens: OrderedDict[str, OrderedDict[str, int]] = OrderedDict()

    def accept(self, token: str, page_id: str, seq: int) -> bool:
        """seq が覚えた値より大きければ覚えて True。同じか小さければ False。"""
        pages = self._tokens.get(token)
        if pages is None:
            pages = self._tokens[token] = OrderedDict()
        self._tokens.move_to_end(token)
        while len(self._tokens) > MAX_TOKENS:
            self._tokens.popitem(last=False)
        last = pages.get(page_id)
        if last is not None:
            pages.move_to_end(page_id)
            if seq <= last:
                return False
        pages[page_id] = seq
        while len(pages) > MAX_PAGES_PER_TOKEN:
            pages.popitem(last=False)
        return True

    def forget(self, token: str) -> None:
        self._tokens.pop(token, None)


def _empty(status: int) -> Response:
    return Response(b"", status=status)


def _json_body() -> object:
    """JSON の本文。読めなければ None。"""
    return request.get_json(silent=True)


def _drive_body(body: object) -> tuple[Move, int, str] | None:
    """{move, seq, page} を確かめる。形が違えば None。"""
    if not isinstance(body, dict):
        return None
    move = body.get("move")
    seq = body.get("seq")
    page_id = body.get("page")
    if not isinstance(move, str) or move not in _MOVES:
        return None
    if type(seq) is not int or not 0 <= seq <= MAX_SEQ:
        return None
    if not isinstance(page_id, str) or not _PAGE_ID.fullmatch(page_id):
        return None
    return _MOVES[move], seq, page_id


def _same_origin() -> bool:
    """出どころが要求の Host と同じか。Origin がなければ Sec-Fetch-Site で見る。"""
    origin = request.headers.get("Origin")
    if origin is None:
        return request.headers.get("Sec-Fetch-Site") == "same-origin"
    return origin.lower() == request.host_url.rstrip("/").lower()


def create_app(
    *,
    service: OperatorService,
    guard: LoginGuard,
    sessions: Sessions,
    photos: PhotoStore,
    hubs: Mapping[Camera, FrameHub] | None = None,
) -> Flask:
    """経路を持つ Flask の app を作る。hubs は映像の配信（/stream）で使う。"""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    app.extensions["remote_hubs"] = dict(hubs) if hubs is not None else {}
    seqs = _SeqTable()
    drive_lock = threading.Lock()  # 捨てるかの判定と渡す順を 1 つずつにする

    def status_json(token: str, reply: Reply | None = None) -> dict[str, object]:
        status = service.status(token)
        body: dict[str, object] = {}
        message = status.message
        if reply is not None:  # 操作の返事は、断りの文を状態の知らせより先にする
            body["reply"] = reply.value
            message = _REPLY_MESSAGES.get(reply, message)
        body["message"] = message
        body["operator"] = status.operator
        body["fast"] = status.fast
        body["cameras"] = {camera.value: bool(status.cameras.get(camera)) for camera in Camera}
        body["dancing"] = status.dancing
        return body

    def answer(reply: Reply) -> Response:
        return jsonify(status_json(g.token, reply))

    def login_page(message: str | None) -> Response:
        return Response(page.login_html(message), mimetype="text/html")

    @app.before_request
    def check_request() -> Response | None:
        # 1. 家のアドレスか（1.1、1.2）
        if not is_home_address(request.remote_addr):
            return _empty(403)
        # Host が家のアドレスか家の名前か（DNS の付け替えを防ぐ）
        if not is_home_host(request.host):
            return _empty(403)
        # 2. ログインの印（2.1）
        token = request.cookies.get(COOKIE)
        g.token = token if sessions.check(token) else None
        if request.path not in _OPEN_PATHS and g.token is None:
            return Response(b"{}", status=401, mimetype="application/json")
        # 3. 状態を変える要求の出どころと JSON
        if request.method not in _SAFE_METHODS:
            if not _same_origin():
                return _empty(403)
            if request.path.startswith("/api/") and request.mimetype != "application/json":
                return _empty(403)
        return None

    @app.after_request
    def add_headers(response: Response) -> Response:
        for name, value in _HEADERS.items():
            response.headers[name] = value
        return response

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException) -> BaseResponse:
        # 知らない経路・違うメソッド・大きすぎる本文など。見出し（Allow など）は残し、本文は出さない
        response = error.get_response()
        response.set_data(b"")
        return response

    @app.errorhandler(Exception)
    def unexpected(error: Exception) -> Response:
        logger.warning("Web の要求の処理に失敗しました: %s", type(error).__name__)
        return _empty(500)

    @app.get("/")
    def index() -> Response:
        if g.token is None:
            return login_page(None)
        return Response(page.control_html(), mimetype="text/html")

    @app.get("/app.js")
    def app_js() -> Response:
        return Response(page.APP_JS, content_type="application/javascript; charset=utf-8")

    @app.get("/app.css")
    def app_css() -> Response:
        return Response(page.APP_CSS, mimetype="text/css")

    @app.post("/login")
    def login() -> Response:
        entered = request.form.get("pin", "")
        result = guard.try_login(entered)
        if result is not LoginResult.OK:
            return login_page(_LOGIN_MESSAGES[result])
        old: str | None = g.token
        if old is not None:  # まだ生きている前の印は切る
            sessions.end(old)
            with drive_lock:
                seqs.forget(old)
        response = Response(status=303, headers={"Location": "/"})
        response.set_cookie(
            COOKIE, sessions.start(), path="/", httponly=True, samesite="Strict", secure=False
        )
        return response

    @app.post("/logout")
    def logout() -> Response:
        token: str = g.token
        sessions.end(token)
        with drive_lock:
            seqs.forget(token)
        response = jsonify({"ok": True})
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="Strict")
        return response

    @app.get("/api/status")
    def api_status() -> Response:
        return jsonify(status_json(g.token))

    @app.post("/api/drive")
    def api_drive() -> Response:
        token: str = g.token
        try:
            body = _json_body()
        except HTTPException:
            service.drive(token, Move.STOP)  # 大きすぎる本文も止める（413 はそのまま返す）
            raise
        parsed = _drive_body(body)
        if parsed is None:
            service.drive(token, Move.STOP)  # 形の違う命令は止める
            return Response(b"{}", status=400, mimetype="application/json")
        move, seq, page_id = parsed
        with drive_lock:
            if not seqs.accept(token, page_id, seq):
                return answer(Reply.IGNORED)
            reply = service.drive(token, move)
        return answer(reply)

    @app.post("/api/look")
    def api_look() -> Response:
        body = _json_body()
        way = body.get("way") if isinstance(body, dict) else None
        if not isinstance(way, str) or way not in _WAYS:
            return _empty(400)
        return answer(service.look(g.token, _WAYS[way]))

    @app.post("/api/act")
    def api_act() -> Response:
        body = _json_body()
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or name not in _ACTS:
            return _empty(400)
        token: str = g.token
        if name == "horn":
            reply = service.horn(token)
        elif name == "dance":
            reply = service.dance(token)
        else:
            reply = service.set_fast(token, name == "fast")
        return answer(reply)

    @app.post("/api/say")
    def api_say() -> Response:
        body = _json_body()
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            return _empty(400)
        if len(text) > MAX_SAY_INPUT:
            return _empty(413)
        return answer(service.say_text(g.token, text))

    @app.post("/api/photo")
    def api_photo() -> Response:
        token: str = g.token
        result = service.take_photo(token)  # 話し終えて撮るまで、このスレッドで待つ
        body = status_json(token, result.reply)
        return jsonify(
            {
                "reply": body["reply"],
                "message": body["message"],
                "photos": [f"/photos/{name}" for name in result.names],
            }
        )

    @app.get("/photos/<name>")
    def photo(name: str) -> Response:
        path = photos.path_for(name)
        if path is None:
            return _empty(404)
        return send_file(path, mimetype="image/jpeg", max_age=0)

    return app
