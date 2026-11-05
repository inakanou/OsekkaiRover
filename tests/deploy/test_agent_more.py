"""実機のそのほかのサブコマンドのテスト（3.7、4.6、5.2、5.5、6.8、7.2、8.1、11.2）。

偽の Runner と一時の家のディレクトリで、本当には動かさない。`main(argv)` を同じプロセスで呼び、
標準出力の JSON を読む。
"""

import io
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from fake_runner import FakeRunner

from rover_deploy import agent
from rover_deploy.agent import AgentContext, main
from rover_deploy.backups import RestoreFailed, backup_path, create_backup, restore_backup
from rover_deploy.common import (
    DeployResult,
    Running,
    Target,
    dump_json,
    result_to_dict,
    target_to_dict,
)
from rover_deploy.layout import CORE_UNIT_NAME, SAMPLE_UNIT_NAME, Layout
from rover_deploy.notices import NOTICE_FILE_NAME, REMIND_AFTER_H, REMINDER_TEXT
from rover_deploy.provision import ProvisionContext, Step
from rover_deploy.service_status import SafetyVerdict
from rover_deploy.state import (
    DeployLock,
    DeployLog,
    DeployState,
    InProgress,
    LastDeploy,
    StateStore,
    make_deploy_id,
)
from rover_deploy.system import CommandFailed, UnexpectedOutput
from shared_fixtures import load_fixture

SHA_A = "a" * 40
SHA_B = "b" * 40
ID_OLD = "20261104T200000Z"
ID_NEW = "20261104T210000Z"
NOW = 1_793_833_200.0  # 2026-11-04T22:20:00Z
URL = "https://example.invalid/owner/repo.git"

SAMPLE = Target(kind="sample", ref=None, ref_kind=None, sha=None)
TAG_A = Target(kind="release", ref="v0.1.0", ref_kind="tag", sha=SHA_A)
BRANCH_B = Target(kind="release", ref="feat/x", ref_kind="branch", sha=SHA_B)

SAFE = SafetyVerdict(ok=True, running=Running.NONE, reason="")
CORE_SAFE = SafetyVerdict(ok=True, running=Running.CORE, reason="")
SAMPLE_RUNNING = SafetyVerdict(ok=False, running=Running.SAMPLE, reason="sample_running")

IS_ACTIVE = ("systemctl", "--user", "is-active")
STOP = ("systemctl", "--user", "stop")
START = ("systemctl", "--user", "start")


class FakeReleases:
    def __init__(self) -> None:
        self.link: Path | None = None

    def current(self) -> Path | None:
        return self.link

    def dirty_count(self, release_dir: Path) -> int:
        return 0


class FakeClock:
    def monotonic(self) -> float:
        return 0.0

    def sleep(self, seconds: float) -> None:
        raise AssertionError("待たない")


class Ctx:
    def __init__(self, tmp_path: Path, *, sample: bool = False) -> None:
        home = tmp_path / "home"
        home.mkdir()
        self.env = {"XDG_RUNTIME_DIR": str(tmp_path / "run"), "USER": "pi"}
        self.layout = Layout(self.env, home=home, uid=1000)
        if sample:
            (self.layout.sample_dir / ".git").mkdir(parents=True)
            (self.layout.sample_dir / "data").mkdir()
        self.fake = FakeRunner()
        self.out = io.StringIO()
        self.verdict = SAFE
        self.releases = FakeReleases()
        self.store = StateStore(self.layout)
        self.now = NOW
        self.context = AgentContext(
            layout=self.layout,
            env=self.env,
            run=self.fake,
            out=self.out,
            now=lambda: self.now,
            clock=FakeClock(),
            check_safety=lambda run, layout: self.verdict,
            releases=self.releases,
        )

    def main(self, *argv: str) -> tuple[int, list[dict[str, object]]]:
        start = self.out.tell()
        code = main(list(argv), context=self.context)
        text = self.out.getvalue()[start:]
        return code, [json.loads(line) for line in text.splitlines()]

    def one(self, *argv: str) -> dict[str, object]:
        code, lines = self.main(*argv)
        assert code == 0
        assert len(lines) == 1
        return lines[0]

    def release_dir(self, sha: str, *, venv: bool = False) -> Path:
        directory = self.layout.release_dir(sha)
        directory.mkdir(parents=True, exist_ok=True)
        if venv:
            python = directory / ".venv/bin/python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
        return directory

    def link_release(self, sha: str, *, venv: bool = False) -> Path:
        """偽の Releases と本当の current のリンクの両方を、版のディレクトリに向ける。"""
        directory = self.release_dir(sha, venv=venv)
        self.releases.link = directory
        link = self.layout.current_link
        if link.is_symlink():
            link.unlink()
        link.symlink_to(directory)
        return directory

    def running(self, *, core: bool, sample: bool) -> None:
        self.fake.script([*IS_ACTIVE, CORE_UNIT_NAME], stdout="active\n" if core else "inactive\n")
        self.fake.script(
            [*IS_ACTIVE, SAMPLE_UNIT_NAME], stdout="active\n" if sample else "inactive\n"
        )

    def hold_lock(self) -> DeployLock:
        self.layout.state_dir.mkdir(parents=True, exist_ok=True)
        lock = DeployLock(self.layout.lock_file)
        assert lock.try_acquire()
        return lock

    def lock_free(self) -> bool:
        other = DeployLock(self.layout.lock_file)
        if not other.try_acquire():
            return False
        other.release()
        return True


