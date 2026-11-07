"""適用の流れのテスト（1.1、1.2、1.10、3.6、4.2〜4.4、4.6、5.1、5.3、9.4、9.5）。

ユニットの動き（起動・停止・有効化）を覚える偽の Runner と、偽の版・偽の確認で、本当には動かさない。
"""

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from fake_runner import FakeRunner

from rover_deploy.calibration import MigrationResult
from rover_deploy.common import Running, Target
from rover_deploy.layout import CORE_UNIT_NAME, SAMPLE_UNIT_NAME, Layout
from rover_deploy.notices import (
    EVERYDAY_TEXT,
    NOTICE_FILE_NAME,
    REMINDER_TEXT,
    Notice,
    write_notice,
)
from rover_deploy.releases import PrepareFailed, ReleaseError
from rover_deploy.service_status import SafetyVerdict
from rover_deploy.state import DeployState, StateStore, read_log_events, read_log_result
from rover_deploy.system import RunResult, check_result
from rover_deploy.transaction import SAFE_STOP_TIMEOUT_S, Transaction
from rover_deploy.verify import VerifyReport

SHA_A = "a" * 40
SHA_B = "b" * 40
DEPLOY_ID = "20261104T210000Z"
NOW = 1_793_833_200.0
URL = "https://example.invalid/owner/repo.git"
HIDDEN = "ひみつの出力"  # 外のコマンドの出力。記録に出てはいけない

SAMPLE = Target(kind="sample", ref=None, ref_kind=None, sha=None)
TAG_A = Target(kind="release", ref="v0.1.0", ref_kind="tag", sha=SHA_A)
BRANCH_B = Target(kind="release", ref="feat/x", ref_kind="branch", sha=SHA_B)

OK = VerifyReport(ok=True, failed=())
SAFE = SafetyVerdict(ok=True, running=Running.NONE, reason="")


class Units:
    """systemctl --user のユニットの動きを覚える偽の Runner。ほかのコマンドは FakeRunner に回す。"""

    def __init__(self, fake: FakeRunner, active: Collection[str] = ()) -> None:
        self.fake = fake
        self.active = set(active)
        self.enabled: set[str] = set()
        self.calls: list[tuple[str, ...]] = []
        self.fail_start: dict[str, int] = {}  # ユニット → 起動に失敗する残りの回数
        self.fail_stop: set[str] = set()
        self.stuck: set[str] = set()  # 止めても動いたままのユニット

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
        args = tuple(argv)
        if args[:2] != ("systemctl", "--user"):
            return self.fake(args, input=input, timeout=timeout, check=check, cwd=cwd, env=env)
        self.calls.append(args)
        verb, unit = args[2], args[-1]
        result = RunResult(0, b"", HIDDEN.encode())
        if verb == "is-active":
            on = unit in self.active
            result = RunResult(0 if on else 3, b"active\n" if on else b"inactive\n", b"")
        elif verb == "start":
            if self.fail_start.get(unit, 0) > 0:
                self.fail_start[unit] -= 1
                result = RunResult(1, b"", HIDDEN.encode())
            else:
                self.active.add(unit)
        elif verb == "stop":
            if unit in self.fail_stop:
                result = RunResult(1, b"", HIDDEN.encode())
            elif unit not in self.stuck:
                self.active.discard(unit)
        elif verb == "enable":
            self.enabled.add(unit)
        elif verb == "disable":
            self.enabled.discard(unit)
        return check_result(args, result, check=check)

    def verbs(self, verb: str) -> list[str]:
        return [c[-1] for c in self.calls if c[2] == verb]


