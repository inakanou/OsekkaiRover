"""気象庁の注意報・警報（r8）のアダプタ（5.7、5.11、10.1、10.2）。

`https://www.jma.go.jp/bosai/warning/data/r8/{office}.json` を 1 回の GET で取る。応答は電文の
種類（`dataTypeCode`）ごとの報の配列で、全部の報の `warning.class20Items` から市区町村のコードの
`kinds` を集める。「解除」「発表警報・注意報はなし」とコードのない種類は除き、24 時間より古い報は
使わない（止まった URL が 200 を返し続けた前例のため）。お話で触れないコード（乾燥・濃霧など）は
捨てる。

失敗は例外でなく `AlertsUnavailable`（種類だけ）で返す。ログには決まった文言と種類だけを出し、
地域のコードや応答の中身を出さない。公式の API ではないので、形が違えば取れなかった扱いにする。
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final, Protocol

from ..model import AlertKind, AlertsProblem, AlertsResult, AlertsUnavailable

logger = logging.getLogger(__name__)

URL_TEMPLATE: Final = "https://www.jma.go.jp/bosai/warning/data/r8/{office}.json"
USER_AGENT: Final = "OsekkaiRover"
# r8 の応答はおおむね 30 KB。大きすぎる応答は読み切らずに形の違いとする
MAX_BODY_BYTES: Final = 1024 * 1024
# これより古い報は使わない
MAX_REPORT_AGE: Final = timedelta(hours=24)

# 出ていないことを表す状態。ほかの状態（発表・継続・警報から注意報など）は出ている
_INACTIVE_STATUSES: Final = frozenset({"解除", "発表警報・注意報はなし"})

# 気象庁の 2026-05-29 からのコードの体系。表にないコードは話さない
_CODE_KINDS: Final[Mapping[str, AlertKind]] = MappingProxyType(
    {
        # 大雨と土砂災害
        **dict.fromkeys(("10", "03", "43", "33", "29", "09", "49", "39"), AlertKind.HEAVY_RAIN),
        # 大雪
        **dict.fromkeys(("12", "06", "36"), AlertKind.HEAVY_SNOW),
        # 強風・暴風・風雪・暴風雪
        **dict.fromkeys(("15", "05", "35", "13", "02", "32"), AlertKind.STRONG_WIND),
        # 雷
        "14": AlertKind.THUNDER,
        # 波浪と高潮
        **dict.fromkeys(("16", "07", "37", "19", "08", "48", "38"), AlertKind.ROUGH_SEA),
    }
)


class _Response(Protocol):
    def read(self, amt: int, /) -> bytes: ...


class Opener(Protocol):
    """`urllib.request.urlopen` と同じ呼び方の口（テストで差し替える）。"""

    def __call__(
        self, url: urllib.request.Request, /, *, timeout: float
    ) -> AbstractContextManager[_Response]: ...


class _BadShape(Exception):
    """応答の形が違う（中のメッセージは持たない）。"""


def _default_opener(
    url: urllib.request.Request, /, *, timeout: float
) -> AbstractContextManager[_Response]:
    response: AbstractContextManager[_Response] = urllib.request.urlopen(url, timeout=timeout)
    return response


def kind_for_code(code: str) -> AlertKind | None:
    """気象庁の注意報・警報のコードを、お話で触れる種類にする。触れないコードは None。"""
    return _CODE_KINDS.get(code)


class JmaAlerts:
    """市区町村の今の注意報・警報の種類を取る（`AlertPort`）。例外を外へ出さない。"""

    def __init__(
        self,
        office: str,
        city: str,
        now: Callable[[], datetime],
        opener: Opener = _default_opener,
    ) -> None:
        self._url = URL_TEMPLATE.format(office=office)
        self._city = city
        self._now = now  # タイムゾーン付きの今の時刻（基盤の時計）
        self._opener = opener

    def active(self, timeout_s: float) -> AlertsResult:
        body = self._fetch(timeout_s)
        if isinstance(body, AlertsUnavailable):
            return body
        try:
            reports = json.loads(body)
            return _collect(reports, self._city, self._now())
        except (ValueError, RecursionError, _BadShape):
            return _unavailable(AlertsProblem.BAD_RESPONSE)

    def _fetch(self, timeout_s: float) -> bytes | AlertsUnavailable:
        request = urllib.request.Request(
            self._url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with self._opener(request, timeout=timeout_s) as response:
                body = response.read(MAX_BODY_BYTES + 1)
        except TimeoutError:
            return _unavailable(AlertsProblem.TIMEOUT)
        except urllib.error.URLError as exc:
            # HTTPError も URLError の仲間。つながる途中の時間切れは reason に入る
            if isinstance(exc.reason, TimeoutError):
                return _unavailable(AlertsProblem.TIMEOUT)
            return _unavailable(AlertsProblem.CONNECTION)
        except (OSError, http.client.HTTPException, ValueError):
            return _unavailable(AlertsProblem.CONNECTION)
        if len(body) > MAX_BODY_BYTES:
            return _unavailable(AlertsProblem.BAD_RESPONSE)
        return body


def _unavailable(kind: AlertsProblem) -> AlertsUnavailable:
    logger.warning("注意報と警報を取れませんでした: %s", kind.value)
    return AlertsUnavailable(kind)


def _collect(reports: object, city: str, now: datetime) -> AlertsResult:
    """全部の報から、市区町村の出ている種類を集める。形が違えば `_BadShape`。"""
    if not isinstance(reports, list) or not reports:
        raise _BadShape
    found: set[AlertKind] = set()
    fresh = False
    city_seen = False
    for report in reports:
        if not isinstance(report, dict):
            raise _BadShape
        if now - _report_time(report) > MAX_REPORT_AGE:
            continue
        fresh = True
        for item in _class20_items(report):
            if item.get("areaCode") != city:
                continue
            city_seen = True
            found.update(_active_kinds(item))
    if not fresh:
        return _unavailable(AlertsProblem.STALE)
    if not city_seen:
        # 新しい報のどれにも市区町村がない（コードの誤りか形の変わり）。「出ていない」と言わない
        raise _BadShape
    return frozenset(found)


def _report_time(report: dict[str, object]) -> datetime:
    value = report.get("reportDatetime")
    if not isinstance(value, str):
        raise _BadShape
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        raise _BadShape from None
    if when.tzinfo is None:
        raise _BadShape
    return when


def _class20_items(report: dict[str, object]) -> list[dict[str, object]]:
    warning = report.get("warning")
    if not isinstance(warning, dict):
        raise _BadShape
    items = warning.get("class20Items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise _BadShape
    return items


def _active_kinds(item: dict[str, object]) -> set[AlertKind]:
    kinds = item.get("kinds")
    if not isinstance(kinds, list):
        raise _BadShape
    found: set[AlertKind] = set()
    for entry in kinds:
        if not isinstance(entry, dict):
            raise _BadShape
        status = entry.get("status")
        code = entry.get("code")
        if not isinstance(status, str) or not (code is None or isinstance(code, str)):
            raise _BadShape
        if code is None or status in _INACTIVE_STATUSES:
            continue
        kind = kind_for_code(code)
        if kind is not None:
            found.add(kind)
    return found