def _json(target: Target) -> str:
    return dump_json(target_to_dict(target))


@pytest.fixture
def ctx(tmp_path: Path) -> Ctx:
    return Ctx(tmp_path)


@pytest.fixture
def sample_ctx(tmp_path: Path) -> Ctx:
    return Ctx(tmp_path, sample=True)


# --- status（5.4、7.2） ---


def test_版の確認_サンプルが動いていて普段の版もサンプル(sample_ctx: Ctx) -> None:
    sample_ctx.verdict = SAMPLE_RUNNING
    report = sample_ctx.one("status")
    assert report["ok"] is True
    assert report["running"] == "sample"
    assert report["current"] == target_to_dict(SAMPLE)
    assert report["current_label"] == "sample"
    assert report["everyday"] == target_to_dict(SAMPLE)
    assert report["everyday_label"] == "sample"
    assert report["is_trial"] is False
    assert report["trial_since"] is None
    assert report["trial_elapsed_s"] is None
    assert report["last_deploy"] is None
    assert report["in_progress"] is None
    assert report["lock"] == "free"


def test_版の確認_試しの版が動いていれば経過を返す(sample_ctx: Ctx) -> None:
    sample_ctx.link_release(SHA_B)
    sample_ctx.store.save(
        DeployState(
            everyday=SAMPLE,
            current=BRANCH_B,
            trial_since=NOW - 600,
            reminded_at=NOW - 60,
            last_deploy=LastDeploy(ID_NEW, "succeeded", NOW - 610),
        )
    )
    sample_ctx.verdict = CORE_SAFE
    report = sample_ctx.one("status")
    assert report["running"] == "core"
    assert report["current"] == target_to_dict(BRANCH_B)
    assert report["current_label"] == "feat/x@bbbbbbb"
    assert report["is_trial"] is True
    assert report["trial_since"] == NOW - 600
    assert report["trial_elapsed_s"] == 600
    assert report["reminded_at"] == NOW - 60
    assert report["last_deploy"] == {
        "id": ID_NEW,
        "outcome": "succeeded",
        "finished_at": NOW - 610,
    }


def test_版の確認_動いている版は記録ではなくリンクの先から求める(ctx: Ctx) -> None:
    ctx.link_release(SHA_A)
    ctx.store.save(DeployState(everyday=TAG_A, current=BRANCH_B))
    ctx.verdict = CORE_SAFE
    report = ctx.one("status")
    assert report["current"] is None
    assert report["is_trial"] is False


def test_版の確認_止まっていれば動いている版はない(ctx: Ctx) -> None:
    ctx.link_release(SHA_A)
    ctx.store.save(DeployState(everyday=TAG_A, current=TAG_A))
    report = ctx.one("status")
    assert report["running"] == "none"
    assert report["current"] is None


def test_版の確認_途中の入れ替えを返す(ctx: Ctx) -> None:
    ctx.store.save(DeployState(in_progress=InProgress(ID_NEW, None)))
    assert ctx.one("status")["in_progress"] == ID_NEW


def test_版の確認_systemctl_が答えなければ分からない(ctx: Ctx) -> None:
    ctx.verdict = SafetyVerdict(ok=False, running=Running.BOTH, reason="unknown")
    assert ctx.one("status")["running"] == "unknown"


