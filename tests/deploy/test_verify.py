"""入れた後の確認のテスト（4.1、4.3）。偽の Runner と偽の時計で、本当には待たない。"""

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest
from fake_runner import FakeRunner

from rover_deploy.layout import CORE_UNIT_NAME, SAMPLE_UNIT_NAME, Layout
from rover_deploy.service_status import STATUS_FILE_NAME
from rover_deploy.system import CommandTimedOut, RunResult, UnexpectedOutput
from rover_deploy.verify import (
    ALIVE_S,
    SELF_CHECK_TIMEOUT_S,
    START_TIMEOUT_S,
    VERSION_LOG_PREFIX,
    VerifyReport,
    has_version_log,
    journal_since_arg,
    parse_self_check,
    verify_release,
    verify_sample,
)
from shared_fixtures import load_fixture, version_log_prefix

PID = 4321
DESCRIBE = "v0.2.0-3-gabc1234"
SINCE = 1_700_000_000.75
START = 1000.0

CORE_SHOW = ("systemctl", "--user", "show", "--property=MainPID,NRestarts,ActiveState")
JOURNAL = ("journalctl",)
GIT = ("git",)


def show(state: str = "active", pid: int = PID, restarts: int = 0) -> str:
    return f"MainPID={pid}\nNRestarts={restarts}\nActiveState={state}\n"


class FakeClock:
    """sleep で時を進めるだけの時計。sleep のたびに `on_sleep` を呼ぶ。"""

    def __init__(self, start: float = START) -> None:
        self.now = start
        self.on_sleep: Callable[[float], None] | None = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.now)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    runtime = tmp_path / "run"
    return Layout(env={"XDG_RUNTIME_DIR": str(runtime)}, home=tmp_path / "home", uid=1000)


@pytest.fixture
def release(tmp_path: Path) -> Path:
    path = tmp_path / "home/osekkai-rover/releases/abc123456789"
    path.mkdir(parents=True)
    return path


def write_status(layout: Layout, *, pid: int = PID, heartbeat: float = START) -> None:
    path = layout.runtime_dir / STATUS_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_fixture("status_idle.json")
    data["pid"] = pid
    data["heartbeat"] = heartbeat
    path.write_text(json.dumps(data), encoding="utf-8")


def self_check_argv(release: Path) -> tuple[str, ...]:
    return (str(release / ".venv/bin/python"), "-m", "osekkai", "--self-check", "--json")


OK_JSON = json.dumps(load_fixture("selfcheck_ok.json"), ensure_ascii=False)
FAILED_JSON = json.dumps(load_fixture("selfcheck_failed.json"), ensure_ascii=False)


def script_ok(
    run: FakeRunner,
    release: Path,
    *,
    describe: str = DESCRIBE,
    check_rc: int = 0,
    check_out: str = OK_JSON,
    check_error: Exception | None = None,
    journal_error: Exception | None = None,
) -> None:
    """起動・self-check・journal の台本を決める。引数で一部だけを悪くする。"""
    run.script(CORE_SHOW, stdout=show())
    if check_error is None:
        run.script(self_check_argv(release), returncode=check_rc, stdout=check_out)
    else:
        run.raise_on(self_check_argv(release), check_error)
    if journal_error is None:
        line = f"INFO osekkai.core.runtime: {VERSION_LOG_PREFIX}{describe}"
        run.script(JOURNAL, stdout=f"{line}\nINFO osekkai: 起動\n")
    else:
        run.raise_on(JOURNAL, journal_error)