class FakeReleases:
    """版のディレクトリを作り、リンクの先を覚えるだけの偽物。"""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout
        self.link: Path | None = None
        self.prepare_error: Exception | None = None
        self.install_errors: list[Exception] = []  # 順に 1 回ずつ起こす
        self.prepared: list[Target] = []
        self.switched: list[Path] = []
        self.installed: list[Path] = []
        self.pruned: list[set[Path]] = []

    def make(self, sha: str) -> Path:
        directory = self.layout.release_dir(sha)
        python = directory / ".venv/bin/python"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("", encoding="utf-8")
        example = directory / "src/osekkai/core/example_config.yaml"
        example.parent.mkdir(parents=True, exist_ok=True)
        example.write_text("core: {}\n", encoding="utf-8")
        return directory

    def prepare(self, target: Target, repo_url: str) -> Path:
        assert repo_url == URL
        self.prepared.append(target)
        if self.prepare_error is not None:
            raise self.prepare_error
        assert target.sha is not None
        return self.make(target.sha)

    def current(self) -> Path | None:
        return self.link

    def switch(self, release_dir: Path) -> Path | None:
        assert release_dir.is_dir()
        previous, self.link = self.link, release_dir
        self.switched.append(release_dir)
        return previous

    def install_unit(self, release_dir: Path) -> None:
        self.installed.append(release_dir)
        if self.install_errors:
            raise self.install_errors.pop(0)
        unit = self.layout.core_unit_file
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("", encoding="utf-8")

    def prune(self, keep: Collection[Path]) -> None:
        self.pruned.append(set(keep))


class FakeVerify:
    """決めた確認の結果を順に返し、呼ばれ方を覚える。"""

    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx
        self.release_reports: list[VerifyReport | Exception] = []
        self.sample_reports: list[VerifyReport] = []
        self.release_calls: list[tuple[Path, float]] = []
        self.sample_calls = 0
        self.seen_states: list[DeployState] = []

    def release(
        self,
        release_dir: Path,
        run: object,
        clock: object,
        layout: Layout,
        *,
        since_epoch: float,
        expected_describe: str | None = None,
    ) -> VerifyReport:
        self.release_calls.append((release_dir, since_epoch))
        self.seen_states.append(self.ctx.store.load())
        assert CORE_UNIT_NAME in self.ctx.units.active
        if not self.release_reports:
            return OK
        report = self.release_reports.pop(0)
        if isinstance(report, Exception):
            raise report
        return report

    def sample(self, run: object, clock: object) -> VerifyReport:
        self.sample_calls += 1
        self.seen_states.append(self.ctx.store.load())
        assert SAMPLE_UNIT_NAME in self.ctx.units.active
        return self.sample_reports.pop(0) if self.sample_reports else OK


class FakeClock:
    def monotonic(self) -> float:
        return 0.0

    def sleep(self, seconds: float) -> None:
        raise AssertionError("確認は偽物なので待たない")


class Ctx:
    def __init__(self, tmp_path: Path, *, sample: bool = True) -> None:
        home = tmp_path / "home"
        self.layout = Layout(env={"XDG_RUNTIME_DIR": str(tmp_path / "run")}, home=home, uid=1000)
        if sample:
            (self.layout.sample_dir / ".git").mkdir(parents=True)
            unit = self.layout.sample_unit_file
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text("", encoding="utf-8")
        self.fake = FakeRunner()
        self.units = Units(self.fake)
        self.releases = FakeReleases(self.layout)
        self.store = StateStore(self.layout)
        self.verify = FakeVerify(self)
        self.verdict = SAFE
        self.migrations: list[tuple[Path, Path, Path]] = []
        self.migration_status = "nothing_to_migrate"

    def check_safety(self, run: object, layout: Layout) -> SafetyVerdict:
        return self.verdict

    def migrate(self, sample_conf: Path, config_file: Path, example: Path) -> MigrationResult:
        self.migrations.append((sample_conf, config_file, example))
        return MigrationResult(self.migration_status)  # type: ignore[arg-type]

    def transaction(self) -> Transaction:
        return Transaction(
            self.layout,
            self.units,
            repo_url=URL,
            releases=self.releases,
            state_store=self.store,
            clock=FakeClock(),
            now=lambda: NOW,
            check_safety=self.check_safety,
            verify_release=self.verify.release,
            verify_sample=self.verify.sample,
            migrate=self.migrate,
        )

    def running_release(self, target: Target, *, everyday: Target | None = SAMPLE) -> None:
        """`target` の版が動いている状態にする。"""
        assert target.sha is not None
        self.releases.link = self.releases.make(target.sha)
        self.releases.install_unit(self.releases.link)
        self.releases.installed.clear()
        self.units.active.add(CORE_UNIT_NAME)
        self.store.save(
            DeployState(
                everyday=everyday,
                current=target,
                previous=SAMPLE,
                trial_since=NOW - 100,
                calibration_migrated="nothing_to_migrate",
            )
        )

    def script_safe_stop(self, returncode: int = 0) -> None:
        link = self.releases.link
        assert link is not None
        self.fake.script(
            [str(link / ".venv/bin/python"), "-m", "osekkai.core.safe_stop", "--strict"],
            returncode=returncode,
            stderr=HIDDEN,
        )

    def notice_rows(self) -> list[object] | None:
        path = self.layout.runtime_dir / NOTICE_FILE_NAME
        if not path.exists():
            return None
        rows: list[object] = json.loads(path.read_text(encoding="utf-8"))["rows"]
        return rows

    def events(self) -> list[tuple[object, object]]:
        return [
            (e["step"], e["status"])
            for e in read_log_events(self.layout.log_dir, DEPLOY_ID)
            if "step" in e
        ]


