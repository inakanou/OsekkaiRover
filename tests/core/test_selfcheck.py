"""self-check の契約と実行のテスト（device-deployment 6.2、6.3、6.4、6.6、6.7）。"""

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from osekkai.core.config import ConfigReport, CoreConfig
from osekkai.core.feature import FeatureSpec
from osekkai.core.paths import Paths
from osekkai.core.selfcheck import (
    REASON_SERVICE,
    REASON_SKIPPED_BUSY,
    REASON_TIMEOUT,
    SELFCHECK_SCHEMA,
    Check,
    CheckContext,
    CheckResult,
    CheckStatus,
    SelfCheckRunner,
    collect_checks,
    exit_code,
    render_lines,
    results_to_dict,
    results_to_json,
)
from osekkai.core.status import ServiceSnapshot
from osekkai.core.types import Resource
from shared_fixtures import load_fixture

OK = CheckResult(CheckStatus.OK)


def _ok(_ctx: CheckContext) -> CheckResult:
    return OK


def _ctx(
    tmp_path: Path, *, hardware_allowed: bool = True, service: ServiceSnapshot | None = None
) -> CheckContext:
    return CheckContext(
        paths=Paths({}, home=tmp_path, uid=1000),
        report=ConfigReport(core=CoreConfig(), features={}),
        service=service,
        hardware_allowed=hardware_allowed,
    )


def _service(unavailable: tuple[str, ...] = ()) -> ServiceSnapshot:
    return ServiceSnapshot(
        pid=4321, version="v0.2.0", heartbeat=1.0, driving=False, unavailable=unavailable
    )


@pytest.fixture
def release() -> Iterator[threading.Event]:
    """止まった項目のスレッドを、テストの終わりに放す。"""
    event = threading.Event()
    yield event
    event.set()


# --- 契約 ---


def test_結果は既定で理由が空() -> None:
    assert CheckResult(CheckStatus.OK).reason == ""


def test_状態の値はJSONの文字() -> None:
    assert [s.value for s in CheckStatus] == ["ok", "failed", "skipped"]


def test_項目の既定の時間は10秒でハードを触らない() -> None:
    check = Check("core.config", _ok)
    assert check.timeout_s == 10.0
    assert check.hardware is False
    assert check.device is None


@pytest.mark.parametrize("name", ["core", "core.", ".lcd", "Core.lcd", "core.lcd.x", "core-x.y"])
def test_形の誤った名前の項目は作れない(name: str) -> None:
    with pytest.raises(ValueError):
        Check(name, _ok)


@pytest.mark.parametrize("timeout", [0.0, -1.0])
def test_時間が正でない項目は作れない(timeout: float) -> None:
    with pytest.raises(ValueError):
        Check("core.config", _ok, timeout_s=timeout)


# --- 実行 ---


def test_項目を順に動かして結果を並べる(tmp_path: Path) -> None:
    def failed(_ctx: CheckContext) -> CheckResult:
        return CheckResult(CheckStatus.FAILED, "応答がない")

    results = SelfCheckRunner().run(
        [Check("core.config", _ok), Check("core.lcd", failed)], _ctx(tmp_path)
    )
    assert results == [
        ("core.config", OK),
        ("core.lcd", CheckResult(CheckStatus.FAILED, "応答がない")),
    ]


def test_項目には文脈を渡す(tmp_path: Path) -> None:
    seen: list[CheckContext] = []

    def remember(ctx: CheckContext) -> CheckResult:
        seen.append(ctx)
        return OK

    ctx = _ctx(tmp_path)
    SelfCheckRunner().run([Check("core.config", remember)], ctx)
    assert seen == [ctx]


def test_項目は別のスレッドで動く(tmp_path: Path) -> None:
    threads: list[threading.Thread] = []

    def where(_ctx: CheckContext) -> CheckResult:
        threads.append(threading.current_thread())
        return OK

    SelfCheckRunner().run([Check("core.config", where)], _ctx(tmp_path))
    assert threads[0] is not threading.current_thread()
    assert threads[0].daemon


def test_時間切れの項目はだめにして次へ進む(tmp_path: Path, release: threading.Event) -> None:
    def stuck(_ctx: CheckContext) -> CheckResult:
        release.wait()
        return OK

    results = SelfCheckRunner().run(
        [Check("core.lcd", stuck, timeout_s=0.05), Check("core.config", _ok)], _ctx(tmp_path)
    )
    assert results == [
        ("core.lcd", CheckResult(CheckStatus.FAILED, REASON_TIMEOUT)),
        ("core.config", OK),
    ]
    assert REASON_TIMEOUT == "時間切れ"


def test_例外の項目はだめにして理由は型の名前だけ(tmp_path: Path) -> None:
    def broken(_ctx: CheckContext) -> CheckResult:
        raise FileNotFoundError("/home/someone/hidden.yaml たろう")

    results = SelfCheckRunner().run(
        [Check("core.storage", broken), Check("core.config", _ok)], _ctx(tmp_path)
    )
    assert results == [
        ("core.storage", CheckResult(CheckStatus.FAILED, "FileNotFoundError")),
        ("core.config", OK),
    ]