class TimedRunner:
    """呼ばれたときの時計の値を、コマンドの名前ごとに覚える Runner。"""

    def __init__(self, run: FakeRunner, clock: FakeClock) -> None:
        self.run = run
        self.clock = clock
        self.times: dict[tuple[str, ...], float] = {}

    def __call__(
        self,
        argv: Sequence[str],
        *,
        input: bytes | None = None,
        timeout: float | None = None,
        check: bool = False,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        self.times[tuple(argv)[:2]] = self.clock.now
        return self.run(argv, input=input, timeout=timeout, check=check, cwd=cwd, env=env)


def verify(run: FakeRunner, release: Path, clock: FakeClock, layout: Layout) -> VerifyReport:
    return verify_release(
        release, run, clock, layout, since_epoch=SINCE, expected_describe=DESCRIBE
    )


# --- 版: よいとき ---


def test_すべてよければ_ok(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout)
    script_ok(fake_runner, release)

    report = verify(fake_runner, release, clock, layout)

    assert report == VerifyReport(ok=True, failed=())


def test_self_check_は版の仮想環境で_JSON_を求めて行う(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout)
    script_ok(fake_runner, release)

    verify(fake_runner, release, clock, layout)

    calls = [c for c in fake_runner.calls if c.argv == self_check_argv(release)]
    assert len(calls) == 1
    assert calls[0].cwd == release
    assert calls[0].timeout == SELF_CHECK_TIMEOUT_S


def test_journal_は起動の時刻からの基盤のユニットを読む(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout)
    script_ok(fake_runner, release)

    verify(fake_runner, release, clock, layout)

    [journal] = [c.argv for c in fake_runner.calls if c.argv[0] == "journalctl"]
    assert f"--user-unit={CORE_UNIT_NAME}" in journal
    assert "--user" not in journal
    assert journal[journal.index("--since") + 1] == "@1700000000"


def test_since_は秒の端数を切り捨てる() -> None:
    assert journal_since_arg(1_700_000_000.99) == "@1700000000"


def test_動き続けるのを_ALIVE_S_の間確かめる(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout)
    script_ok(fake_runner, release)
    timed = TimedRunner(fake_runner, clock)

    report = verify_release(
        release, timed, clock, layout, since_epoch=SINCE, expected_describe=DESCRIBE
    )

    assert report.ok
    self_check_at = timed.times[self_check_argv(release)[:2]]
    assert timed.times[("journalctl", f"--user-unit={CORE_UNIT_NAME}")] - self_check_at >= ALIVE_S


def test_版の名前を渡さなければ版のディレクトリで_git_describe_を行う(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout)
    script_ok(fake_runner, release, describe="v0.3.0")
    fake_runner.script(GIT, stdout="v0.3.0\n")

    report = verify_release(release, fake_runner, clock, layout, since_epoch=SINCE)

    assert report.ok
    assert ("git", "-C", str(release), "describe", "--tags", "--always", "--dirty") in (
        fake_runner.argvs
    )


# --- 版: 起動しない・報告しない ---


def test_起動しなければ_start_で_self_check_を行わない(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout)
    fake_runner.script(CORE_SHOW, stdout=show("activating", pid=0))

    report = verify(fake_runner, release, clock, layout)

    assert report == VerifyReport(ok=False, failed=("start",))
    assert self_check_argv(release) not in fake_runner.argvs
    assert START_TIMEOUT_S <= clock.now - START < START_TIMEOUT_S + 2


def test_systemctl_が答えなければ_start(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    fake_runner.raise_on(CORE_SHOW, UnexpectedOutput("systemctl"))

    report = verify(fake_runner, release, clock, layout)

    assert report.failed == ("start",)


def test_遅れて起動すれば待つ(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    fake_runner.script(CORE_SHOW, stdout=show("activating", pid=0))
    fake_runner.script(CORE_SHOW, stdout=show("activating", pid=0))
    script_ok(fake_runner, release)
    clock.on_sleep = lambda now: write_status(layout, heartbeat=now)

    report = verify(fake_runner, release, clock, layout)

    assert report.ok


def test_状態を報告しなければ_status_で_self_check_を行わない(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    fake_runner.script(CORE_SHOW, stdout=show())

    report = verify(fake_runner, release, clock, layout)

    assert report == VerifyReport(ok=False, failed=("status",))
    assert self_check_argv(release) not in fake_runner.argvs


def test_報告の_pid_が_MainPID_と違えば_status(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout, pid=PID + 1)
    fake_runner.script(CORE_SHOW, stdout=show())

    report = verify(fake_runner, release, clock, layout)

    assert report.failed == ("status",)


def test_心拍が古ければ_status(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout, heartbeat=START - 60)
    fake_runner.script(CORE_SHOW, stdout=show())

    report = verify(fake_runner, release, clock, layout)

    assert report.failed == ("status",)


def test_報告が遅れて始まれば待ってから_self_check_を行う(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    script_ok(fake_runner, release)
    marks: list[float] = []

    def later(now: float) -> None:
        if now >= START + 5 and not marks:
            write_status(layout, heartbeat=now)
            marks.append(now)

    clock.on_sleep = later

    report = verify(fake_runner, release, clock, layout)

    assert report.ok
    assert marks


# --- 版: self-check ---


def test_self_check_のだめの項目を集める(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout)
    script_ok(fake_runner, release, check_rc=1, check_out=FAILED_JSON)

    report = verify(fake_runner, release, clock, layout)

    assert report == VerifyReport(ok=False, failed=("self_check:core.lcd",))
    # 理由（self-check の文言）は写さない
    assert all("応答" not in name for name in report.failed)


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, "読めない"),
        (0, ""),
        (2, json.dumps(load_fixture("selfcheck_ok.json"))),
        (1, json.dumps(load_fixture("selfcheck_ok.json"))),
        (0, json.dumps(load_fixture("selfcheck_failed.json"))),
    ],
    ids=["読めない", "空", "終了コード2", "よいのに1", "だめなのに0"],
)
def test_self_check_の結果がおかしければ_self_check(
    fake_runner: FakeRunner,
    release: Path,
    clock: FakeClock,
    layout: Layout,
    returncode: int,
    stdout: str,
) -> None:
    write_status(layout)
    script_ok(fake_runner, release, check_rc=returncode, check_out=stdout)

    report = verify(fake_runner, release, clock, layout)

    assert report == VerifyReport(ok=False, failed=("self_check",))


def test_self_check_が時間切れなら_self_check(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout)
    script_ok(fake_runner, release, check_error=CommandTimedOut("python"))

    report = verify(fake_runner, release, clock, layout)

    assert report.failed == ("self_check",)


def test_self_check_のだめでも_残りの確認を行う(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout)
    script_ok(fake_runner, release, describe="v0.1.0", check_rc=1, check_out=FAILED_JSON)
    fake_runner.script(CORE_SHOW, stdout=show())  # 報告を待つ
    fake_runner.script(CORE_SHOW, stdout=show())  # 確かめ始め
    fake_runner.script(CORE_SHOW, stdout=show(restarts=1))

    report = verify(fake_runner, release, clock, layout)

    assert report.failed == ("self_check:core.lcd", "alive", "version_log")


def test_見本の_JSON_を読める() -> None:
    assert parse_self_check(json.dumps(load_fixture("selfcheck_ok.json"))) == ()
    assert parse_self_check(json.dumps(load_fixture("selfcheck_failed.json"))) == ("core.lcd",)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(schema=2),
        lambda d: d.update(extra=1),
        lambda d: d["results"][0].update(status="maybe"),
        lambda d: d["results"][0].update(status=["failed"]),
        lambda d: d.update(results={}),
        lambda d: d["results"][0].update(name="名前 に 空白"),
        lambda d: d["results"][0].pop("reason"),
        lambda d: d.update(ok=True),
    ],
    ids=[
        "schema",
        "余分なキー",
        "status",
        "status が一覧",
        "results が辞書",
        "名前の形",
        "reason がない",
        "ok と合わない",
    ],
)
def test_形の違う_JSON_は読まない(change: Callable[[dict[str, object]], object]) -> None:
    data = load_fixture("selfcheck_failed.json")
    change(data)
    assert parse_self_check(json.dumps(data)) is None


