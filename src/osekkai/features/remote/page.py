"""ログインと操作の画面の HTML・CSS・JavaScript（文字列）。

要件 2.7、4.1、4.3、6.4、8.2、8.3、9.2。
Flask を読まない。web がここの文字列をそのまま返す。
CSP（default-src 'self'; style-src 'self'; script-src 'self'）に合わせて、
インラインのスクリプト・見た目・on の属性・外の資源を使わない。
JavaScript は /app.js、CSS は /app.css から返す。

状態の JSON（/api/status と操作の返事）の見出しの名前は STATUS_KEYS のとおり:
- reply: 操作の結果（Reply の値）。/api/status にはない
- message: 端末に出す知らせの文か null。操作の返事では、返事の文（もじを いれてね など）を先に入れる
- operator: "me"・"other"・"none"
- fast: 速さ（真ならはやい）
- cameras: {"neck": 真偽, "usb": 真偽}（8.3）
- dancing: ダンス中か
/api/drive の本文は {move, seq, page}（DRIVE_KEYS）。
写真の返事は {reply, message, photos: ["/photos/<名前>"]}。
"""

from __future__ import annotations

import html
import json
from typing import Final

from . import texts

STATUS_KEYS: Final = ("reply", "message", "operator", "fast", "cameras", "dancing")
# /api/drive の本文の見出し。seq は画面ごとに 1 ずつ増える整数（STOP にも付く）。
# page は画面ごとの短い乱数の 16 進の文字列。web は同じ page で、見た中で一番大きい seq より
# 小さい seq の命令を捨てる（遅れて届いた命令が STOP を越えないように。4.3）
DRIVE_KEYS: Final = ("move", "seq", "page")

DRIVE_EVERY_MS: Final = 150  # 押している間に送る間隔（4.1）
STATUS_EVERY_MS: Final = 1000  # 状態を読む間隔
STREAM_RETRY_MS: Final = 5000  # 映像を見られなかったときに付け直すまで

# 画面のボタンの文字（要件の文でないもの）
TITLE: Final = "ラジコン"
LABEL_PIN: Final = "ばんごう"
LABEL_LOGIN: Final = "はじめる"
LABEL_STOP: Final = "とめる"
LABEL_HORN: Final = "ブッブー"
LABEL_DANCE: Final = "ダンス"
LABEL_SLOW: Final = "ゆっくり"
LABEL_FAST: Final = "はやい"
LABEL_PHOTO: Final = "しゃしん"
LABEL_SAY: Final = "はなしてもらう"
LABEL_SEND: Final = "おくる"
LABEL_LOGOUT: Final = "おわる"
LABEL_CLOSE: Final = "とじる"
LABEL_NECK: Final = "くびの カメラ"
LABEL_USB: Final = "まえの カメラ"

# (命令, 文字)。上の段が前、下の段が後ろ
_DRIVE_FRONT: Final = (("left", "ひだりまえ"), ("fwd", "まえ"), ("right", "みぎまえ"))
_DRIVE_BACK: Final = (
    ("back_left", "ひだりうしろ"),
    ("back", "うしろ"),
    ("back_right", "みぎうしろ"),
)
# (向き, 文字, 置く場所)
_LOOK: Final = (
    ("up", "うえ", "look-up"),
    ("left", "ひだり", "look-left"),
    ("center", "まんなか", "look-center"),
    ("right", "みぎ", "look-right"),
    ("down", "した", "look-down"),
)

MAX_PIN: Final = 8
MAX_SAY: Final = 120


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def _head() -> str:
    return (
        "<!doctype html>\n"
        '<html lang="ja">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(TITLE)}</title>\n"
        '<link rel="stylesheet" href="/app.css">\n'
    )


