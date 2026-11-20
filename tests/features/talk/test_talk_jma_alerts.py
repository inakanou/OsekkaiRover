"""気象庁の注意報・警報のアダプタ（JmaAlerts）を、見本の応答で確かめる（5.7、5.11、10.1、10.2）。

外へは問い合わせない。`opener` を差し替え、受け取った問い合わせを覚える。
`jma_r8_tokyo.json` は 2026-11-20 に取った東京（130000）の r8 の応答を、市区町村を減らして
置いたもの（気象庁の公開のデータ）。ほかの見本は形をまねて作ったもの。
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from osekkai.features.talk.adapters import jma_alerts
from osekkai.features.talk.adapters.jma_alerts import MAX_BODY_BYTES, JmaAlerts, kind_for_code
from osekkai.features.talk.model import AlertKind, AlertPort, AlertsProblem, AlertsUnavailable

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JST = timezone(timedelta(hours=9))
# 東京の見本を取った後の時刻。11-05 と 11-18 の報は 24 時間より古い
NOW = datetime(2026, 11, 20, 13, 0, tzinfo=JST)
OFFICE = "130000"
CITY = "1311300"
TIMEOUT_S = 20.0

ALL_KINDS = frozenset(AlertKind)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class _Response:
    def __init__(self, body: bytes, reads: list[int]) -> None:
        self._body = body
        self._reads = reads

    def read(self, amt: int, /) -> bytes:
        self._reads.append(amt)
        return self._body[:amt]


class FakeOpener:
    """決めた応答を返すか、決めた例外を出す偽の urlopen。受け取った問い合わせを覚える。"""

    def __init__(
        self,
        body: bytes = b"[]",
        *,
        error: BaseException | None = None,
        read_error: BaseException | None = None,
    ) -> None:
        self.body = body
        self.error = error
        self.read_error = read_error
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []
        self.reads: list[int] = []

    @contextmanager
    def __call__(self, url: urllib.request.Request, /, *, timeout: float) -> Iterator[_Response]:
        self.requests.append(url)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        if self.read_error is not None:
            raise self.read_error
        yield _Response(self.body, self.reads)


def _alerts(opener: FakeOpener, *, city: str = CITY, now: datetime = NOW) -> JmaAlerts:
    return JmaAlerts(OFFICE, city, lambda: now, opener=opener)


def _active(body: bytes, *, city: str = CITY, now: datetime = NOW) -> object:
    return _alerts(FakeOpener(body), city=city, now=now).active(TIMEOUT_S)


def _report(
    kinds: list[dict[str, object]], *, at: datetime = NOW - timedelta(hours=1), city: str = CITY
) -> dict[str, object]:
    return {
        "reportDatetime": at.isoformat(),
        "warning": {
            "class10Items": [],
            "class20Items": [
                {"areaCode": "1310100", "kinds": [{"status": "発表警報・注意報はなし"}]},
                {"areaCode": city, "kinds": kinds},
            ],
        },
        "dataTypeCode": "VPWW58",
    }


def _body(*reports: object) -> bytes:
    return json.dumps(list(reports), ensure_ascii=False).encode("utf-8")


# --- ポートの形と問い合わせ ---


def test_注意報のポートを満たす() -> None:
    port: AlertPort = _alerts(FakeOpener())
    assert callable(port.active)


def test_府県のコードの_r8_の_URL_に時間切れと名前を付けて問い合わせる() -> None:
    opener = FakeOpener(_fixture("jma_r8_tokyo.json"))
    _alerts(opener).active(12.5)
    [request] = opener.requests
    assert request.full_url == "https://www.jma.go.jp/bosai/warning/data/r8/130000.json"
    assert request.get_method() == "GET"
    assert request.get_header("User-agent") == "OsekkaiRover"
    assert opener.timeouts == [12.5]


def test_応答は上限の少し先までしか読まない() -> None:
    opener = FakeOpener(_fixture("jma_r8_tokyo.json"))
    _alerts(opener).active(TIMEOUT_S)
    assert opener.reads == [MAX_BODY_BYTES + 1]


def test_既定では標準ライブラリの_urlopen_で問い合わせる(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, float]] = []

    def fake_urlopen(url: urllib.request.Request, *, timeout: float) -> object:
        calls.append((url.full_url, timeout))
        raise urllib.error.URLError("つながらない")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = JmaAlerts(OFFICE, CITY, lambda: NOW).active(7.0)
    assert result == AlertsUnavailable(AlertsProblem.CONNECTION)
    assert calls == [("https://www.jma.go.jp/bosai/warning/data/r8/130000.json", 7.0)]


# --- 本物の見本 ---


@pytest.mark.parametrize(
    ("city", "expected"),
    [
        # 強風（継続）と波浪（継続）
        ("1310200", {AlertKind.STRONG_WIND, AlertKind.ROUGH_SEA}),
        ("1310100", {AlertKind.STRONG_WIND}),
        # どの報でも「なし」
        ("1311300", set()),
        # 新しい報の雷は「解除」なので除く
        ("1338100", {AlertKind.STRONG_WIND, AlertKind.ROUGH_SEA}),
        # 古い報の大雨の「解除」は見ない
        ("1340100", {AlertKind.STRONG_WIND, AlertKind.ROUGH_SEA}),
        # 古い報の土砂災害の「解除」は見ない
        ("1336100", {AlertKind.STRONG_WIND, AlertKind.ROUGH_SEA}),
        # 波浪の「発表」
        ("1342100", {AlertKind.ROUGH_SEA}),
    ],
)
def test_本物の見本から市区町村の注意報を集める(city: str, expected: set[AlertKind]) -> None:
    assert _active(_fixture("jma_r8_tokyo.json"), city=city) == frozenset(expected)


def test_すべての報から種類を集め_話さないコードは捨てる() -> None:
    # 大雨（発表）・大雪（継続）・暴風（警報から注意報）・雷・高潮（特別警報から警報）。
    # 乾燥と濃霧は捨てる
    assert _active(_fixture("jma_r8_all_kinds.json")) == ALL_KINDS


def test_ほかの市区町村の注意報は集めない() -> None:
    assert _active(_fixture("jma_r8_all_kinds.json"), city="1310100") == frozenset()


# --- コードの対応 ---


@pytest.mark.parametrize(
    ("codes", "kind"),
    [
        (("10", "03", "43", "33", "29", "09", "49", "39"), AlertKind.HEAVY_RAIN),
        (("12", "06", "36"), AlertKind.HEAVY_SNOW),
        (("15", "05", "35", "13", "02", "32"), AlertKind.STRONG_WIND),
        (("14",), AlertKind.THUNDER),
        (("16", "07", "37", "19", "08", "48", "38"), AlertKind.ROUGH_SEA),
    ],
)
def test_コードをお話の種類に対応させる(codes: tuple[str, ...], kind: AlertKind) -> None:
    for code in codes:
        assert kind_for_code(code) is kind, code
        assert _active(_body(_report([{"code": code, "status": "発表"}]))) == {kind}, code


@pytest.mark.parametrize("code", ["20", "21", "22", "23", "24", "25", "26", "27", "99", "1", ""])
def test_対応しないコードは話さないものとして捨てる(code: str) -> None:
    assert kind_for_code(code) is None
    assert _active(_body(_report([{"code": code, "status": "発表"}]))) == frozenset()


# --- 状態 ---


@pytest.mark.parametrize(
    "status", ["発表", "継続", "警報から注意報", "特別警報から警報", "特別警報から注意報"]
)
def test_出ている状態は集める(status: str) -> None:
    assert _active(_body(_report([{"code": "14", "status": status}]))) == {AlertKind.THUNDER}


def test_解除は除く() -> None:
    assert _active(_body(_report([{"code": "14", "status": "解除"}]))) == frozenset()


def test_なしとコードのない種類は除く() -> None:
    body = _body(_report([{"status": "発表警報・注意報はなし"}, {"status": "発表"}]))
    assert _active(body) == frozenset()


def test_ある報で解除でも別の報で出ていれば集める() -> None:
    body = _body(
        _report([{"code": "10", "status": "解除"}]),
        _report([{"code": "10", "status": "継続"}]),
    )
    assert _active(body) == {AlertKind.HEAVY_RAIN}


# --- 古い報 ---


def test_24_時間より古い報は使わない() -> None:
    body = _body(
        _report([{"code": "10", "status": "発表"}], at=NOW - timedelta(hours=24, minutes=1)),
        _report([{"status": "発表警報・注意報はなし"}]),
    )
    assert _active(body) == frozenset()


def test_ちょうど_24_時間前の報は使う() -> None:
    body = _body(_report([{"code": "10", "status": "発表"}], at=NOW - timedelta(hours=24)))
    assert _active(body) == {AlertKind.HEAVY_RAIN}


def test_報の時刻はタイムゾーンを考えて比べる() -> None:
    # UTC で書いた 1 時間前の報
    at = (NOW - timedelta(hours=1)).astimezone(UTC)
    assert _active(_body(_report([{"code": "14", "status": "発表"}], at=at))) == {AlertKind.THUNDER}


def test_全部の報が古ければ取れなかった結果を返す() -> None:
    assert _active(_fixture("jma_r8_stale.json")) == AlertsUnavailable(AlertsProblem.STALE)


def test_本物の見本も時刻が進めば古い扱いになる() -> None:
    later = datetime(2026, 11, 22, 12, 30, tzinfo=JST)
    result = _active(_fixture("jma_r8_tokyo.json"), city="1310200", now=later)
    assert result == AlertsUnavailable(AlertsProblem.STALE)


# --- 失敗 ---


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (TimeoutError("timed out"), AlertsProblem.TIMEOUT),
        (urllib.error.URLError(TimeoutError("timed out")), AlertsProblem.TIMEOUT),
        (urllib.error.URLError("name resolution"), AlertsProblem.CONNECTION),
        (
            urllib.error.HTTPError(
                "https://www.jma.go.jp/", 500, "Server Error", http.client.HTTPMessage(), None
            ),
            AlertsProblem.CONNECTION,
        ),
        (
            urllib.error.HTTPError(
                "https://www.jma.go.jp/", 404, "Not Found", http.client.HTTPMessage(), None
            ),
            AlertsProblem.CONNECTION,
        ),
        (ConnectionResetError("reset"), AlertsProblem.CONNECTION),
        (OSError("network unreachable"), AlertsProblem.CONNECTION),
        (http.client.RemoteDisconnected("closed"), AlertsProblem.CONNECTION),
    ],
)
def test_問い合わせの失敗は例外でなく種類で返す(error: BaseException, kind: AlertsProblem) -> None:
    result = _alerts(FakeOpener(error=error)).active(TIMEOUT_S)
    assert result == AlertsUnavailable(kind)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (TimeoutError("timed out"), AlertsProblem.TIMEOUT),
        (http.client.IncompleteRead(b"[", 100), AlertsProblem.CONNECTION),
        (ConnectionResetError("reset"), AlertsProblem.CONNECTION),
    ],
)
def test_読む途中の失敗も種類で返す(error: BaseException, kind: AlertsProblem) -> None:
    result = _alerts(FakeOpener(read_error=error)).active(TIMEOUT_S)
    assert result == AlertsUnavailable(kind)


def _naive_report() -> dict[str, object]:
    report = _report([{"code": "10", "status": "発表"}])
    report["reportDatetime"] = "2026-11-20T12:00:00"
    return report


def _without(key: str) -> dict[str, object]:
    report = _report([{"code": "10", "status": "発表"}])
    del report[key]
    return report


def _with_warning(warning: object) -> dict[str, object]:
    report = _report([{"code": "10", "status": "発表"}])
    report["warning"] = warning
    return report


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"", id="空"),
        pytest.param(b"<html>maintenance</html>", id="JSON でない"),
        pytest.param(b"\xff\xfe[]", id="UTF-8 でない"),
        pytest.param(b"[" * 200_000, id="深すぎる入れ子"),
        pytest.param(_fixture("jma_r8_bad_shape.json"), id="旧い形"),
        pytest.param(b"[]", id="報がない"),
        pytest.param(b"null", id="null"),
        pytest.param(_body("報"), id="報が文字"),
        pytest.param(_body(_without("reportDatetime")), id="時刻がない"),
        pytest.param(_body({**_report([]), "reportDatetime": 20261120}), id="時刻が数"),
        pytest.param(_body({**_report([]), "reportDatetime": "きのう"}), id="時刻が読めない"),
        pytest.param(_body(_naive_report()), id="時刻にタイムゾーンがない"),
        pytest.param(_body(_without("warning")), id="warning がない"),
        pytest.param(_body(_with_warning([])), id="warning が配列"),
        pytest.param(_body(_with_warning({"class10Items": []})), id="class20Items がない"),
        pytest.param(_body(_with_warning({"class20Items": {}})), id="class20Items が辞書"),
        pytest.param(_body(_with_warning({"class20Items": ["x"]})), id="市区町村が文字"),
        pytest.param(
            _body(_with_warning({"class20Items": [{"areaCode": CITY}]})), id="kinds がない"
        ),
        pytest.param(
            _body(_with_warning({"class20Items": [{"areaCode": CITY, "kinds": ["10"]}]})),
            id="種類が文字",
        ),
        pytest.param(_body(_report([{"code": "10"}])), id="状態がない"),
        pytest.param(_body(_report([{"code": 10, "status": "発表"}])), id="コードが数"),
        pytest.param(
            _body(_report([{"code": "10", "status": "発表"}], city="1310200")),
            id="市区町村がない",
        ),
    ],
)
def test_形が違えば取れなかった結果を返す(body: bytes) -> None:
    assert _active(body) == AlertsUnavailable(AlertsProblem.BAD_RESPONSE)


def test_古い報の形の違いでも取れなかった結果を返す() -> None:
    stale = _report([{"code": 10, "status": "発表"}], at=NOW - timedelta(days=3))
    body = _body(stale, _report([{"status": "発表警報・注意報はなし"}]))
    # 古い報は使わないので、中の形は見ない（新しい報で決まる）
    assert _active(body) == frozenset()


def test_大きすぎる応答は読み切らずに取れなかった結果を返す() -> None:
    body = b"[" + b" " * MAX_BODY_BYTES + b"]"
    opener = FakeOpener(body)
    assert _alerts(opener).active(TIMEOUT_S) == AlertsUnavailable(AlertsProblem.BAD_RESPONSE)
    assert opener.reads == [MAX_BODY_BYTES + 1]


def test_上限ちょうどの応答は読む() -> None:
    report = _body(_report([{"code": "14", "status": "発表"}]))
    body = report[:-1] + b" " * (MAX_BODY_BYTES - len(report)) + b"]"
    assert len(body) == MAX_BODY_BYTES
    assert _active(body) == {AlertKind.THUNDER}


# --- ログ ---


def test_ログに地域のコードと応答の中身を出さない(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=jma_alerts.__name__)
    tokyo = _fixture("jma_r8_tokyo.json")
    _active(tokyo, city="1310200")
    _active(_fixture("jma_r8_stale.json"))
    _active(b"<html>HIDDEN_PAGE</html>")
    _alerts(FakeOpener(error=urllib.error.URLError("HIDDEN_REASON"))).active(TIMEOUT_S)
    text = caplog.text
    for hidden in ("1310200", CITY, OFFICE, "HIDDEN_PAGE", "HIDDEN_REASON", "強風", "継続", "r8"):
        assert hidden not in text, hidden
    assert "stale" in text
    assert "bad_response" in text
    assert "connection" in text


def test_取れたときはログを出さない(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=jma_alerts.__name__)
    _active(_fixture("jma_r8_tokyo.json"), city="1310200")
    assert caplog.records == []