# --- 版: 動き続けるか ---


@pytest.mark.parametrize(
    "later",
    [show(restarts=1), show(pid=PID + 1), show("inactive", pid=0), show("activating")],
    ids=["再起動の回数", "MainPID", "止まった", "起動し直し中"],
)
def test_動き続けなければ_alive(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout, later: str
) -> None:
    write_status(layout)
    script_ok(fake_runner, release)
    fake_runner.script(CORE_SHOW, stdout=show())  # 報告を待つ
    fake_runner.script(CORE_SHOW, stdout=show())  # 確かめ始め
    fake_runner.script(CORE_SHOW, stdout=later)

    report = verify(fake_runner, release, clock, layout)

    assert report.failed == ("alive",)


def test_途中で_systemctl_が答えなければ_alive(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout)
    script_ok(fake_runner, release)
    fake_runner.script(CORE_SHOW, stdout=show())
    fake_runner.script(CORE_SHOW, stdout=show())
    fake_runner.raise_on(CORE_SHOW, UnexpectedOutput("systemctl"))

    report = verify(fake_runner, release, clock, layout)

    assert report.failed == ("alive",)


def test_終わりの間際の再起動も見つける(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout)
    script_ok(fake_runner, release)
    alive_polls = int(ALIVE_S)  # 1 秒ごとに聞く
    for _ in range(alive_polls + 1):
        fake_runner.script(CORE_SHOW, stdout=show())
    fake_runner.script(CORE_SHOW, stdout=show(restarts=1))

    report = verify(fake_runner, release, clock, layout)

    assert report.failed == ("alive",)


