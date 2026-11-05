"""self-check の契約と実行（device-deployment 6.2〜6.4、6.6、6.7）。

確認の項目（`Check`）を 1 つずつ別のスレッドで動かし、結果を並べる。

- 決めた時間で終わらない項目は「だめ（時間切れ）」にして次へ進む。スレッドは置いていく
- 例外の項目は「だめ」にし、理由は例外の型の名前だけにする（中身は出さない）
- ハードの項目は、ハードを触ってよくなければ動かさない。サービスの報告があれば
  それで判定し（使えないデバイスに名前があればだめ）、なければ飛ばす

理由はすべて固定の文言にする。設定の値・名前・話す文章・秘密を入れない。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from osekkai.core.config import ConfigReport
    from osekkai.core.feature import FeatureSpec
    from osekkai.core.paths import Paths
    from osekkai.core.status import ServiceSnapshot

logger = logging.getLogger(__name__)

SELFCHECK_SCHEMA: Final = 1
SELFCHECK_THREAD_NAME: Final = "osekkai-selfcheck"
DEFAULT_TIMEOUT_S: Final = 10.0
BASE_PREFIX: Final = "core"
# 項目の名前は「持ち主.項目」（"core.lcd"、"<機能の名前>.<項目>"）
CHECK_NAME_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")

# 固定の理由の文言
REASON_TIMEOUT: Final = "時間切れ"
REASON_SERVICE: Final = "サービスの報告"
REASON_SKIPPED_BUSY: Final = "ほかが動いている"
REASON_BAD_RESULT: Final = "結果の形が違う"

_LABELS: Final = {"ok": "よい", "failed": "だめ", "skipped": "飛ばした"}


class CheckStatus(Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class CheckResult:
    """項目の結果。"""

    status: CheckStatus
    reason: str = ""  # 固定の文言（値・名前・文章を含めない）


@dataclass(frozen=True)
class CheckContext:
    """項目に渡す文脈。"""

    paths: Paths
    report: ConfigReport  # 検証した設定
    service: ServiceSnapshot | None  # 動いているサービスの報告（なければ None）
    hardware_allowed: bool  # ロックを取れて、--no-hardware でない


@dataclass(frozen=True)
class Check:
    """確認の項目。"""

    name: str  # "core.lcd"、"<機能の名前>.<項目>"
    run: Callable[[CheckContext], CheckResult]
    timeout_s: float = DEFAULT_TIMEOUT_S
    hardware: bool = False  # True なら hardware_allowed でないと動かさない
    # サービスの報告で判定するときのデバイスの名前（ServiceSnapshot.unavailable と同じ名前）。
    # None なら報告では判定できないので、ハードを触ってよくなければ飛ばす
    device: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or CHECK_NAME_PATTERN.fullmatch(self.name) is None:
            raise ValueError("項目の名前は「持ち主.項目」の形にしてください")
        if not self.timeout_s > 0:
            raise ValueError("項目の時間は正の数にしてください")

    @property
    def owner(self) -> str:
        """名前の前の部分（"core" か機能の名前）。"""
        return self.name.split(".", 1)[0]


Results = list[tuple[str, CheckResult]]


class SelfCheckRunner:
    """項目を順に動かして、結果を並べる。"""

    def run(self, checks: Sequence[Check], ctx: CheckContext) -> Results:
        return [(check.name, self._run_one(check, ctx)) for check in checks]

    def _run_one(self, check: Check, ctx: CheckContext) -> CheckResult:
        if check.hardware and not ctx.hardware_allowed:
            return _judge_by_service(check, ctx.service)
        box: list[CheckResult] = []

        def target() -> None:
            try:
                # 型の注釈に反する結果も確かめる
                result: object = check.run(ctx)
            except Exception as exc:  # 項目の誤りで全体を止めない
                # 中身は出さない（パスや値が入ることがある）
                logger.warning("確認の項目が失敗しました: %s %s", check.name, type(exc).__name__)
                box.append(CheckResult(CheckStatus.FAILED, type(exc).__name__))
                return
            if not isinstance(result, CheckResult) or not isinstance(result.status, CheckStatus):
                box.append(CheckResult(CheckStatus.FAILED, REASON_BAD_RESULT))
                return
            box.append(result)

        thread = threading.Thread(target=target, name=SELFCHECK_THREAD_NAME, daemon=True)
        thread.start()
        thread.join(check.timeout_s)
        if thread.is_alive() or not box:
            logger.warning("確認の項目が時間内に終わりません: %s", check.name)
            return CheckResult(CheckStatus.FAILED, REASON_TIMEOUT)
        return box[0]


def _judge_by_service(check: Check, service: ServiceSnapshot | None) -> CheckResult:
    """ハードを触れないときの判定（6.7）。"""
    if service is None or check.device is None:
        return CheckResult(CheckStatus.SKIPPED, REASON_SKIPPED_BUSY)
    if check.device in service.unavailable:
        return CheckResult(CheckStatus.FAILED, REASON_SERVICE)
    return CheckResult(CheckStatus.OK, REASON_SERVICE)


def collect_checks(base: Iterable[Check], specs: Iterable[FeatureSpec]) -> list[Check]:
    """基盤の項目の後に、機能の項目を申告の順に並べる（6.2）。名前の誤りと重なりは止める。"""
    checks: list[Check] = []
    for check in base:
        if check.owner != BASE_PREFIX:
            raise ValueError("基盤の項目の名前は core. で始めてください")
        checks.append(check)
    for spec in specs:
        for check in spec.checks:
            if check.owner != spec.name:
                raise ValueError("機能の項目の名前は機能の名前で始めてください")
            checks.append(check)
    names = [check.name for check in checks]
    if len(set(names)) != len(names):
        raise ValueError("同じ名前の項目があります")
    return checks


def render_lines(results: Sequence[tuple[str, CheckResult]]) -> list[str]:
    """表示の行（1 項目 1 行。よい / だめ / 飛ばした、項目の名前、理由）。"""
    lines: list[str] = []
    for name, result in results:
        parts = [_LABELS[result.status.value], name]
        if result.reason:
            parts.append(result.reason)
        lines.append(" ".join(parts))
    return lines


def results_to_dict(results: Sequence[tuple[str, CheckResult]]) -> dict[str, Any]:
    """`--json` の形（tests/fixtures/selfcheck_*.json と同じキー）。"""
    return {
        "schema": SELFCHECK_SCHEMA,
        "ok": exit_code(results) == 0,
        "results": [
            {"name": name, "status": result.status.value, "reason": result.reason}
            for name, result in results
        ],
    }


def results_to_json(results: Sequence[tuple[str, CheckResult]]) -> str:
    return json.dumps(results_to_dict(results), ensure_ascii=False)


def exit_code(results: Sequence[tuple[str, CheckResult]]) -> int:
    """だめが 1 つでもあれば 1、なければ 0（6.3）。"""
    return 1 if any(result.status is CheckStatus.FAILED for _, result in results) else 0