def login_html(message: str | None) -> str:
    """ログインの画面。message は「ばんごうが ちがうよ」などの知らせ（2.3、2.4）。"""
    notice = f'<p class="message" role="alert">{_e(message)}</p>\n' if message else ""
    return (
        _head()
        + "</head>\n"
        + '<body class="login">\n'
        + f"<h1>{_e(TITLE)}</h1>\n"
        + notice
        + '<form method="post" action="/login" autocomplete="off">\n'
        + f'<label for="pin">{_e(LABEL_PIN)}</label>\n'
        + '<input id="pin" name="pin" type="password" inputmode="numeric"'
        + f' autocomplete="off" maxlength="{MAX_PIN}" required>\n'
        + f'<button type="submit">{_e(LABEL_LOGIN)}</button>\n'
        + "</form>\n"
        + "</body>\n"
        + "</html>\n"
    )


def _drive_button(move: str, label: str) -> str:
    return f'<button type="button" class="drive" data-move="{move}">{_e(label)}</button>\n'


def _video(cam: str, label: str) -> str:
    return (
        f'<figure class="video" id="frame-{cam}">\n'
        f'<img id="video-{cam}" alt="{_e(label)}">\n'
        f'<p class="overlay" id="overlay-{cam}" hidden></p>\n'
        f"<figcaption>{_e(label)}</figcaption>\n"
        "</figure>\n"
    )


def control_html() -> str:
    """操作の画面。"""
    front = "".join(_drive_button(m, label) for m, label in _DRIVE_FRONT)
    back = "".join(_drive_button(m, label) for m, label in _DRIVE_BACK)
    look = "".join(
        f'<button type="button" class="look {place}" data-way="{way}">{_e(label)}</button>\n'
        for way, label, place in _LOOK
    )
    return (
        _head()
        + '<script src="/app.js" defer></script>\n'
        + "</head>\n"
        + '<body class="control">\n'
        + '<p class="message" id="message" role="status" hidden></p>\n'
        + '<section class="videos">\n'
        + _video("neck", LABEL_NECK)
        + _video("usb", LABEL_USB)
        + "</section>\n"
        + '<section class="drive-pad">\n'
        + front
        + back
        + f'<p class="guide">{_e(texts.MSG_BACK_GUIDE)}</p>\n'
        + f'<button type="button" class="drive stop" data-move="stop">{_e(LABEL_STOP)}</button>\n'
        + "</section>\n"
        + '<section class="look-pad">\n'
        + look
        + "</section>\n"
        + '<section class="acts">\n'
        + f'<button type="button" data-act="horn">{_e(LABEL_HORN)}</button>\n'
        + '<button type="button" data-act="dance" aria-pressed="false">'
        + f"{_e(LABEL_DANCE)}</button>\n"
        + f'<button type="button" id="speed" aria-pressed="false">{_e(LABEL_SLOW)}</button>\n'
        + f'<button type="button" id="photo">{_e(LABEL_PHOTO)}</button>\n'
        + "</section>\n"
        + '<section class="say">\n'
        + f'<label for="say-text">{_e(LABEL_SAY)}</label>\n'
        + f'<input id="say-text" type="text" maxlength="{MAX_SAY}" autocomplete="off">\n'
        + f'<button type="button" id="say-send">{_e(LABEL_SEND)}</button>\n'
        + "</section>\n"
        + f'<button type="button" id="logout" class="logout">{_e(LABEL_LOGOUT)}</button>\n'
        + '<section class="photos" id="photos" hidden>\n'
        + '<div id="photo-list"></div>\n'
        + f'<button type="button" id="photos-close">{_e(LABEL_CLOSE)}</button>\n'
        + "</section>\n"
        + "</body>\n"
        + "</html>\n"
    )


_JS_TEXTS: Final = json.dumps(
    {
        "streamFull": texts.MSG_STREAM_FULL,  # 8.2
        "cameraUnavailable": texts.MSG_CAMERA_UNAVAILABLE,  # 8.3
        "slow": LABEL_SLOW,
        "fast": LABEL_FAST,
    }
)