def test_例外の中身はログにも出さない(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    def broken(_ctx: CheckContext) -> CheckResult:
        raise RuntimeError("たろう sk-hidden")

    SelfCheckRunner().run([Check("core.storage", broken)], _ctx(tmp_path))
    assert "たろう" not in caplog.text
    assert "sk-hidden" not in caplog.text


def test_結果の形が違う項目はだめ(tmp_path: Path) -> None:
    def wrong(_ctx: CheckContext) -> CheckResult:
        return "ok"  # type: ignore[return-value]

    [(_, result)] = SelfCheckRunner().run([Check("core.config", wrong)], _ctx(tmp_path))
    assert result.status is CheckStatus.FAILED
    assert "ok" not in result.reason


# --- ハードの項目 ---


def test_ハードを触ってよければハードの項目を動かす(tmp_path: Path) -> None:
    results = SelfCheckRunner().run(
        [Check("core.lcd", _ok, hardware=True, device="display")], _ctx(tmp_path)
    )
    assert results == [("core.lcd", OK)]


def _never(_ctx: CheckContext) -> CheckResult:
    raise AssertionError("ハードの項目を動かしてはいけない")


def test_報告がなければハードの項目は飛ばす(tmp_path: Path) -> None:
    results = SelfCheckRunner().run(
        [Check("core.lcd", _never, hardware=True, device="display")],
        _ctx(tmp_path, hardware_allowed=False),
    )
    assert results == [("core.lcd", CheckResult(CheckStatus.SKIPPED, REASON_SKIPPED_BUSY))]
    assert REASON_SKIPPED_BUSY == "ほかが動いている"


def test_報告で使えるデバイスならよい(tmp_path: Path) -> None:
    results = SelfCheckRunner().run(
        [Check("core.lcd", _never, hardware=True, device="display")],
        _ctx(tmp_path, hardware_allowed=False, service=_service(("servos",))),
    )
    assert results == [("core.lcd", CheckResult(CheckStatus.OK, REASON_SERVICE))]
    assert REASON_SERVICE == "サービスの報告"


def test_報告で使えないデバイスならだめ(tmp_path: Path) -> None:
    results = SelfCheckRunner().run(
        [Check("core.servos", _never, hardware=True, device="servos")],
        _ctx(tmp_path, hardware_allowed=False, service=_service(("servos",))),
    )
    assert results == [("core.servos", CheckResult(CheckStatus.FAILED, REASON_SERVICE))]


def test_デバイスの名前のないハードの項目は報告があっても飛ばす(tmp_path: Path) -> None:
    results = SelfCheckRunner().run(
        [Check("demo.arm", _never, hardware=True)],
        _ctx(tmp_path, hardware_allowed=False, service=_service()),
    )
    assert results == [("demo.arm", CheckResult(CheckStatus.SKIPPED, REASON_SKIPPED_BUSY))]


def test_ハードを触ってよくなくてもハードでない項目は動かす(tmp_path: Path) -> None:
    results = SelfCheckRunner().run(
        [Check("core.config", _ok)], _ctx(tmp_path, hardware_allowed=False)
    )
    assert results == [("core.config", OK)]


# --- 項目の集め方 ---


def _spec(name: str, *checks: Check) -> FeatureSpec:
    return FeatureSpec(
        name=name, priority=30, resources=frozenset({Resource.DISPLAY}), checks=checks
    )


def test_機能の項目は基盤の項目の後に並ぶ() -> None:
    base = [Check("core.config", _ok), Check("core.lcd", _ok)]
    specs = [
        _spec("demo", Check("demo.ready", _ok)),
        _spec("stars"),
        _spec("talk", Check("talk.voice", _ok), Check("talk.words", _ok)),
    ]
    names = [c.name for c in collect_checks(base, specs)]
    assert names == ["core.config", "core.lcd", "demo.ready", "talk.voice", "talk.words"]


def test_基盤の項目は_core_で始まる() -> None:
    with pytest.raises(ValueError):
        collect_checks([Check("demo.ready", _ok)], [])


def test_機能の項目はその機能の名前で始まる() -> None:
    with pytest.raises(ValueError):
        collect_checks([], [_spec("demo", Check("talk.voice", _ok))])


def test_同じ名前の項目は並べない() -> None:
    with pytest.raises(ValueError):
        collect_checks([], [_spec("demo", Check("demo.ready", _ok), Check("demo.ready", _ok))])


# --- 表示・JSON・終了コード ---


def _failed_results() -> list[tuple[str, CheckResult]]:
    return [
        ("core.config", OK),
        ("core.lcd", CheckResult(CheckStatus.FAILED, "応答がない")),
        ("core.servos", CheckResult(CheckStatus.SKIPPED, REASON_SKIPPED_BUSY)),
    ]


def _ok_results() -> list[tuple[str, CheckResult]]:
    return [
        ("core.config", OK),
        ("core.lcd", CheckResult(CheckStatus.OK, REASON_SERVICE)),
        ("demo.ready", OK),
    ]


def test_表示は1項目1行() -> None:
    assert render_lines(_failed_results()) == [
        "よい core.config",
        "だめ core.lcd 応答がない",
        "飛ばした core.servos ほかが動いている",
    ]


def test_JSONはだめのある見本と同じ() -> None:
    assert results_to_dict(_failed_results()) == load_fixture("selfcheck_failed.json")
    assert json.loads(results_to_json(_failed_results())) == load_fixture("selfcheck_failed.json")


def test_JSONはよいだけの見本と同じ() -> None:
    assert results_to_dict(_ok_results()) == load_fixture("selfcheck_ok.json")
    assert json.loads(results_to_json(_ok_results())) == load_fixture("selfcheck_ok.json")
    assert SELFCHECK_SCHEMA == 1


def test_JSONは日本語をそのまま書く() -> None:
    assert "応答がない" in results_to_json(_failed_results())


def test_だめがあれば終了コード1() -> None:
    assert exit_code(_failed_results()) == 1


def test_だめがなければ終了コード0() -> None:
    assert exit_code(_ok_results()) == 0
    skipped = [("core.lcd", CheckResult(CheckStatus.SKIPPED, REASON_SKIPPED_BUSY))]
    assert exit_code(skipped) == 0
    assert exit_code([]) == 0