# --- 版: 版のログ ---


def test_版のログがなければ_version_log(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout)
    script_ok(fake_runner, release, describe="v0.1.0")

    report = verify(fake_runner, release, clock, layout)

    assert report == VerifyReport(ok=False, failed=("version_log",))


def test_journal_を読めなければ_version_log(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout)
    script_ok(fake_runner, release, journal_error=CommandTimedOut("journalctl"))

    report = verify(fake_runner, release, clock, layout)

    assert report.failed == ("version_log",)


def test_版の名前を求められなければ_version_log(
    fake_runner: FakeRunner, release: Path, clock: FakeClock, layout: Layout
) -> None:
    write_status(layout)
    script_ok(fake_runner, release)
    fake_runner.script(GIT, returncode=128)

    report = verify_release(release, fake_runner, clock, layout, since_epoch=SINCE)

    assert report.failed == ("version_log",)


def test_版のログの文言の頭は共通の見本と同じ() -> None:
    assert version_log_prefix() == VERSION_LOG_PREFIX


def test_見本の版のログを_journal_の形で見つける() -> None:
    example = load_fixture("version_log.json")["example"]
    describe = example[len(VERSION_LOG_PREFIX) :]
    assert has_version_log([f"INFO osekkai.core.runtime: {example}"], describe)
    assert has_version_log([example], describe)


@pytest.mark.parametrize(
    "line",
    [
        f"INFO osekkai.core.runtime: {VERSION_LOG_PREFIX}{DESCRIBE}-dirty",
        f"INFO osekkai.core.runtime: {VERSION_LOG_PREFIX}v0.2.0",
        f"INFO osekkai: 何か{VERSION_LOG_PREFIX}{DESCRIBE}",
        DESCRIBE,
    ],
    ids=["dirty", "別の版", "文の途中", "頭がない"],
)
def test_違う版のログは数えない(line: str) -> None:
    assert not has_version_log([line], DESCRIBE)


# --- サンプル ---

SAMPLE_SHOW = ("systemctl", "--user", "show", "--property=MainPID,NRestarts,ActiveState")


def test_サンプルが動き続ければ_ok(fake_runner: FakeRunner, clock: FakeClock) -> None:
    fake_runner.script(SAMPLE_SHOW, stdout=show())

    report = verify_sample(fake_runner, clock)

    assert report == VerifyReport(ok=True, failed=())
    assert all(argv[-1] == SAMPLE_UNIT_NAME for argv in fake_runner.argvs)
    assert clock.now - START >= ALIVE_S


def test_サンプルが再起動すれば_alive(fake_runner: FakeRunner, clock: FakeClock) -> None:
    fake_runner.script(SAMPLE_SHOW, stdout=show())
    fake_runner.script(SAMPLE_SHOW, stdout=show())
    fake_runner.script(SAMPLE_SHOW, stdout=show(restarts=2))

    report = verify_sample(fake_runner, clock)

    assert report == VerifyReport(ok=False, failed=("alive",))


def test_サンプルが起動しなければ_start(fake_runner: FakeRunner, clock: FakeClock) -> None:
    fake_runner.script(SAMPLE_SHOW, stdout=show("failed", pid=0))

    report = verify_sample(fake_runner, clock)

    assert report == VerifyReport(ok=False, failed=("start",))
    assert START_TIMEOUT_S <= clock.now - START < START_TIMEOUT_S + 2
