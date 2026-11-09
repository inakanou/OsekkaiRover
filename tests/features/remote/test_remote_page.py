"""ログインと操作の画面の文字列を確かめる。

要件 2.7、4.1、4.3、6.4、8.2、8.3、9.2。CSP（インラインのスクリプト・見た目を使わない）も見る。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from osekkai.features.remote import page, texts

DRIVE_LABELS = ("まえ", "ひだりまえ", "みぎまえ", "うしろ", "ひだりうしろ", "みぎうしろ")
DRIVE_MOVES = ("fwd", "left", "right", "back", "back_left", "back_right")
LOOK_WAYS = ("up", "down", "left", "right", "center")


def _pages() -> tuple[str, ...]:
    return (page.login_html(None), page.login_html(texts.MSG_WRONG_PIN), page.control_html())


# --- ログインの画面 ---


def test_ログインの画面は_pin_の欄を持ち_login_に送る() -> None:
    html = page.login_html(None)
    assert '<form method="post" action="/login"' in html
    field = re.search(r"<input[^>]*name=\"pin\"[^>]*>", html)
    assert field is not None
    tag = field.group(0)
    assert 'type="password"' in tag
    assert 'inputmode="numeric"' in tag
    assert 'autocomplete="off"' in tag
    assert 'maxlength="8"' in tag


@pytest.mark.parametrize("message", [texts.MSG_WRONG_PIN, texts.MSG_LOCKED])
def test_ログインの画面に知らせを出す(message: str) -> None:
    assert message in page.login_html(message)


def test_知らせのないログインの画面に知らせの文はない() -> None:
    html = page.login_html(None)
    assert texts.MSG_WRONG_PIN not in html
    assert texts.MSG_LOCKED not in html


def test_ログインの画面の知らせはエスケープする() -> None:
    html = page.login_html("<b>x</b>")
    assert "<b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


# --- 操作の画面 ---


def test_操作の画面に走る_6_つのボタンと_とめる_がある() -> None:
    html = page.control_html()
    for label, move in zip(DRIVE_LABELS, DRIVE_MOVES, strict=True):
        assert re.search(rf'data-move="{move}"[^>]*>{label}<', html), move
    assert re.search(r'data-move="stop"[^>]*>とめる<', html)


def test_後退のボタンの近くに案内がある() -> None:
    html = page.control_html()
    assert texts.MSG_BACK_GUIDE in html
    guide = html.index(texts.MSG_BACK_GUIDE)
    back = html.index('data-move="back"')
    assert abs(guide - back) < 400


def test_首の_5_つのボタンがある() -> None:
    html = page.control_html()
    for way in LOOK_WAYS:
        assert f'data-way="{way}"' in html, way
    for label in ("うえ", "した", "ひだり", "みぎ", "まんなか"):
        assert f">{label}<" in html, label


def test_ブッブー_ダンス_速さ_写真_おわるのボタンがある() -> None:
    html = page.control_html()
    assert 'data-act="horn"' in html
    assert 'data-act="dance"' in html
    assert 'id="speed"' in html
    assert 'id="photo"' in html
    assert 'id="logout"' in html
    for label in ("ブッブー", "ダンス", "ゆっくり", "しゃしん", "おわる"):
        assert label in html, label


def test_話させる欄は_120_文字まで() -> None:
    html = page.control_html()
    field = re.search(r"<input[^>]*id=\"say-text\"[^>]*>", html)
    assert field is not None
    assert 'maxlength="120"' in field.group(0)
    assert 'id="say-send"' in html


def test_2_つの映像の枠と重ねる知らせがある() -> None:
    html = page.control_html()
    for cam in ("neck", "usb"):
        assert re.search(rf'<img[^>]*id="video-{cam}"', html), cam
        assert f'id="overlay-{cam}"' in html
    assert 'id="message"' in html
    assert 'id="photos"' in html


def test_操作の画面は_app_js_と_app_css_を読む() -> None:
    html = page.control_html()
    assert '<script src="/app.js" defer></script>' in html
    assert '<link rel="stylesheet" href="/app.css">' in html


# --- CSP に合う ---


@pytest.mark.parametrize("html", _pages())
def test_インラインのスクリプトがない(html: str) -> None:
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S)
    assert all(body == "" for body in scripts)
    assert all(tag == '<script src="/app.js" defer>' for tag in re.findall(r"<script[^>]*>", html))


@pytest.mark.parametrize("html", _pages())
def test_on_の属性と_style_がない(html: str) -> None:
    assert re.search(r"\son\w+\s*=", html, flags=re.I) is None
    assert re.search(r"\sstyle\s*=", html, flags=re.I) is None
    assert "<style" not in html.lower()


@pytest.mark.parametrize("html", _pages())
def test_外の資源を読まない(html: str) -> None:
    assert re.search(r"https?://", html) is None
    assert "//" not in re.sub(r"<!--.*?-->", "", html)


def test_js_と_css_は外の資源を読まない() -> None:
    assert re.search(r"https?://", page.APP_JS) is None
    assert re.search(r"https?://", page.APP_CSS) is None
    assert "@import" not in page.APP_CSS


# --- JavaScript ---


def test_押している間は_150_ms_ごとに送る() -> None:
    js = page.APP_JS
    assert "pointerdown" in js
    assert "setInterval(" in js
    assert "150" in js
    assert "setPointerCapture" in js


@pytest.mark.parametrize(
    "event", ["pointerup", "pointercancel", "lostpointercapture", "visibilitychange"]
)
def test_離したら止める合図を受ける(event: str) -> None:
    assert f'"{event}"' in page.APP_JS


def test_写真のボタンは止めてから撮る() -> None:
    js = page.APP_JS
    start = js.index("function takePhoto")
    body = js[start : js.index("\n  }\n", start)]
    assert "stopDriving(" in body
    assert body.index("stopDriving(") < body.index("/api/photo")


def test_写真の返事の知らせを出す() -> None:
    js = page.APP_JS
    start = js.index("function takePhoto")
    body = js[start : js.index("\n  }\n", start)]
    assert "showMessage(" in body  # 9.3: 撮れなかったことを出す


def test_状態は_1_秒ごとに読み_401_で読み直す() -> None:
    js = page.APP_JS
    assert "/api/status" in js
    assert "1000" in js
    assert "401" in js
    assert "location.reload()" in js


def test_送るときは_json_で同じ所だけ() -> None:
    js = page.APP_JS
    assert "fetch(" in js
    assert '"application/json"' in js
    assert '"same-origin"' in js
    for path in ("/api/drive", "/api/look", "/api/act", "/api/say", "/api/photo", "/logout"):
        assert f'"{path}"' in js, path


def test_話させた文字を端末に残さない() -> None:
    assert "localStorage" not in page.APP_JS
    assert "sessionStorage" not in page.APP_JS
    assert "document.cookie" not in page.APP_JS


def test_映像は隠れたら外し_戻ったら付け直す() -> None:
    js = page.APP_JS
    assert "/stream/" in js
    assert 'src = ""' in js
    assert "Date.now()" in js
    assert '"error"' in js


def test_映像の知らせは要件の文() -> None:
    js = page.APP_JS
    assert json.dumps(texts.MSG_STREAM_FULL) in js  # 8.2
    assert json.dumps(texts.MSG_CAMERA_UNAVAILABLE) in js  # 8.3


def test_撮った写真は保存のリンクで出す() -> None:
    js = page.APP_JS
    assert "download" in js
    assert "photos" in js


def test_状態の見出しの名前を決めている() -> None:
    assert page.STATUS_KEYS == ("reply", "message", "operator", "fast", "cameras", "dancing")
    js = page.APP_JS
    for key in ("message", "operator", "fast", "cameras", "dancing"):
        assert f".{key}" in js, key


def test_js_は_textContent_で出し_innerHTML_を使わない() -> None:
    assert "innerHTML" not in page.APP_JS
    assert "textContent" in page.APP_JS


def test_css_は大きな押しボタン() -> None:
    css = page.APP_CSS
    assert "button" in css
    assert "min-height" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="node がない")
def test_js_の文法が正しい(tmp_path: Path) -> None:
    script = tmp_path / "app.js"
    script.write_text(page.APP_JS, encoding="utf-8")
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(
        [node, "--check", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


# --- 走る命令の順（遅れた命令が STOP を越えない） ---


def test_走る命令は_1_つだけ送り中にする() -> None:
    js = page.APP_JS
    assert "moveInFlight" in js
    assert "keepalive: true" in js or "keepalive: keepalive" in js


def test_走る命令は_seq_と_page_を持つ() -> None:
    assert page.DRIVE_KEYS == ("move", "seq", "page")
    js = page.APP_JS
    assert "seq: seq" in js
    assert "page: PAGE_ID" in js
    assert "getRandomValues" in js


def test_ボタンは_touch_action_を決める() -> None:
    css = page.APP_CSS
    assert "touch-action: manipulation" in css
    assert "touch-action: none" in css


def test_走るボタンは長押しでブラウザの操作を出さない() -> None:
    js = page.APP_JS
    assert 'button.addEventListener("touchstart"' in js
    assert "passive: false" in js
    css = page.APP_CSS
    assert ".drive-pad, .look-pad {" in css


def test_ページのどこで離しても止める() -> None:
    js = page.APP_JS
    assert 'document.addEventListener("pointerup"' in js
    assert 'document.addEventListener("pointercancel"' in js
    assert 'window.addEventListener("blur"' in js


# node で偽の DOM を作り、APP_JS を動かす。失敗すると assert が投げて終わる
_HARNESS = r"""
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function makeEl(id) {
  return {
    id: id, dataset: {}, hidden: false, textContent: "", src: "", value: "", attrs: {},
    handlers: {},
    addEventListener(name, fn) { (this.handlers[name] = this.handlers[name] || []).push(fn); },
    fire(name, ev) { (this.handlers[name] || []).forEach((fn) => fn(ev || fakeEvent())); },
    setAttribute(k, v) { this.attrs[k] = v; },
    setPointerCapture() {},
    appendChild() {},
    replaceChildren() {},
  };
}
function fakeEvent() { return { pointerId: 1, preventDefault() {} }; }