_JS_TEMPLATE: Final = """\
"use strict";
(function () {
  const T = __TEXTS__;
  const DRIVE_EVERY_MS = __DRIVE_EVERY_MS__;
  const STATUS_EVERY_MS = __STATUS_EVERY_MS__;
  const STREAM_RETRY_MS = __STREAM_RETRY_MS__;
  const CAMERAS = ["neck", "usb"];

  let driveTimer = null;
  let driving = false;
  let moveInFlight = false; // 走る命令（STOP でない）を送り中か。送り中は次を送らない
  let stopAgain = false; // 送り中の命令が終わったら STOP をもう一度送る
  let seq = 0;
  const PAGE_ID = makePageId();
  let fast = false;
  let hidden = document.visibilityState === "hidden";
  const cameraOk = { neck: true, usb: true };
  const live = { neck: false, usb: false };
  const failed = { neck: false, usb: false };
  const retryAt = { neck: 0, usb: 0 };

  function makePageId() {
    const bytes = new Uint8Array(6);
    try {
      crypto.getRandomValues(bytes);
    } catch (err) {
      for (let i = 0; i < bytes.length; i++) {
        bytes[i] = Math.floor(Math.random() * 256);
      }
    }
    return Array.from(bytes, function (b) { return (b + 256).toString(16).slice(1); }).join("");
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function handle(res) {
    if (res.status === 401) {
      location.reload();
      return null;
    }
    if (!res.ok) {
      return null;
    }
    return res.json().catch(function () { return null; });
  }

  function post(path, body, keepalive) {
    return fetch(path, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      keepalive: keepalive === true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(handle)
      .catch(function () { return null; });
  }

  function showMessage(text) {
    const el = byId("message");
    el.textContent = text || "";
    el.hidden = !text;
  }

  function setOverlay(cam, text) {
    const el = byId("overlay-" + cam);
    el.textContent = text || "";
    el.hidden = !text;
  }

  function applyStatus(s) {
    if (!s) {
      return;
    }
    if ("message" in s) {
      showMessage(typeof s.message === "string" ? s.message : "");
    }
    if (typeof s.operator === "string") {
      document.body.dataset.operator = s.operator;
    }
    if (typeof s.fast === "boolean") {
      fast = s.fast;
      const speed = byId("speed");
      speed.textContent = fast ? T.fast : T.slow;
      speed.setAttribute("aria-pressed", fast ? "true" : "false");
    }
    if (typeof s.dancing === "boolean") {
      const dance = document.querySelector("[data-act=dance]");
      dance.setAttribute("aria-pressed", s.dancing ? "true" : "false");
    }
    if (s.cameras && typeof s.cameras === "object") {
      CAMERAS.forEach(function (cam) {
        const ok = s.cameras[cam] === true;
        if (ok === cameraOk[cam]) {
          return;
        }
        cameraOk[cam] = ok;
        if (ok) {
          attach(cam);
        } else {
          detach(cam);
          setOverlay(cam, T.cameraUnavailable);
        }
      });
    }
  }

  // --- 映像 ---

  function attach(cam) {
    if (hidden) {
      return;
    }
    if (!cameraOk[cam]) {
      setOverlay(cam, T.cameraUnavailable);
      return;
    }
    failed[cam] = false;
    setOverlay(cam, "");
    live[cam] = true;
    byId("video-" + cam).src = "/stream/" + cam + "?t=" + Date.now();
  }

  function detach(cam) {
    live[cam] = false;
    byId("video-" + cam).src = "";
  }

  function onVideoError(cam) {
    if (!live[cam]) {
      return;
    }
    live[cam] = false;
    failed[cam] = true;
    retryAt[cam] = Date.now() + STREAM_RETRY_MS;
    setOverlay(cam, cameraOk[cam] ? T.streamFull : T.cameraUnavailable);
  }

  function retryVideos() {
    const now = Date.now();
    CAMERAS.forEach(function (cam) {
      if (failed[cam] && cameraOk[cam] && now >= retryAt[cam]) {
        attach(cam);
      }
    });
  }

  // --- 走る ---

  function sendDrive(move, keepalive) {
    seq += 1;
    return post("/api/drive", { move: move, seq: seq, page: PAGE_ID }, keepalive);
  }

  function moveSettled() {
    moveInFlight = false;
    if (stopAgain) {
      stopAgain = false;
      if (!driving) {
        sendStop();
      }
    }
  }

  function sendMove(move) {
    if (moveInFlight) {
      return;
    }
    moveInFlight = true;
    sendDrive(move, false)
      .then(function (s) {
        moveSettled();
        applyStatus(s);
      })
      .catch(function () { moveSettled(); });
  }

  function sendStop() {
    // STOP は画面を閉じても届くように keepalive で送る
    sendDrive("stop", true).then(applyStatus);
  }

  function clearDriveTimer() {
    if (driveTimer !== null) {
      clearInterval(driveTimer);
      driveTimer = null;
    }
  }

  function startDriving(move) {
    clearDriveTimer();
    driving = true;
    sendMove(move);
    driveTimer = setInterval(function () { sendMove(move); }, DRIVE_EVERY_MS);
  }

  function stopDriving() {
    clearDriveTimer();
    driving = false;
    sendStop();
    if (moveInFlight) {
      stopAgain = true;
    }
  }

  function stopIfDriving() {
    if (driving) {
      stopDriving();
    }
  }

  function setupDrive(button) {
    const move = button.dataset.move;
    button.addEventListener("contextmenu", function (e) { e.preventDefault(); });
    // 長押しで、ブラウザの選択やメニューが働いて押しが切れないようにする
    button.addEventListener("touchstart", function (e) { e.preventDefault(); }, { passive: false });
    if (move === "stop") {
      button.addEventListener("pointerdown", function (e) {
        e.preventDefault();
        stopDriving();
      });
      return;
    }
    button.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      try {
        button.setPointerCapture(e.pointerId);
      } catch (err) {
        // 捕まえられなくても、離したときの合図で止める
      }
      startDriving(move);
    });
    ["pointerup", "pointercancel"].forEach(function (name) {
      button.addEventListener(name, function () { stopDriving(); });
    });
    button.addEventListener("lostpointercapture", stopIfDriving);
  }

  // --- 写真 ---

  function showPhotos(urls) {
    const list = byId("photo-list");
    const items = [];
    urls.forEach(function (url) {
      if (typeof url !== "string" || url.indexOf("/photos/") !== 0) {
        return;
      }
      const link = document.createElement("a");
      link.href = url;
      link.download = url.slice("/photos/".length);
      const img = document.createElement("img");
      img.src = url;
      img.alt = "";
      link.appendChild(img);
      items.push(link);
    });
    list.replaceChildren.apply(list, items);
    byId("photos").hidden = items.length === 0;
  }

  function takePhoto() {
    stopDriving();
    post("/api/photo", {}).then(function (r) {
      if (!r) {
        return;
      }
      if ("message" in r) {
        showMessage(typeof r.message === "string" ? r.message : "");
      }
      showPhotos(Array.isArray(r.photos) ? r.photos : []);
    });
  }

  // --- 話させる ---

  function say() {
    const field = byId("say-text");
    const text = field.value;
    field.value = "";
    post("/api/say", { text: text }).then(applyStatus);
  }

  // --- 状態 ---

  function poll() {
    if (hidden) {
      return;
    }
    fetch("/api/status", { credentials: "same-origin", cache: "no-store" })
      .then(handle)
      .then(applyStatus)
      .catch(function () {});
    retryVideos();
  }

  function onVisibility() {
    if (document.visibilityState === "hidden") {
      hidden = true;
      stopDriving();
      CAMERAS.forEach(detach);
    } else {
      hidden = false;
      CAMERAS.forEach(attach);
      poll();
    }
  }

  function start() {
    document.querySelectorAll("[data-move]").forEach(setupDrive);
    document.querySelectorAll("[data-way]").forEach(function (button) {
      button.addEventListener("click", function () {
        post("/api/look", { way: button.dataset.way }).then(applyStatus);
      });
    });
    document.querySelectorAll("[data-act]").forEach(function (button) {
      button.addEventListener("click", function () {
        post("/api/act", { name: button.dataset.act }).then(applyStatus);
      });
    });
    byId("speed").addEventListener("click", function () {
      post("/api/act", { name: fast ? "slow" : "fast" }).then(applyStatus);
    });
    byId("photo").addEventListener("click", takePhoto);
    byId("photos-close").addEventListener("click", function () { showPhotos([]); });
    byId("say-send").addEventListener("click", say);
    byId("say-text").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.isComposing) {
        e.preventDefault();
        say();
      }
    });
    byId("logout").addEventListener("click", function () {
      stopDriving();
      fetch("/logout", {
        method: "POST",
        credentials: "same-origin",
        keepalive: true,
        headers: { "Content-Type": "application/json" },
        body: "{}",
      })
        .catch(function () {})
        .then(function () { location.assign("/"); });
    });
    CAMERAS.forEach(function (cam) {
      byId("video-" + cam).addEventListener("error", function () { onVideoError(cam); });
    });
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", function () { stopDriving(); });
    document.addEventListener("pointerup", stopIfDriving);
    document.addEventListener("pointercancel", stopIfDriving);
    window.addEventListener("blur", stopIfDriving);
    CAMERAS.forEach(attach);
    poll();
    setInterval(poll, STATUS_EVERY_MS);
  }

  start();
})();
"""