def test_版の確認_状態が壊れていれば理由を返す(ctx: Ctx) -> None:
    ctx.layout.state_dir.mkdir(parents=True)
    ctx.layout.state_file.write_text("{", encoding="utf-8")
    report = ctx.one("status")
    assert report["ok"] is False
    assert report["reason"] == "state_broken"
    assert report["running"] == "none"


def test_版の確認は実機を変えない(ctx: Ctx) -> None:
    ctx.one("status")
    assert not ctx.layout.state_dir.exists()


# --- set-everyday（5.2） ---


def test_普段の版を記録する_入れ替えはしない(sample_ctx: Ctx) -> None:
    report = sample_ctx.one("set-everyday", "--target", _json(TAG_A))
    assert report == {
        "ok": True,
        "reason": "",
        "everyday": target_to_dict(TAG_A),
        "everyday_label": "v0.1.0",
    }
    assert sample_ctx.store.load().everyday == TAG_A
    assert sample_ctx.fake.calls == []
    assert sample_ctx.lock_free()


def test_普段の版_今の版と違えば試しの時刻を数え始める(ctx: Ctx) -> None:
    ctx.store.save(DeployState(everyday=TAG_A, current=TAG_A))
    ctx.one("set-everyday", "--target", _json(SAMPLE))
    state = ctx.store.load()
    assert state.everyday == SAMPLE
    assert state.trial_since == NOW


def test_普段の版_今の版と同じになれば試しと戻し忘れを消す(ctx: Ctx) -> None:
    runtime = ctx.layout.runtime_dir
    runtime.mkdir(parents=True)
    (runtime / NOTICE_FILE_NAME).write_text("{}", encoding="utf-8")
    ctx.store.save(
        DeployState(everyday=SAMPLE, current=BRANCH_B, trial_since=NOW - 1, reminded_at=NOW)
    )
    ctx.one("set-everyday", "--target", _json(BRANCH_B))
    state = ctx.store.load()
    assert state.everyday == BRANCH_B
    assert state.trial_since is None
    assert state.reminded_at is None
    assert not (runtime / NOTICE_FILE_NAME).exists()


def test_普段の版_sha_のない版は使い方の誤り(ctx: Ctx) -> None:
    bad = dump_json({"kind": "release", "ref": "v1", "ref_kind": "tag", "sha": "abc"})
    code, lines = ctx.main("set-everyday", "--target", bad)
    assert code == 2
    assert lines == [{"ok": False, "reason": "bad_target"}]
    assert not ctx.layout.state_file.exists()


def test_普段の版_ロックが取られていれば記録しない(ctx: Ctx) -> None:
    lock = ctx.hold_lock()
    try:
        report = ctx.one("set-everyday", "--target", _json(TAG_A))
    finally:
        lock.release()
    assert report == {"ok": False, "reason": "lock_busy"}
    assert not ctx.layout.state_file.exists()


# --- self-check（6.7、6.8） ---


def _self_check_prefix(release: Path) -> list[str]:
    return [str(release / ".venv/bin/python"), "-m", "osekkai", "--self-check", "--json"]


def test_self_check_すべてよい(ctx: Ctx) -> None:
    release = ctx.link_release(SHA_A, venv=True)
    fixture = load_fixture("selfcheck_ok.json")
    ctx.fake.script(_self_check_prefix(release), stdout=json.dumps(fixture))
    report = ctx.one("self-check", "--json")
    assert report["ok"] is True
    assert report["no_hardware"] is False
    assert report["results"] == fixture["results"]
    call = ctx.fake.calls[0]
    assert list(call.argv) == _self_check_prefix(release)
    assert call.cwd == release


def test_self_check_だめの項目と理由を返す(ctx: Ctx) -> None:
    release = ctx.link_release(SHA_A, venv=True)
    fixture = load_fixture("selfcheck_failed.json")
    ctx.fake.script(_self_check_prefix(release), returncode=1, stdout=json.dumps(fixture))
    report = ctx.one("self-check")
    assert report["ok"] is False
    assert report["reason"] == ""
    assert report["results"] == fixture["results"]


def test_self_check_サンプルが動いていればハードに触らない(ctx: Ctx) -> None:
    release = ctx.link_release(SHA_A, venv=True)
    ctx.verdict = SAMPLE_RUNNING
    ctx.fake.script(
        _self_check_prefix(release), stdout=json.dumps(load_fixture("selfcheck_ok.json"))
    )
    report = ctx.one("self-check")
    assert report["no_hardware"] is True
    assert ctx.fake.calls[0].argv[-1] == "--no-hardware"