const els = {};
const ids = ["message", "overlay-neck", "overlay-usb", "video-neck", "video-usb", "speed",
  "photo", "photos", "photo-list", "photos-close", "say-text", "say-send", "logout"];
ids.forEach((id) => { els[id] = makeEl(id); });
const moves = ["fwd", "left", "right", "back", "back_left", "back_right", "stop"];
const moveEls = moves.map((m) => { const e = makeEl("m-" + m); e.dataset.move = m; return e; });
const actEls = ["horn", "dance"].map((a) => {
  const e = makeEl("a-" + a);
  e.dataset.act = a;
  return e;
});
const doc = makeEl("document");
doc.visibilityState = "visible";
doc.body = makeEl("body");
doc.getElementById = (id) => els[id];
doc.querySelectorAll = (sel) =>
  sel === "[data-move]" ? moveEls : sel === "[data-act]" ? actEls : [];
doc.querySelector = (sel) => actEls[1];
doc.createElement = (tag) => makeEl(tag);
const win = makeEl("window");

let now = 0;
let timers = [];
let nextId = 1;
function setIntervalFake(fn, ms) {
  const t = { id: nextId++, fn, ms, at: now + ms };
  timers.push(t);
  return t.id;
}
function clearIntervalFake(id) { timers = timers.filter((t) => t.id !== id); }
function advance(ms) {
  const end = now + ms;
  for (;;) {
    const due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    now = due.at;
    due.at += due.ms;
    due.fn();
  }
  now = end;
}