@pytest.fixture
def ctx(tmp_path: Path) -> Ctx:
    return Ctx(tmp_path)


# --- 成功 ---


def test_trial_release_succeeds_without_enabling(ctx: Ctx) -> None:
    result = ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    release_dir = ctx.layout.release_dir(SHA_B)
    assert result.outcome == "succeeded"
    assert result.running == BRANCH_B
    assert result.failed_checks == ()
    assert ctx.releases.prepared == [BRANCH_B]
    assert ctx.releases.link == release_dir
    assert ctx.releases.installed == [release_dir]
    assert ctx.verify.release_calls == [(release_dir, NOW)]
    assert ctx.units.verbs("start") == [CORE_UNIT_NAME]
    # 試しの版は有効にしない（再起動すれば普段の版に戻る）
    assert ctx.units.verbs("enable") == []
    assert ctx.units.verbs("disable") == []
    # 版の知らせ
    assert ctx.notice_rows() == ["feat/x@bbbbbbb", "ためし ふだん:さんぷる"]

    state = ctx.store.load()
    assert state.current == BRANCH_B
    assert state.previous == SAMPLE
    assert state.trial_since == NOW
    assert state.reminded_at is None
    assert state.in_progress is None
    assert state.last_deploy is not None
    assert state.last_deploy.outcome == "succeeded"
    assert state.last_deploy.deploy_id == DEPLOY_ID
    assert state.last_deploy.finished_at == NOW
    assert ctx.releases.pruned == [{release_dir}]


def test_in_progress_is_written_during_the_run_and_cleared(ctx: Ctx) -> None:
    ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    during = ctx.verify.seen_states[0].in_progress
    assert during is not None
    assert during.deploy_id == DEPLOY_ID
    assert during.previous == SAMPLE  # サンプルを止めた後の入れ替え
    assert ctx.store.load().in_progress is None


def test_other_unit_is_confirmed_stopped_before_start(ctx: Ctx) -> None:
    ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    calls = ctx.units.calls
    start = calls.index(("systemctl", "--user", "start", CORE_UNIT_NAME))
    assert ("systemctl", "--user", "is-active", SAMPLE_UNIT_NAME) in calls[:start]


def test_calibration_is_migrated_the_first_time(ctx: Ctx) -> None:
    ctx.migration_status = "migrated"
    ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    release_dir = ctx.layout.release_dir(SHA_B)
    assert ctx.migrations == [
        (
            ctx.layout.sample_dir / "data/picar-x.conf",
            ctx.layout.core_config_dir / "config.yaml",
            release_dir / "src/osekkai/core/example_config.yaml",
        )
    ]
    assert ctx.store.load().calibration_migrated == "migrated"


def test_calibration_is_not_migrated_again(ctx: Ctx) -> None:
    ctx.running_release(TAG_A)
    ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    assert ctx.migrations == []
    assert ctx.store.load().calibration_migrated == "nothing_to_migrate"


def test_running_core_is_stopped_before_switch(ctx: Ctx) -> None:
    ctx.running_release(TAG_A)
    result = ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    assert result.outcome == "succeeded"
    assert ctx.units.verbs("stop") == [CORE_UNIT_NAME]
    state = ctx.store.load()
    assert state.current == BRANCH_B
    assert state.previous == TAG_A
    keep = ctx.releases.pruned[0]
    assert {ctx.layout.release_dir(SHA_A), ctx.layout.release_dir(SHA_B)} <= keep