APP_JS: Final = (
    _JS_TEMPLATE.replace("__TEXTS__", _JS_TEXTS)
    .replace("__DRIVE_EVERY_MS__", str(DRIVE_EVERY_MS))
    .replace("__STATUS_EVERY_MS__", str(STATUS_EVERY_MS))
    .replace("__STREAM_RETRY_MS__", str(STREAM_RETRY_MS))
)

APP_CSS: Final = """\
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { margin: 0; padding: 0; }
body {
  background: #fffaf0;
  color: #222;
  font-family: system-ui, sans-serif;
  font-size: 20px;
  padding: 12px 16px 32px;
  max-width: 560px;
  margin: 0 auto;
}
h1 { text-align: center; }
button {
  font: inherit;
  font-weight: bold;
  min-height: 64px;
  border: 0;
  border-radius: 16px;
  background: #ffd166;
  color: #222;
  touch-action: manipulation;
  user-select: none;
  -webkit-user-select: none;
  -webkit-touch-callout: none;
}
button[aria-pressed=true] { background: #06d6a0; }
input {
  font: inherit;
  min-height: 56px;
  width: 100%;
  padding: 8px 12px;
  border: 2px solid #999;
  border-radius: 12px;
}
.login form { display: grid; gap: 12px; }
.message {
  background: #ef476f;
  color: #fff;
  border-radius: 12px;
  padding: 12px;
  text-align: center;
}
.control .message { position: sticky; top: 0; z-index: 2; }
.videos { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.video { position: relative; margin: 0; background: #333; border-radius: 12px; overflow: hidden; }
.video img { display: block; width: 100%; aspect-ratio: 4 / 3; object-fit: cover; }
.video figcaption { color: #fff; font-size: 14px; text-align: center; padding: 2px; }
.overlay {
  position: absolute;
  inset: 0;
  margin: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 8px;
  background: rgba(0, 0, 0, 0.6);
  color: #fff;
  font-size: 16px;
}
section { margin-top: 16px; }
.drive-pad, .look-pad {
  user-select: none;
  -webkit-user-select: none;
  -webkit-touch-callout: none;
}
.drive-pad { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.drive { min-height: 80px; touch-action: none; }
.drive-pad .guide { grid-column: 1 / -1; margin: 0; font-size: 16px; text-align: center; }
.drive-pad .stop { grid-column: 1 / -1; background: #ef476f; color: #fff; }
.look-pad {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  grid-template-areas: ". up ." "left center right" ". down .";
  gap: 8px;
}
.look-up { grid-area: up; }
.look-left { grid-area: left; }
.look-center { grid-area: center; }
.look-right { grid-area: right; }
.look-down { grid-area: down; }
.acts { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.say { display: grid; gap: 8px; }
.logout { width: 100%; margin-top: 24px; background: #ccc; }
body[data-operator=other] .drive-pad,
body[data-operator=other] .look-pad,
body[data-operator=other] .acts,
body[data-operator=other] .say { opacity: 0.4; }
.photos {
  position: fixed;
  inset: 0;
  margin: 0;
  z-index: 3;
  background: rgba(0, 0, 0, 0.85);
  padding: 16px;
  overflow-y: auto;
  display: grid;
  gap: 12px;
  align-content: start;
}
.photos img { display: block; width: 100%; border-radius: 12px; }
"""
