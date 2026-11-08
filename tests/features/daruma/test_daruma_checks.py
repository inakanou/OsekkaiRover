"""だるまさんの self-check の項目（daruma.camera。7.4、7.5）のテスト。

一時の保存先（XDG）と設定ファイルから基盤の ConfigLoader で報告を作り、
基盤の SelfCheckRunner で動かす。動いているサービスの報告は、このファイルの中で
ServiceSnapshot を作って渡す（共有の偽物は使わない）。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from osekkai.core.config import ConfigLoader, ConfigReport, FeatureConfig
from osekkai.core.paths import Paths
from osekkai.core.selfcheck import (
    CheckContext,
    CheckResult,
    CheckStatus,
    SelfCheckRunner,
    render_lines,
    results_to_json,
)
from osekkai.core.status import ServiceSnapshot
from osekkai.features.daruma.checks import (
    DARUMA_CHECKS,
    REASON_CONFIG_UNUSABLE,
    REASON_DISABLED,
    REASON_NOT_RUNNING,
    REASON_PRESENCE_CONFIG_UNUSABLE,
    REASON_PRESENCE_DISABLED,
    REASON_PRESENCE_NOT_RUNNING,
)
from osekkai.features.daruma.config import DarumaConfig
from osekkai.features.presence.config import PresenceConfig

CAMERA = "daruma.camera"
# 出力に出てはいけない、目立つ偽の呼び名（10 文字より短く）
FAKE_CALL = "zzcall12"
BOTH = ("presence", "daruma")


@pytest.fixture
def paths(xdg_dirs: dict[str, Path]) -> Paths:
    return Paths()


def _report(paths: Paths, text: str | None) -> ConfigReport:
    if text is not None:
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        paths.config_file.write_text(text, encoding="utf-8")
    models: dict[str, type[FeatureConfig]] = {"daruma": DarumaConfig, "presence": PresenceConfig}
    return ConfigLoader(paths, models).load(create_missing=False)


def _snapshot(features: Sequence[str] = BOTH) -> ServiceSnapshot:
    return ServiceSnapshot(
        pid=1, version="v0.1.0", heartbeat=1.0, driving=False, features=tuple(features)
    )


def _config(daruma: str = f"{{call_name: {FAKE_CALL}}}", presence: str = "{}") -> str:
    return f"daruma: {daruma}\npresence: {presence}\n"


def _run(
    paths: Paths, text: str | None = None, service: ServiceSnapshot | None = None
) -> CheckResult:
    ctx = CheckContext(
        paths=paths,
        report=_report(paths, _config() if text is None else text),
        service=service,
        hardware_allowed=service is None,
    )
    results = SelfCheckRunner().run(DARUMA_CHECKS, ctx)
    # 呼び名は表示にも JSON にも出ない（7.5）
    assert FAKE_CALL not in "\n".join(render_lines(results))
    assert FAKE_CALL not in results_to_json(results)
    return dict(results)[CAMERA]


# --- 申告 ---


def test_項目の名前と種類() -> None:
    assert [check.name for check in DARUMA_CHECKS] == [CAMERA]
    (check,) = DARUMA_CHECKS
    assert check.hardware is False
    assert check.device is None
    assert check.owner == "daruma"


# --- 自分の設定 ---


def test_設定ファイルがなければ設定を使えないで飛ばす(paths: Paths) -> None:
    ctx = CheckContext(
        paths=paths, report=_report(paths, None), service=_snapshot(), hardware_allowed=False
    )
    result = dict(SelfCheckRunner().run(DARUMA_CHECKS, ctx))[CAMERA]
    assert result == CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)


def test_設定に誤りがあれば設定を使えないで飛ばす(paths: Paths) -> None:
    text = _config(daruma=f"{{call_name: {FAKE_CALL}, max_rounds: 999}}")
    result = _run(paths, text, _snapshot())
    assert result == CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)


def test_設定の節がなければ既定の設定で確かめる(paths: Paths) -> None:
    # 節がないときは既定値（有効）として基盤が報告する
    result = _run(paths, "presence: {}\n", _snapshot())
    assert result == CheckResult(CheckStatus.OK)


def test_無効なら飛ばす(paths: Paths) -> None:
    text = _config(daruma=f"{{enabled: false, call_name: {FAKE_CALL}}}")
    result = _run(paths, text, _snapshot())
    assert result == CheckResult(CheckStatus.SKIPPED, REASON_DISABLED)


# --- ロボット ---


def test_ロボットが動いていなければ飛ばす(paths: Paths) -> None:
    assert _run(paths, service=None) == CheckResult(CheckStatus.SKIPPED, REASON_NOT_RUNNING)


def test_ロボットが動いていなくても見つけてあいさつが無効ならよいに理由を添える(
    paths: Paths,
) -> None:
    text = _config(presence="{enabled: false}")
    assert _run(paths, text, None) == CheckResult(CheckStatus.OK, REASON_PRESENCE_DISABLED)


def test_ロボットが動いていなくても見つけてあいさつの設定を使えなければよいに理由を添える(
    paths: Paths,
) -> None:
    text = _config(presence="{away_minutes: 0}")
    result = _run(paths, text, None)
    assert result == CheckResult(CheckStatus.OK, REASON_PRESENCE_CONFIG_UNUSABLE)


# --- 見つけてあいさつ（だめにしない） ---


def test_見つけてあいさつの設定を使えなければよいに理由を添える(paths: Paths) -> None:
    text = _config(presence="{away_minutes: 0}")
    result = _run(paths, text, _snapshot())
    assert result == CheckResult(CheckStatus.OK, REASON_PRESENCE_CONFIG_UNUSABLE)


def test_見つけてあいさつが無効ならよいに理由を添える(paths: Paths) -> None:
    text = _config(presence="{enabled: false}")
    result = _run(paths, text, _snapshot(("daruma",)))
    assert result == CheckResult(CheckStatus.OK, REASON_PRESENCE_DISABLED)


def test_見つけてあいさつが動いていなければよいに理由を添える(paths: Paths) -> None:
    result = _run(paths, service=_snapshot(("daruma", "routine")))
    assert result == CheckResult(CheckStatus.OK, REASON_PRESENCE_NOT_RUNNING)


def test_見つけてあいさつと遊びが動いていればよい(paths: Paths) -> None:
    assert _run(paths, service=_snapshot()) == CheckResult(CheckStatus.OK)


# --- 理由の文言 ---


def test_理由は決まった文言() -> None:
    assert REASON_CONFIG_UNUSABLE == "設定を使えない"
    assert REASON_DISABLED == "無効"
    assert REASON_NOT_RUNNING == "ロボットが動いていないので確かめない"
    assert REASON_PRESENCE_CONFIG_UNUSABLE == "見つけてあいさつの設定を使えないので遊べない"
    assert REASON_PRESENCE_DISABLED == "見つけてあいさつが無効なので遊べない"
    assert REASON_PRESENCE_NOT_RUNNING == "見つけてあいさつが動いていないので遊べない"