def test_everyday_release_is_enabled_and_sample_disabled(ctx: Ctx) -> None:
    ctx.units.enabled.add(SAMPLE_UNIT_NAME)
    ctx.store.save(DeployState(everyday=TAG_A))
    result = ctx.transaction().run(TAG_A, DEPLOY_ID, everyday=True)

    assert result.outcome == "succeeded"
    assert ctx.units.enabled == {CORE_UNIT_NAME}
    assert ctx.notice_rows() == ["v0.1.0", EVERYDAY_TEXT]
    state = ctx.store.load()
    assert state.current == TAG_A
    assert state.trial_since is None


def test_back_to_everyday_sample(ctx: Ctx) -> None:
    ctx.running_release(BRANCH_B)
    ctx.units.enabled.add(CORE_UNIT_NAME)  # 前に普段の版だった基盤が有効のまま
    ctx.store.save(replace(ctx.store.load(), reminded_at=NOW - 10))
    write_notice(ctx.layout.runtime_dir, Notice(rows=(None, REMINDER_TEXT), until=None))

    result = ctx.transaction().run(SAMPLE, DEPLOY_ID, everyday=True)

    assert result.outcome == "succeeded"
    assert result.running == SAMPLE
    assert ctx.releases.prepared == []
    assert ctx.releases.switched == []
    assert ctx.migrations == []
    assert ctx.verify.release_calls == []
    assert ctx.verify.sample_calls == 1
    assert ctx.units.verbs("stop") == [CORE_UNIT_NAME]
    assert ctx.units.active == {SAMPLE_UNIT_NAME}
    calls = ctx.units.calls
    start = calls.index(("systemctl", "--user", "start", SAMPLE_UNIT_NAME))
    assert ("systemctl", "--user", "is-active", CORE_UNIT_NAME) in calls[:start]
    # 戻し忘れの知らせを消す
    assert ctx.notice_rows() is None
    assert ctx.units.enabled == {SAMPLE_UNIT_NAME}
    state = ctx.store.load()
    assert state.current == SAMPLE
    assert state.previous == BRANCH_B
    assert state.trial_since is None
    assert state.reminded_at is None


# --- 切り戻し ---


def test_verify_failure_rolls_back_to_previous_release(ctx: Ctx) -> None:
    ctx.running_release(TAG_A)
    ctx.verify.release_reports = [VerifyReport(ok=False, failed=("self_check:core.lcd",)), OK]

    result = ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    dir_a, dir_b = ctx.layout.release_dir(SHA_A), ctx.layout.release_dir(SHA_B)
    assert result.outcome == "rolled_back"
    assert result.running == TAG_A
    assert result.failed_checks == ("self_check:core.lcd",)
    assert ctx.releases.switched == [dir_b, dir_a]
    assert ctx.releases.installed == [dir_b, dir_a]
    assert ctx.releases.link == dir_a
    assert [c[0] for c in ctx.verify.release_calls] == [dir_b, dir_a]
    assert ctx.units.verbs("start") == [CORE_UNIT_NAME, CORE_UNIT_NAME]
    assert ctx.notice_rows() == ["v0.1.0", "ためし ふだん:さんぷる"]
    state = ctx.store.load()
    assert state.current == TAG_A
    assert state.trial_since == NOW - 100  # 前の試しの続き
    assert state.in_progress is None
    assert state.last_deploy is not None
    assert state.last_deploy.outcome == "rolled_back"


def test_verify_failure_rolls_back_to_sample(ctx: Ctx) -> None:
    ctx.verify.release_reports = [VerifyReport(ok=False, failed=("alive",))]

    result = ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    assert result.outcome == "rolled_back"
    assert result.running == SAMPLE
    assert result.failed_checks == ("alive",)
    assert ctx.verify.sample_calls == 1
    assert ctx.units.active == {SAMPLE_UNIT_NAME}
    assert ctx.units.verbs("stop") == [CORE_UNIT_NAME]
    assert ctx.notice_rows() is None
    state = ctx.store.load()
    assert state.current == SAMPLE
    assert state.trial_since is None