def test_self_check_今の版がなければ行わない(ctx: Ctx) -> None:
    assert ctx.one("self-check") == {"ok": False, "reason": "no_release"}
    assert ctx.fake.calls == []


def test_self_check_結果が読めなければ理由を返す(ctx: Ctx) -> None:
    release = ctx.link_release(SHA_A, venv=True)
    # 終了コードと結果が合わない
    ctx.fake.script(
        _self_check_prefix(release),
        returncode=1,
        stdout=json.dumps(load_fixture("selfcheck_ok.json")),
    )
    assert ctx.one("self-check") == {"ok": False, "reason": "self_check_unreadable"}


def test_self_check_行えなければ理由を返す(ctx: Ctx) -> None:
    release = ctx.link_release(SHA_A, venv=True)
    ctx.fake.raise_on(_self_check_prefix(release), CommandFailed("python", 1))
    assert ctx.one("self-check") == {"ok": False, "reason": "self_check_error"}


def test_self_check_入れ替えの最中は行わない(ctx: Ctx) -> None:
    ctx.link_release(SHA_A, venv=True)
    lock = ctx.hold_lock()
    try:
        assert ctx.one("self-check") == {"ok": False, "reason": "lock_busy"}
    finally:
        lock.release()
    assert ctx.fake.calls == []


# --- backups（3.7） ---


def _write_config(ctx: Ctx, text: str) -> Path:
    config = ctx.layout.core_config_dir / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text, encoding="utf-8")
    return config


def test_バックアップの一覧は識別子と大きさだけ(ctx: Ctx) -> None:
    _write_config(ctx, "a: 1\n")
    old = create_backup(ctx.layout, ID_OLD, env=ctx.env)
    new = create_backup(ctx.layout, ID_NEW, env=ctx.env)
    report = ctx.one("backups")
    assert report == {
        "ok": True,
        "reason": "",
        "backups": [
            {"id": ID_NEW, "size_bytes": new.size_bytes},
            {"id": ID_OLD, "size_bytes": old.size_bytes},
        ],
    }


def test_バックアップの一覧_なければ空(ctx: Ctx) -> None:
    assert ctx.one("backups") == {"ok": True, "reason": "", "backups": []}


# --- restore（3.7） ---


class RestoreSpy:
    """restore_backup を包み、呼ばれたときに Runner が何を呼んでいたかを残す。"""

    def __init__(self, ctx: Ctx, monkeypatch: pytest.MonkeyPatch, error: Exception | None = None):
        self.ctx = ctx
        self.error = error
        self.seen: list[tuple[str, ...]] | None = None
        self.args: tuple[Path, bool] | None = None
        self._real = restore_backup
        monkeypatch.setattr(agent, "restore_backup", self)

    def __call__(
        self, layout: Layout, backup: Path, *, include_sample: bool, env: object = None
    ) -> None:
        self.seen = list(self.ctx.fake.argvs)
        self.args = (backup, include_sample)
        if self.error is not None:
            raise self.error
        self._real(layout, backup, include_sample=include_sample, env=self.ctx.env)


def _unit_calls(fake: FakeRunner) -> list[tuple[str, ...]]:
    return [argv for argv in fake.argvs if argv[:3] in (STOP, START)]