const requests = [];
let maxMovesInFlight = 0;
function movesInFlight() {
  return requests.filter((r) => r.url === "/api/drive" && r.body.move !== "stop" && !r.done).length;
}
function fetchFake(url, opts) {
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  const req = { url, opts: opts || {}, body, done: false };
  requests.push(req);
  maxMovesInFlight = Math.max(maxMovesInFlight, movesInFlight());
  return new Promise((resolve) => {
    req.resolve = () => {
      req.done = true;
      resolve({ status: 200, ok: true, json: () => Promise.resolve({}) });
    };
  });
}
const flush = () => new Promise((r) => setImmediate(r));
async function settleDrives() {
  requests.filter((r) => r.url === "/api/drive" && !r.done).forEach((r) => r.resolve());
  for (let i = 0; i < 5; i++) await flush();
}
const drives = () => requests.filter((r) => r.url === "/api/drive");

const sandbox = {
  document: doc, window: win, location: { reload() {}, assign() {} }, fetch: fetchFake,
  setInterval: setIntervalFake, clearInterval: clearIntervalFake, Date: { now: () => now },
  crypto: {
    getRandomValues: (a) => {
      for (let i = 0; i < a.length; i++) a[i] = i * 37 + 5;
      return a;
    },
  },
  Uint8Array, Array, JSON, Promise, Math, String, Number, Object,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], "utf-8"), sandbox);