def test_start_failure_rolls_back(ctx: Ctx) -> None:
    ctx.running_release(TAG_A)
    ctx.units.fail_start[CORE_UNIT_NAME] = 1  # 新しい版の起動だけ失敗する

    result = ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    assert result.outcome == "rolled_back"
    assert result.running == TAG_A
    assert result.failed_checks == ("start",)
    assert ctx.releases.link == ctx.layout.release_dir(SHA_A)
    assert [c[0] for c in ctx.verify.release_calls] == [ctx.layout.release_dir(SHA_A)]


def test_switch_error_rolls_back(ctx: Ctx) -> None:
    ctx.running_release(TAG_A)
    ctx.releases.install_errors = [ReleaseError("ユニットのファイルを写せませんでした")]

    result = ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    assert result.outcome == "rolled_back"
    assert result.running == TAG_A
    assert result.failed_checks == ("switch",)
    assert ctx.releases.link == ctx.layout.release_dir(SHA_A)
    assert ctx.units.active == {CORE_UNIT_NAME}
    assert ("switch", "failed") in ctx.events()


def test_verify_exception_rolls_back(ctx: Ctx) -> None:
    ctx.running_release(TAG_A)
    ctx.verify.release_reports = [RuntimeError(HIDDEN), OK]

    result = ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    assert result.outcome == "rolled_back"
    assert result.running == TAG_A
    assert result.failed_checks == ("verify",)
    assert ctx.releases.link == ctx.layout.release_dir(SHA_A)
    assert ("verify", "failed") in ctx.events()
    raw = (ctx.layout.log_dir / f"{DEPLOY_ID}.jsonl").read_text(encoding="utf-8")
    assert HIDDEN not in raw
    assert "RuntimeError" in raw


def test_other_unit_still_active_is_not_started_alongside(ctx: Ctx) -> None:
    ctx.running_release(TAG_A)
    ctx.units.active.add(SAMPLE_UNIT_NAME)
    ctx.units.stuck.add(SAMPLE_UNIT_NAME)
    ctx.script_safe_stop(0)

    result = ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    assert ctx.units.verbs("start") == []
    assert result.outcome == "halted"
    assert result.failed_checks[:2] == ("conflict", "rollback:conflict")


def test_rollback_failure_halts_with_strict_safe_stop(ctx: Ctx) -> None:
    ctx.running_release(TAG_A)
    ctx.verify.release_reports = [
        VerifyReport(ok=False, failed=("version_log",)),
        VerifyReport(ok=False, failed=("alive",)),
    ]
    ctx.script_safe_stop(0)  # 切り戻した後のリンクの先（前の版）の safe_stop

    result = ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    assert result.outcome == "halted"
    assert result.running is None
    assert result.failed_checks == ("version_log", "rollback:alive")
    assert ctx.units.active == set()
    safe_stop = [c for c in ctx.fake.calls if "osekkai.core.safe_stop" in c.argv]
    assert len(safe_stop) == 1
    assert safe_stop[0].argv[-1] == "--strict"
    assert safe_stop[0].timeout == SAFE_STOP_TIMEOUT_S
    state = ctx.store.load()
    assert state.current is None
    assert state.in_progress is None
    assert state.last_deploy is not None
    assert state.last_deploy.outcome == "halted"
    assert ("halt", "ok") in ctx.events()


def test_halt_reports_failed_safe_stop(ctx: Ctx) -> None:
    ctx.verify.release_reports = [VerifyReport(ok=False, failed=("alive",))]
    ctx.verify.sample_reports = [VerifyReport(ok=False, failed=("start",))]
    ctx.releases.link = ctx.releases.make(SHA_B)
    ctx.script_safe_stop(1)

    result = ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    assert result.outcome == "halted"
    assert result.failed_checks == ("alive", "rollback:start", "halt:safe_stop")
    assert ("halt", "failed") in ctx.events()