def test_復元_止めてから戻して起動する(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _write_config(ctx, "a: 1\n")
    create_backup(ctx.layout, ID_OLD, env=ctx.env)
    config.write_text("a: 2\n", encoding="utf-8")
    ctx.verdict = CORE_SAFE
    ctx.fake.script([*STOP, CORE_UNIT_NAME])
    ctx.fake.script([*START, CORE_UNIT_NAME])
    spy = RestoreSpy(ctx, monkeypatch)

    report = ctx.one("restore", ID_OLD)

    assert report["ok"] is True
    assert report["stopped"] == [CORE_UNIT_NAME]
    assert report["started"] == [CORE_UNIT_NAME]
    # 戻すときにはもう止まっていて、まだ起動していない
    assert spy.seen == [(*STOP, CORE_UNIT_NAME)]
    assert _unit_calls(ctx.fake) == [(*STOP, CORE_UNIT_NAME), (*START, CORE_UNIT_NAME)]
    assert config.read_text(encoding="utf-8") == "a: 1\n"
    assert ctx.lock_free()


def test_復元_戻す前に今の状態のバックアップを作る(ctx: Ctx) -> None:
    config = _write_config(ctx, "a: 1\n")
    create_backup(ctx.layout, ID_OLD, env=ctx.env)
    config.write_text("a: 2\n", encoding="utf-8")
    report = ctx.one("restore", ID_OLD)
    pre = report["pre_backup"]
    assert isinstance(pre, dict)
    assert set(pre) == {"id", "size_bytes"}
    pre_path = backup_path(ctx.layout, str(pre["id"]))
    assert pre_path.is_file()
    # 今の状態のバックアップから戻せば、戻す前の中身になる
    ctx.now += 1
    ctx.one("restore", str(pre["id"]))
    assert config.read_text(encoding="utf-8") == "a: 2\n"


def test_復元_止まっていれば起動もしない(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(ctx, "a: 1\n")
    create_backup(ctx.layout, ID_OLD, env=ctx.env)
    report = ctx.one("restore", ID_OLD)
    assert report["ok"] is True
    assert report["stopped"] == []
    assert report["started"] == []
    assert ctx.fake.calls == []


@pytest.mark.parametrize("reason", ["driving", "busy", "unknown", "sample_running"])
def test_復元_安全でなければ何もしない(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    _write_config(ctx, "a: 1\n")
    create_backup(ctx.layout, ID_OLD, env=ctx.env)
    ctx.verdict = SafetyVerdict(ok=False, running=Running.CORE, reason=reason)  # type: ignore[arg-type]
    spy = RestoreSpy(ctx, monkeypatch)
    report = ctx.one("restore", ID_OLD)
    assert report["ok"] is False
    assert report["reason"] == reason
    assert report["pre_backup"] is None
    assert spy.seen is None
    assert ctx.fake.calls == []
    assert [p.name for p in ctx.layout.backups_dir.iterdir()] == [f"backup-{ID_OLD}.tar.gz"]


def test_復元_サンプルのデータは指定したときだけ(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(ctx, "a: 1\n")
    create_backup(ctx.layout, ID_OLD, env=ctx.env)
    spy = RestoreSpy(ctx, monkeypatch)
    report = ctx.one("restore", ID_OLD, "--include-sample")
    assert report["include_sample"] is True
    assert spy.args == (backup_path(ctx.layout, ID_OLD), True)
    ctx.now += 1
    ctx.one("restore", ID_OLD)
    assert spy.args == (backup_path(ctx.layout, ID_OLD), False)


def test_復元_送ったファイルから戻す(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _write_config(ctx, "a: 1\n")
    made = create_backup(ctx.layout, ID_OLD, env=ctx.env)
    sent = ctx.layout.backups_dir / "upload.tar.gz"
    made.path.rename(sent)
    config.write_text("a: 2\n", encoding="utf-8")
    report = ctx.one("restore", "--file", str(sent))
    assert report["ok"] is True
    assert config.read_text(encoding="utf-8") == "a: 1\n"


def test_復元_置き場所の外のファイルは使い方の誤り(ctx: Ctx, tmp_path: Path) -> None:
    outside = tmp_path / "x.tar.gz"
    outside.write_bytes(b"")
    code, lines = ctx.main("restore", "--file", str(outside))
    assert code == 2
    assert lines == [{"ok": False, "reason": "bad_backup_file"}]
    # 置き場所を指すリンクで外に出ることもできない
    ctx.layout.backups_dir.mkdir(parents=True)
    link = ctx.layout.backups_dir / "link.tar.gz"
    link.symlink_to(outside)
    code, lines = ctx.main("restore", "--file", str(link))
    assert code == 2


def test_復元_識別子の形が違えば使い方の誤り(ctx: Ctx) -> None:
    code, lines = ctx.main("restore", "../x")
    assert code == 2
    assert lines == [{"ok": False, "reason": "bad_deploy_id"}]


def test_復元_識別子もファイルもなければ使い方の誤り(ctx: Ctx) -> None:
    code, _ = ctx.main("restore")
    assert code == 2


def test_復元_バックアップがなければ何もしない(ctx: Ctx) -> None:
    report = ctx.one("restore", ID_OLD)
    assert report["ok"] is False
    assert report["reason"] == "no_backup"
    assert ctx.fake.calls == []


def test_復元_ロックが取られていれば断る(ctx: Ctx) -> None:
    _write_config(ctx, "a: 1\n")
    create_backup(ctx.layout, ID_OLD, env=ctx.env)
    lock = ctx.hold_lock()
    try:
        assert ctx.one("restore", ID_OLD) == {"ok": False, "reason": "lock_busy"}
    finally:
        lock.release()


def test_復元_戻せなくても止めたサービスを起動する(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(ctx, "a: 1\n")
    create_backup(ctx.layout, ID_OLD, env=ctx.env)
    ctx.verdict = CORE_SAFE
    ctx.fake.script([*STOP, CORE_UNIT_NAME])
    ctx.fake.script([*START, CORE_UNIT_NAME])
    RestoreSpy(ctx, monkeypatch, error=RestoreFailed())
    report = ctx.one("restore", ID_OLD)
    assert report["ok"] is False
    assert report["reason"] == "restore_failed"
    assert report["started"] == [CORE_UNIT_NAME]


def test_復元_止められなければ戻さない(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(ctx, "a: 1\n")
    create_backup(ctx.layout, ID_OLD, env=ctx.env)
    ctx.verdict = CORE_SAFE
    ctx.fake.raise_on([*STOP, CORE_UNIT_NAME], CommandFailed("systemctl", 1))
    spy = RestoreSpy(ctx, monkeypatch)
    report = ctx.one("restore", ID_OLD)
    assert report["reason"] == "stop_failed"
    assert spy.seen is None


def test_復元_戻した後に起動できなければ知らせる(ctx: Ctx) -> None:
    _write_config(ctx, "a: 1\n")
    create_backup(ctx.layout, ID_OLD, env=ctx.env)
    ctx.verdict = CORE_SAFE
    ctx.fake.script([*STOP, CORE_UNIT_NAME])
    ctx.fake.raise_on([*START, CORE_UNIT_NAME], CommandFailed("systemctl", 1))
    report = ctx.one("restore", ID_OLD)
    assert report["ok"] is False
    assert report["reason"] == "start_failed_after"
    assert report["started"] == []


# --- stop（11.2） ---


def _safe_stop_argv(release: Path) -> list[str]:
    return [str(release / ".venv/bin/python"), "-m", "osekkai.core.safe_stop", "--strict"]


def _unit_files(ctx: Ctx) -> None:
    ctx.layout.user_unit_dir.mkdir(parents=True)
    ctx.layout.core_unit_file.write_text("", encoding="utf-8")
    ctx.layout.sample_unit_file.write_text("", encoding="utf-8")


def test_停止_両方のユニットを止めて厳しい安全停止(ctx: Ctx) -> None:
    release = ctx.link_release(SHA_A, venv=True)
    _unit_files(ctx)
    ctx.fake.script([*STOP, CORE_UNIT_NAME])
    ctx.fake.script([*STOP, SAMPLE_UNIT_NAME])
    ctx.fake.script(_safe_stop_argv(release), returncode=0)
    report = ctx.one("stop")
    assert report == {"ok": True, "reason": "", "failed": []}
    assert ctx.fake.argvs == [
        (*STOP, CORE_UNIT_NAME),
        (*STOP, SAMPLE_UNIT_NAME),
        tuple(_safe_stop_argv(release)),
    ]
    assert ctx.lock_free()


def test_停止_モーターが止まったと確かめられなければ知らせる(ctx: Ctx) -> None:
    release = ctx.link_release(SHA_A, venv=True)
    ctx.fake.script(_safe_stop_argv(release), returncode=1)
    report = ctx.one("stop")
    assert report == {"ok": False, "reason": "halt_failed", "failed": ["safe_stop"]}


def test_停止_今の版がなければ安全停止は失敗(ctx: Ctx) -> None:
    _unit_files(ctx)
    ctx.fake.script([*STOP, CORE_UNIT_NAME])
    ctx.fake.raise_on([*STOP, SAMPLE_UNIT_NAME], CommandFailed("systemctl", 1))
    report = ctx.one("stop")
    assert report["failed"] == ["stop_sample", "safe_stop"]


def test_停止_ロックが取られていれば断る(ctx: Ctx) -> None:
    lock = ctx.hold_lock()
    try:
        assert ctx.one("stop") == {"ok": False, "reason": "lock_busy"}
    finally:
        lock.release()
    assert ctx.fake.calls == []


# --- remind（5.5） ---


def _trial_state(since: float) -> DeployState:
    return DeployState(everyday=SAMPLE, current=BRANCH_B, trial_since=since)


def test_戻し忘れ_長く試しの版なら知らせて時刻を残す(ctx: Ctx) -> None:
    ctx.store.save(_trial_state(NOW - REMIND_AFTER_H * 3600))
    ctx.running(core=True, sample=False)
    report = ctx.one("remind")
    assert report == {"ok": True, "reason": "", "reminded": True}
    notice = json.loads((ctx.layout.runtime_dir / NOTICE_FILE_NAME).read_text(encoding="utf-8"))
    assert notice["rows"] == [None, REMINDER_TEXT]
    assert ctx.store.load().reminded_at == NOW
    assert ctx.lock_free()


def test_戻し忘れ_まだ早ければ知らせない(ctx: Ctx) -> None:
    ctx.store.save(_trial_state(NOW - 60))
    ctx.running(core=True, sample=False)
    assert ctx.one("remind") == {"ok": True, "reason": "", "reminded": False}
    assert not (ctx.layout.runtime_dir / NOTICE_FILE_NAME).exists()
    assert ctx.store.load().reminded_at is None


def test_戻し忘れ_サンプルが動いていれば知らせない(ctx: Ctx) -> None:
    ctx.store.save(_trial_state(NOW - 10 * 3600))
    ctx.running(core=False, sample=True)
    assert ctx.one("remind")["reminded"] is False


def test_戻し忘れ_状態がなくても静かに終わる(ctx: Ctx) -> None:
    ctx.running(core=False, sample=False)
    assert ctx.one("remind") == {"ok": True, "reason": "", "reminded": False}


def test_戻し忘れ_動いているか分からなければ知らせない(ctx: Ctx) -> None:
    ctx.store.save(_trial_state(NOW - 10 * 3600))
    ctx.fake.raise_on([*IS_ACTIVE, CORE_UNIT_NAME], UnexpectedOutput("systemctl"))
    report = ctx.one("remind")
    assert report == {"ok": False, "reason": "running_unknown", "reminded": False}


def test_戻し忘れ_状態が壊れていれば知らせない(ctx: Ctx) -> None:
    ctx.layout.state_dir.mkdir(parents=True)
    ctx.layout.state_file.write_text("[", encoding="utf-8")
    report = ctx.one("remind")
    assert report == {"ok": False, "reason": "state_broken", "reminded": False}


def test_戻し忘れ_入れ替えの最中は何もしない(ctx: Ctx) -> None:
    ctx.store.save(_trial_state(NOW - 10 * 3600))
    lock = ctx.hold_lock()
    try:
        report = ctx.one("remind")
    finally:
        lock.release()
    assert report == {"ok": True, "reason": "lock_busy", "reminded": False}
    assert ctx.fake.calls == []


# --- provision（8.1〜8.8） ---


class ProvisionSpy:
    def __init__(self, ctx: Ctx, monkeypatch: pytest.MonkeyPatch, steps: Sequence[Step]) -> None:
        self.ctx = ctx
        self.steps = list(steps)
        self.context: ProvisionContext | None = None
        self.lock_held: list[bool] = []
        monkeypatch.setattr(agent, "provision_steps", self)

    def __call__(self, context: ProvisionContext) -> list[Step]:
        self.context = context
        self.lock_held.append(not self.ctx.lock_free())
        return self.steps


def test_初期設定_手順を行い結果を_JSON_で返す(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    done: list[str] = []
    steps = [
        Step("os", lambda: True, lambda: None),
        Step("boot_config", lambda: False, lambda: done.append("boot"), needs_reboot=True),
        Step("self_check", lambda: False, lambda: None, outcome=lambda: True),
    ]
    spy = ProvisionSpy(ctx, monkeypatch, steps)
    report = ctx.one("provision", "--target", _json(TAG_A), "--repo-url", URL)
    assert report == {
        "ok": True,
        "reason": "",
        "skipped": ["os"],
        "applied": ["boot_config", "self_check"],
        "failed_step": None,
        "retry_hint": "",
        "needs_reboot": True,
        "self_check_ok": True,
    }
    assert done == ["boot"]
    assert spy.lock_held == [True]
    context = spy.context
    assert context is not None
    assert context.target == TAG_A
    assert context.repo_url == URL
    assert context.user == "pi"
    assert context.data_dir == ctx.layout.home / ".local/share/osekkai"
    assert context.layout is ctx.layout
    # 経過は標準エラーに出し、標準出力は JSON だけ
    err = capsys.readouterr().err
    assert "[boot_config]" in err
    assert ctx.lock_free()


def test_初期設定_失敗した手順とやり直し方を返す(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        raise RuntimeError("ひみつ")

    steps = [
        Step("apt", lambda: False, fail, retry_hint="もう一度"),
        Step("layout", lambda: False, lambda: None),
    ]
    ProvisionSpy(ctx, monkeypatch, steps)
    report = ctx.one("provision", "--repo-url", URL)
    assert report["ok"] is False
    assert report["reason"] == "provision_failed"
    assert report["failed_step"] == "apt"
    assert report["retry_hint"] == "もう一度"
    assert report["applied"] == []
    assert "ひみつ" not in json.dumps(report, ensure_ascii=False)


def test_初期設定_版の指定がなければ_None(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = ProvisionSpy(ctx, monkeypatch, [])
    ctx.one("provision", "--repo-url", URL)
    assert spy.context is not None
    assert spy.context.target is None


def test_初期設定_ロックが取られていれば断る(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = ProvisionSpy(ctx, monkeypatch, [])
    lock = ctx.hold_lock()
    try:
        assert ctx.one("provision", "--repo-url", URL) == {"ok": False, "reason": "lock_busy"}
    finally:
        lock.release()
    assert spy.context is None


def test_初期設定_リポジトリの_URL_がなければ使い方の誤り(ctx: Ctx) -> None:
    code, _ = ctx.main("provision")
    assert code == 2


# --- log（4.6） ---


def _write_log(ctx: Ctx, deploy_id: str, *, result: bool = True) -> DeployResult:
    log = DeployLog(ctx.layout.log_dir, deploy_id, clock=lambda: NOW)
    log.event("prepare", "start")
    log.event("prepare", "ok")
    outcome = DeployResult(deploy_id, "rolled_back", None, ("alive",))
    if result:
        log.result(outcome)
    return outcome


def test_記録の一覧は新しい順(ctx: Ctx) -> None:
    _write_log(ctx, ID_OLD)
    _write_log(ctx, ID_NEW)
    assert ctx.one("log", "--list") == {"ok": True, "reason": "", "ids": [ID_NEW, ID_OLD]}


def test_記録_識別子がなければ最新の_1_件(ctx: Ctx) -> None:
    _write_log(ctx, ID_OLD)
    newest = _write_log(ctx, ID_NEW)
    report = ctx.one("log")
    assert report["deploy_id"] == ID_NEW
    assert report["result"] == result_to_dict(newest)
    assert report["events"] == [
        {"t": NOW, "step": "prepare", "status": "start", "reason": ""},
        {"t": NOW, "step": "prepare", "status": "ok", "reason": ""},
    ]


def test_記録_識別子を指定する(ctx: Ctx) -> None:
    old = _write_log(ctx, ID_OLD)
    _write_log(ctx, ID_NEW)
    report = ctx.one("log", ID_OLD)
    assert report["ok"] is True
    assert report["deploy_id"] == ID_OLD
    assert report["result"] == result_to_dict(old)


def test_記録_結果がまだなければ_None(ctx: Ctx) -> None:
    _write_log(ctx, ID_NEW, result=False)
    report = ctx.one("log", ID_NEW)
    assert report["result"] is None
    assert len(report["events"]) == 2  # type: ignore[arg-type]


def test_記録_なければ理由を返す(ctx: Ctx) -> None:
    assert ctx.one("log") == {"ok": False, "reason": "no_log"}
    assert ctx.one("log", ID_NEW) == {"ok": False, "reason": "no_log"}
    assert ctx.one("log", "--list") == {"ok": True, "reason": "", "ids": []}


def test_記録_識別子の形が違えば使い方の誤り(ctx: Ctx) -> None:
    code, lines = ctx.main("log", "../../etc")
    assert code == 2
    assert lines == [{"ok": False, "reason": "bad_deploy_id"}]


# --- どのサブコマンドも JSON を 1 つ ---


@pytest.mark.parametrize(
    "argv",
    [
        ("status",),
        ("backups",),
        ("self-check",),
        ("log", "--list"),
        ("log",),
    ],
)
def test_どれも_JSON_を_1_つ返す(ctx: Ctx, argv: tuple[str, ...]) -> None:
    code, lines = ctx.main(*argv)
    assert code == 0
    assert len(lines) == 1
    assert "ok" in lines[0]


def test_今の状態のバックアップと同じ名前があれば戻さない(ctx: Ctx) -> None:
    # 同じ秒に作ったバックアップを上書きしない
    _write_config(ctx, "a: 1\n")
    same = make_deploy_id(NOW)
    create_backup(ctx.layout, same, env=ctx.env)
    report = ctx.one("restore", same)
    assert report["ok"] is False
    assert report["reason"] == "backup_failed"