(async () => {
  const fwd = moveEls[0];
  fwd.fire("pointerdown");
  assert.equal(drives().length, 1);
  advance(150 * 3);
  assert.equal(drives().length, 1, "送り中は次を送らない");
  await settleDrives();
  advance(150);
  assert.equal(drives().length, 2);
  await settleDrives();
  advance(150);
  assert.equal(drives().length, 3);
  assert.ok(drives().every((r) => r.body.move === "fwd"));
  // 3 つめの命令が送り中のまま離す
  fwd.fire("pointerup");
  const stopAt = drives().length - 1;
  assert.equal(drives()[stopAt].body.move, "stop", "離したらすぐ STOP");
  assert.equal(drives()[stopAt].opts.keepalive, true, "STOP は keepalive");
  await settleDrives();
  advance(2000);
  await settleDrives();
  const after = drives().slice(stopAt);
  assert.ok(after.every((r) => r.body.move === "stop"), "STOP の後に走る命令を送らない");
  assert.equal(drives()[drives().length - 1].body.move, "stop");
  assert.ok(after.length >= 2, "送り中だった命令が終わったら STOP をもう一度送る");

  // 画面が隠れたら keepalive の STOP
  fwd.fire("pointerdown");
  await settleDrives();
  doc.visibilityState = "hidden";
  doc.fire("visibilitychange");
  const last = drives()[drives().length - 1];
  assert.equal(last.body.move, "stop");
  assert.equal(last.opts.keepalive, true);
  await settleDrives();
  doc.visibilityState = "visible";
  doc.fire("visibilitychange");

  // ページのどこで離しても止める
  fwd.fire("pointerdown");
  doc.fire("pointerup");
  assert.equal(drives()[drives().length - 1].body.move, "stop");
  await settleDrives();
  fwd.fire("pointerdown");
  win.fire("blur");
  assert.equal(drives()[drives().length - 1].body.move, "stop");
  await settleDrives();

  assert.ok(maxMovesInFlight <= 1, "走る命令の送り中は 1 つまで");
  const seqs = drives().map((r) => r.body.seq);
  seqs.forEach((s, i) => { if (i > 0) assert.ok(s > seqs[i - 1], "seq は増える"); });
  assert.ok(seqs.every((s) => Number.isInteger(s)));
  const pages = new Set(drives().map((r) => r.body.page));
  assert.equal(pages.size, 1);
  const pageId = [...pages][0];
  assert.match(pageId, /^[0-9a-f]{8,32}$/);
  drives().forEach((r) => {
    assert.equal(r.opts.headers["Content-Type"], "application/json");
    assert.equal(r.opts.credentials, "same-origin");
  });
  console.log("OK");
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exit(1); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node がない")
def test_押して離すと_STOP_が最後で_seq_は増える(tmp_path: Path) -> None:
    script = tmp_path / "app.js"
    script.write_text(page.APP_JS, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(
        [node, str(harness), str(script)], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