def test_no_previous_version_halts(tmp_path: Path) -> None:
    ctx = Ctx(tmp_path, sample=False)
    ctx.verify.release_reports = [VerifyReport(ok=False, failed=("alive",))]
    ctx.releases.link = ctx.releases.make(SHA_B)
    ctx.script_safe_stop(0)

    result = ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    assert result.outcome == "halted"
    assert result.failed_checks == ("alive", "rollback:no_previous")
    assert ctx.verify.sample_calls == 0


def test_halt_stops_both_units(ctx: Ctx) -> None:
    ctx.units.active = {CORE_UNIT_NAME, SAMPLE_UNIT_NAME}
    ctx.releases.link = ctx.releases.make(SHA_A)
    ctx.releases.install_unit(ctx.releases.link)
    ctx.script_safe_stop(0)

    failed = ctx.transaction().halt()

    assert failed == []
    assert sorted(ctx.units.verbs("stop")) == sorted([CORE_UNIT_NAME, SAMPLE_UNIT_NAME])
    assert ctx.units.active == set()


def test_halt_skips_unit_without_unit_file(tmp_path: Path) -> None:
    ctx = Ctx(tmp_path, sample=False)  # サンプルのユニットのファイルがない
    ctx.units.fail_stop.add(SAMPLE_UNIT_NAME)  # 読み込まれていないユニットの stop は失敗する
    ctx.units.active = {CORE_UNIT_NAME}
    ctx.releases.link = ctx.releases.make(SHA_A)
    ctx.releases.install_unit(ctx.releases.link)
    ctx.script_safe_stop(0)

    failed = ctx.transaction().halt()

    assert failed == []
    assert ctx.units.verbs("stop") == [CORE_UNIT_NAME]
    assert ctx.units.active == set()


def test_halt_without_release_cannot_confirm_safe_stop(ctx: Ctx) -> None:
    ctx.units.fail_stop.add(SAMPLE_UNIT_NAME)

    failed = ctx.transaction().halt()

    assert failed == ["stop_sample", "safe_stop"]


# --- 中止 ---


def test_prepare_failure_changes_nothing(ctx: Ctx) -> None:
    ctx.running_release(TAG_A)
    before = ctx.store.load()
    ctx.releases.prepare_error = PrepareFailed("pip")

    result = ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    assert result.outcome == "aborted"
    assert result.running == TAG_A
    assert result.failed_checks == ("prepare:pip",)
    assert ctx.releases.link == ctx.layout.release_dir(SHA_A)
    assert ctx.releases.switched == []
    assert ctx.units.verbs("stop") == []
    assert ctx.units.verbs("start") == []
    assert ctx.units.active == {CORE_UNIT_NAME}
    assert ctx.notice_rows() is None
    assert ctx.migrations == []
    state = ctx.store.load()
    assert replace(state, last_deploy=None) == replace(before, last_deploy=None)
    assert state.last_deploy is not None
    assert state.last_deploy.outcome == "aborted"
    assert ("prepare", "failed") in ctx.events()


def test_safety_recheck_aborts(ctx: Ctx) -> None:
    ctx.running_release(TAG_A)
    ctx.verdict = SafetyVerdict(ok=False, running=Running.CORE, reason="busy", busy=("talk",))

    result = ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    assert result.outcome == "aborted"
    assert result.failed_checks == ("safety:busy", "busy:talk")
    assert ctx.units.verbs("stop") == []
    assert ctx.releases.switched == []
    assert ctx.store.load().in_progress is None
    assert ("recheck", "failed") in ctx.events()


# --- 記録 ---


def test_log_has_steps_and_result_line_without_command_output(ctx: Ctx) -> None:
    ctx.running_release(TAG_A)
    ctx.verify.release_reports = [VerifyReport(ok=False, failed=("alive",)), OK]

    result = ctx.transaction().run(BRANCH_B, DEPLOY_ID, everyday=False)

    events = ctx.events()
    for step in ("prepare", "recheck", "switch", "verify", "rollback"):
        assert (step, "start") in events or (step, "ok") in events
    assert ("verify", "failed") in events
    assert read_log_result(ctx.layout.log_dir, DEPLOY_ID) == result
    raw = (ctx.layout.log_dir / f"{DEPLOY_ID}.jsonl").read_text(encoding="utf-8")
    assert HIDDEN not in raw
    assert URL not in raw
