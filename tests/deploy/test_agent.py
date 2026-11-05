"""実機の入れ替えのサブコマンドのテスト（1.5、1.7、1.9、2.4、3.1、3.2、4.5、9.2）。

偽の Runner と一時の家のディレクトリで、本当には動かさない。`main(argv)` を同じプロセスで呼び、
標準出力の JSON を読む。
"""

import io
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from fake_runner import FakeRunner

from rover_deploy import agent
from rover_deploy.agent import (
    DEPLOY_UNIT,
    MIB,
    RELEASE_SPACE_MB,
    AgentContext,
    main,
)
from rover_deploy.backups import (
    backup_path,
    estimate_backup_bytes,
    sample_backup_path,
)
from rover_deploy.common import (
    DeployResult,
    Running,
    Target,
    dump_json,
    result_to_dict,
    target_to_dict,
)
from rover_deploy.layout import CORE_UNIT_NAME, SAMPLE_UNIT_NAME, Layout
from rover_deploy.releases import ReleaseError
from rover_deploy.service_status import SafetyVerdict, check_safety
from rover_deploy.state import (
    DeployLock,
    DeployLog,
    DeployState,
    InProgress,
    StateStore,
    read_log_result,
)
from rover_deploy.system import UnexpectedOutput

SHA_A = "a" * 40
SHA_B = "b" * 40
DEPLOY_ID = "20261104T210000Z"
NOW = 1_793_833_200.0
URL = "https://example.invalid/owner/repo.git"
GIB = 1024 * MIB

SAMPLE = Target(kind="sample", ref=None, ref_kind=None, sha=None)
TAG_A = Target(kind="release", ref="v0.1.0", ref_kind="tag", sha=SHA_A)
BRANCH_B = Target(kind="release", ref="feat/x", ref_kind="branch", sha=SHA_B)

SAFE = SafetyVerdict(ok=True, running=Running.NONE, reason="")
IS_ACTIVE_DEPLOY = ("systemctl", "--user", "is-active", DEPLOY_UNIT)


class FakeReleases:
    def __init__(self) -> None:
        self.link: Path | None = None
        self.dirty: int | Exception = 0
        self.counted: list[Path] = []

    def current(self) -> Path | None:
        return self.link

    def dirty_count(self, release_dir: Path) -> int:
        self.counted.append(release_dir)
        if isinstance(self.dirty, Exception):
            raise self.dirty
        return self.dirty


class FakeClock:
    """待つたびに時計を進め、待つときに決めた動きをする。"""

    def __init__(self) -> None:
        self.t = 0.0
        self.on_sleep: list[Callable[[], None]] = []
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.t += seconds
        if self.on_sleep:
            self.on_sleep.pop(0)()


class FakeTransaction:
    def __init__(self, result: DeployResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[Target, str, bool, str]] = []
        self.lock_held: list[bool] = []


class Ctx:
    def __init__(self, tmp_path: Path, *, sample: bool = False) -> None:
        home = tmp_path / "home"
        home.mkdir()
        self.env = {"XDG_RUNTIME_DIR": str(tmp_path / "run")}
        self.layout = Layout(self.env, home=home, uid=1000)
        if sample:
            (self.layout.sample_dir / ".git").mkdir(parents=True)
            (self.layout.sample_dir / "data").mkdir()
            (self.layout.sample_dir / "data/picar-x.conf").write_text("x\n", encoding="utf-8")
        self.fake = FakeRunner()
        self.out = io.StringIO()
        self.verdict = SAFE
        self.free = 10 * GIB
        self.releases = FakeReleases()
        self.clock = FakeClock()
        self.transaction = FakeTransaction(DeployResult(DEPLOY_ID, "succeeded", TAG_A, ()))
        self.urls: list[str] = []
        self.store = StateStore(self.layout)
        self.context = AgentContext(
            layout=self.layout,
            env=self.env,
            run=self.fake,
            out=self.out,
            now=lambda: NOW,
            clock=self.clock,
            free_bytes=lambda path: self.free,
            check_safety=lambda run, layout: self.verdict,
            releases=self.releases,
            transaction=self._make_transaction,
        )

    def _make_transaction(self, context: AgentContext, repo_url: str) -> "Ctx":
        self.urls.append(repo_url)
        return self

    # FakeTransaction の口（Ctx が自分で受ける）
    def run(self, target: Target, deploy_id: str, *, everyday: bool) -> DeployResult:
        tx = self.transaction
        tx.calls.append((target, deploy_id, everyday, self.urls[-1]))
        other = DeployLock(self.layout.lock_file)
        tx.lock_held.append(not other.try_acquire())
        other.release()
        if isinstance(tx.result, Exception):
            raise tx.result
        log = DeployLog(self.layout.log_dir, deploy_id, clock=lambda: NOW)
        log.event("prepare", "ok")
        log.result(tx.result)
        return tx.result

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

    def link_release(self, sha: str) -> Path:
        directory = self.layout.release_dir(sha)
        directory.mkdir(parents=True, exist_ok=True)
        self.releases.link = directory
        return directory


def _target_json(target: Target) -> str:
    return dump_json(target_to_dict(target))


@pytest.fixture
def ctx(tmp_path: Path) -> Ctx:
    return Ctx(tmp_path)


@pytest.fixture
def sample_ctx(tmp_path: Path) -> Ctx:
    return Ctx(tmp_path, sample=True)


# --- preflight ---


def test_事前の確認_すべてよいとき(ctx: Ctx) -> None:
    report = ctx.one("preflight", "--target", _target_json(TAG_A))
    assert report["ok"] is True
    assert report["reason"] == ""
    assert report["reasons"] == []
    assert report["running"] == "none"
    assert report["current"] is None
    assert report["current_label"] is None
    assert report["target"] == target_to_dict(TAG_A)
    assert report["target_label"] == "v0.1.0"
    assert report["safety"] == {"ok": True, "reason": "", "busy": []}
    assert report["lock"] == "free"
    assert report["interrupted"] is None
    assert report["dirty"] is None
    assert report["first_deploy"] is False


def test_事前の確認は実機を変えない(tmp_path: Path) -> None:
    c = Ctx(tmp_path, sample=True)
    c.context.check_safety = check_safety  # 本物の判断で、読むだけのコマンドを確かめる
    c.fake.script(
        ("systemctl", "--user", "is-active", CORE_UNIT_NAME), returncode=3, stdout="inactive\n"
    )
    c.fake.script(("systemctl", "--user", "is-active", SAMPLE_UNIT_NAME), stdout="active\n")
    before = sorted(p for p in tmp_path.rglob("*"))
    report = c.one("preflight", "--target", _target_json(TAG_A))
    assert report["reason"] == "sample_running"
    assert sorted(p for p in tmp_path.rglob("*")) == before  # ファイルを作らない
    assert all(argv[2] == "is-active" for argv in c.fake.argvs)


def test_事前の確認_サンプルが動いていれば今の版はサンプルで中止(sample_ctx: Ctx) -> None:
    sample_ctx.verdict = SafetyVerdict(ok=False, running=Running.SAMPLE, reason="sample_running")
    report = sample_ctx.one("preflight", "--target", _target_json(TAG_A))
    assert report["ok"] is False
    assert report["reason"] == "sample_running"
    assert report["running"] == "sample"
    assert report["current"] == target_to_dict(SAMPLE)
    assert report["current_label"] == "sample"
    assert report["first_deploy"] is True
    assert report["everyday"] == target_to_dict(SAMPLE)


@pytest.mark.parametrize("reason", ["driving", "unknown"])
def test_事前の確認_走行中や確かめられないときは中止(ctx: Ctx, reason: str) -> None:
    ctx.verdict = SafetyVerdict(ok=False, running=Running.CORE, reason=reason)  # type: ignore[arg-type]
    report = ctx.one("preflight", "--target", _target_json(TAG_A))
    assert report["ok"] is False
    assert report["reason"] == reason
    assert report["running"] == "core"


def test_事前の確認_作業中なら機能の名前を返す(ctx: Ctx) -> None:
    ctx.verdict = SafetyVerdict(
        ok=False, running=Running.CORE, reason="busy", busy=("daily_routine", "greeting")
    )
    report = ctx.one("preflight", "--target", _target_json(TAG_A))
    assert report["reason"] == "busy"
    assert report["safety"] == {
        "ok": False,
        "reason": "busy",
        "busy": ["daily_routine", "greeting"],
    }


def test_事前の確認_systemctl_が答えなければ動いているものは分からない(ctx: Ctx) -> None:
    ctx.verdict = SafetyVerdict(ok=False, running=Running.BOTH, reason="unknown")
    report = ctx.one("preflight", "--target", _target_json(TAG_A))
    assert report["running"] == "unknown"
    assert report["reason"] == "unknown"


def test_事前の確認_基盤が動いていればリンクの先と同じ版を今の版にする(ctx: Ctx) -> None:
    ctx.store.save(DeployState(current=TAG_A))
    ctx.link_release(SHA_A)
    ctx.verdict = SafetyVerdict(ok=True, running=Running.CORE, reason="")
    report = ctx.one("preflight", "--target", _target_json(BRANCH_B))
    assert report["current"] == target_to_dict(TAG_A)
    assert report["current_label"] == "v0.1.0"
    assert report["target_label"] == f"feat/x@{SHA_B[:7]}"


def test_事前の確認_リンクの先が違えば今の版は分からない(ctx: Ctx) -> None:
    ctx.store.save(DeployState(current=TAG_A))
    ctx.link_release(SHA_B)
    ctx.verdict = SafetyVerdict(ok=True, running=Running.CORE, reason="")
    report = ctx.one("preflight", "--target", _target_json(BRANCH_B))
    assert report["current"] is None


def test_事前の確認_空き容量が足りなければ中止(sample_ctx: Ctx) -> None:
    needed = (
        estimate_backup_bytes(sample_ctx.layout, include_sample=True, env=sample_ctx.env)
        + RELEASE_SPACE_MB * MIB
    )
    sample_ctx.free = needed - 1
    report = sample_ctx.one("preflight", "--target", _target_json(TAG_A))
    assert report["ok"] is False
    assert report["reason"] == "disk_low"
    assert report["disk"] == {"ok": False, "free_bytes": needed - 1, "needed_bytes": needed}
    sample_ctx.free = needed
    report = sample_ctx.one("preflight", "--target", _target_json(TAG_A))
    assert report["ok"] is True


def test_事前の確認_初回でなければサンプルの大きさを数えない(sample_ctx: Ctx) -> None:
    sample_ctx.store.save(replace(sample_ctx.store.default(), sample_saved=DEPLOY_ID))
    report = sample_ctx.one("preflight", "--target", _target_json(TAG_A))
    assert report["first_deploy"] is False
    disk = report["disk"]
    assert isinstance(disk, dict)
    assert disk["needed_bytes"] == (
        estimate_backup_bytes(sample_ctx.layout, include_sample=False, env=sample_ctx.env)
        + RELEASE_SPACE_MB * MIB
    )


def test_事前の確認_空き容量を読めなければ中止(ctx: Ctx) -> None:
    def broken(path: Path) -> int:
        raise OSError("読めない")

    ctx.context.free_bytes = broken
    report = ctx.one("preflight", "--target", _target_json(TAG_A))
    assert report["reason"] == "disk_low"


def test_事前の確認_今の版に手の変更があれば件数だけ返して中止(ctx: Ctx) -> None:
    directory = ctx.link_release(SHA_A)
    ctx.releases.dirty = 3
    report = ctx.one("preflight", "--target", _target_json(BRANCH_B))
    assert report["ok"] is False
    assert report["reason"] == "dirty"
    assert report["dirty"] == 3
    assert ctx.releases.counted == [directory]


def test_事前の確認_手の変更がなければ_0(ctx: Ctx) -> None:
    ctx.link_release(SHA_A)
    report = ctx.one("preflight", "--target", _target_json(BRANCH_B))
    assert report["ok"] is True
    assert report["dirty"] == 0


def test_事前の確認_手の変更を数えられなければ中止(ctx: Ctx) -> None:
    ctx.link_release(SHA_A)
    ctx.releases.dirty = ReleaseError("数えられない")
    report = ctx.one("preflight", "--target", _target_json(BRANCH_B))
    assert report["reason"] == "dirty_unknown"
    assert report["dirty"] is None


def test_事前の確認_ロックが取られていれば中止(ctx: Ctx) -> None:
    lock = DeployLock(ctx.layout.lock_file)
    assert lock.try_acquire()
    try:
        ctx.store.save(DeployState(in_progress=InProgress(DEPLOY_ID, None)))
        report = ctx.one("preflight", "--target", _target_json(TAG_A))
    finally:
        lock.release()
    assert report["ok"] is False
    assert report["reason"] == "lock_busy"
    assert report["lock"] == "busy"
    # 入れ替えが進んでいる間の途中の印は、途中で終わったものではない
    assert report["interrupted"] is None
    assert "interrupted" not in report["reasons"]  # type: ignore[operator]


def test_事前の確認_ロックを確かめた後は外す(ctx: Ctx) -> None:
    ctx.layout.state_dir.mkdir(parents=True)
    ctx.layout.lock_file.write_text("", encoding="utf-8")
    report = ctx.one("preflight", "--target", _target_json(TAG_A))
    assert report["lock"] == "free"
    lock = DeployLock(ctx.layout.lock_file)
    assert lock.try_acquire()
    lock.release()


def test_事前の確認_前の入れ替えが途中で終わっていれば中止(ctx: Ctx) -> None:
    ctx.store.save(DeployState(in_progress=InProgress(DEPLOY_ID, SAMPLE)))
    report = ctx.one("preflight", "--target", _target_json(TAG_A))
    assert report["ok"] is False
    assert report["reason"] == "interrupted"
    assert report["interrupted"] == DEPLOY_ID


def test_事前の確認_状態が壊れていれば中止(ctx: Ctx) -> None:
    ctx.layout.state_dir.mkdir(parents=True)
    ctx.layout.state_file.write_text("{", encoding="utf-8")
    report = ctx.one("preflight", "--target", _target_json(TAG_A))
    assert report["reason"] == "state_broken"


def test_事前の確認_理由はすべて並べる(ctx: Ctx) -> None:
    ctx.store.save(DeployState(in_progress=InProgress(DEPLOY_ID, SAMPLE)))
    ctx.verdict = SafetyVerdict(ok=False, running=Running.CORE, reason="driving")
    ctx.free = 0
    ctx.link_release(SHA_A)
    ctx.releases.dirty = 1
    report = ctx.one("preflight", "--target", _target_json(TAG_A))
    assert report["reasons"] == ["interrupted", "driving", "disk_low", "dirty"]
    assert report["reason"] == "interrupted"


def test_事前の確認_版の形が違えば使い方の誤り(ctx: Ctx) -> None:
    code, lines = ctx.main("preflight", "--target", '{"kind":"release","ref":"x"}')
    assert code == 2
    assert lines == [{"ok": False, "reason": "bad_target"}]


def test_引数がなければ使い方の誤り(ctx: Ctx) -> None:
    code, lines = ctx.main("preflight")
    assert code == 2
    assert lines == []


# --- backup ---


def test_バックアップ_初回はサンプルのバックアップも作る(sample_ctx: Ctx) -> None:
    sample_ctx.layout.core_config_dir.mkdir(parents=True)
    (sample_ctx.layout.core_config_dir / "config.yaml").write_text("a: 1\n", encoding="utf-8")
    report = sample_ctx.one("backup", "--deploy-id", DEPLOY_ID)
    assert report["ok"] is True
    assert report["first_deploy"] is True
    path = backup_path(sample_ctx.layout, DEPLOY_ID)
    sample_path = sample_backup_path(sample_ctx.layout, DEPLOY_ID)
    assert report["backup"] == {
        "id": DEPLOY_ID,
        "path": str(path),
        "size_bytes": path.stat().st_size,
    }
    assert report["sample_backup"] == {
        "id": DEPLOY_ID,
        "path": str(sample_path),
        "size_bytes": sample_path.stat().st_size,
    }
    assert path.stat().st_mode & 0o777 == 0o600
    # サンプルの記録はまだしない（開発 PC が受け取ってから mark-sample-saved で記録する）
    assert sample_ctx.store.load().sample_saved is None


def test_バックアップ_サンプルを保存済みならサンプルは作らない(sample_ctx: Ctx) -> None:
    sample_ctx.store.save(replace(sample_ctx.store.default(), sample_saved="20261101T000000Z"))
    report = sample_ctx.one("backup", "--deploy-id", DEPLOY_ID)
    assert report["ok"] is True
    assert report["first_deploy"] is False
    assert report["sample_backup"] is None
    assert not sample_backup_path(sample_ctx.layout, DEPLOY_ID).exists()


def test_バックアップ_古い世代を消す(ctx: Ctx) -> None:
    ids = [f"2026110{i}T000000Z" for i in range(1, 7)]
    for backup_id in ids:
        assert ctx.one("backup", "--deploy-id", backup_id)["ok"] is True
    names = sorted(p.name for p in ctx.layout.backups_dir.iterdir())
    assert names == [f"backup-{i}.tar.gz" for i in ids[1:]]


def test_バックアップ_ロックが取られていれば中止(ctx: Ctx) -> None:
    lock = DeployLock(ctx.layout.lock_file)
    assert lock.try_acquire()
    try:
        report = ctx.one("backup", "--deploy-id", DEPLOY_ID)
    finally:
        lock.release()
    assert report == {"ok": False, "reason": "lock_busy"}
    assert not ctx.layout.backups_dir.exists()


def test_バックアップ_ロックは終わったら外す(ctx: Ctx) -> None:
    ctx.one("backup", "--deploy-id", DEPLOY_ID)
    lock = DeployLock(ctx.layout.lock_file)
    assert lock.try_acquire()
    lock.release()


def test_バックアップ_作れなければ中止(ctx: Ctx) -> None:
    ctx.layout.state_dir.mkdir(parents=True)
    ctx.layout.backups_dir.write_text("", encoding="utf-8")  # ディレクトリにできない
    report = ctx.one("backup", "--deploy-id", DEPLOY_ID)
    assert report == {"ok": False, "reason": "backup_failed"}


def test_バックアップ_サンプルのバックアップを作れなければ中止(sample_ctx: Ctx) -> None:
    pip = sample_ctx.layout.sample_venv / "bin/pip"
    pip.parent.mkdir(parents=True)
    pip.write_text("", encoding="utf-8")
    sample_ctx.fake.script((str(pip), "freeze"), returncode=1)
    report = sample_ctx.one("backup", "--deploy-id", DEPLOY_ID)
    assert report == {"ok": False, "reason": "backup_failed"}


def test_バックアップ_途中で止まった復元を先に片付ける(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        agent, "recover_interrupted_restore", lambda layout, env: calls.append("recover")
    )
    ctx.one("backup", "--deploy-id", DEPLOY_ID)
    assert calls == ["recover"]


def test_バックアップ_識別子の形が違えば使い方の誤り(ctx: Ctx) -> None:
    code, lines = ctx.main("backup", "--deploy-id", "../x")
    assert code == 2
    assert lines == [{"ok": False, "reason": "bad_deploy_id"}]


# --- mark-sample-saved ---


def test_サンプルを受け取れたことを記録する(sample_ctx: Ctx) -> None:
    sample_ctx.one("backup", "--deploy-id", DEPLOY_ID)
    report = sample_ctx.one("mark-sample-saved", DEPLOY_ID)
    assert report == {"ok": True, "reason": "", "sample_saved": DEPLOY_ID}
    state = sample_ctx.store.load()
    assert state.sample_saved == DEPLOY_ID
    assert state.everyday == SAMPLE  # ほかの値は変えない
    # 次からは初回ではない
    assert sample_ctx.one("preflight", "--target", _target_json(TAG_A))["first_deploy"] is False


def test_サンプルのバックアップがなければ記録しない(sample_ctx: Ctx) -> None:
    report = sample_ctx.one("mark-sample-saved", DEPLOY_ID)
    assert report == {"ok": False, "reason": "no_sample_backup"}
    assert sample_ctx.store.load().sample_saved is None


def test_サンプルの記録_ロックが取られていれば中止(sample_ctx: Ctx) -> None:
    lock = DeployLock(sample_ctx.layout.lock_file)
    assert lock.try_acquire()
    try:
        report = sample_ctx.one("mark-sample-saved", DEPLOY_ID)
    finally:
        lock.release()
    assert report == {"ok": False, "reason": "lock_busy"}


# --- apply ---


def _apply_argv(target: Target, *extra: str) -> list[str]:
    return [
        "apply",
        "--target",
        _target_json(target),
        "--deploy-id",
        DEPLOY_ID,
        *extra,
        "--repo-url",
        URL,
    ]


def test_適用は_systemd_run_で切り離して始める(ctx: Ctx) -> None:
    ctx.fake.script(IS_ACTIVE_DEPLOY, returncode=3, stdout="inactive\n")
    ctx.fake.script(("systemd-run",))
    report = ctx.one(*_apply_argv(TAG_A))
    assert report == {"ok": True, "reason": "", "started": True, "deploy_id": DEPLOY_ID}
    assert ctx.fake.argvs[-1] == (
        "systemd-run",
        "--user",
        "--unit=osekkai-deploy",
        "--collect",
        "--property=KillMode=process",
        f"--working-directory={ctx.layout.tool_dir}",
        "/usr/bin/python3",
        "-m",
        "rover_deploy.agent",
        "run-apply",
        "--target",
        _target_json(TAG_A),
        "--deploy-id",
        DEPLOY_ID,
        "--repo-url",
        URL,
    )
    assert ctx.fake.calls[-1].check is True


def test_適用_普段の版の印を渡す(ctx: Ctx) -> None:
    ctx.fake.script(IS_ACTIVE_DEPLOY, returncode=3, stdout="inactive\n")
    ctx.fake.script(("systemd-run",))
    ctx.one(*_apply_argv(TAG_A, "--everyday"))
    argv = ctx.fake.argvs[-1]
    assert argv[argv.index("--deploy-id") + 2 :] == ("--everyday", "--repo-url", URL)


def test_切り離した適用の側は同じ引数を受け取れる(ctx: Ctx) -> None:
    ctx.fake.script(IS_ACTIVE_DEPLOY, returncode=3, stdout="inactive\n")
    ctx.fake.script(("systemd-run",))
    ctx.one(*_apply_argv(BRANCH_B, "--everyday"))
    argv = ctx.fake.argvs[-1]
    inner = list(argv[argv.index("run-apply") :])
    report = ctx.one(*inner)
    assert report["ok"] is True
    assert ctx.transaction.calls == [(BRANCH_B, DEPLOY_ID, True, URL)]


@pytest.mark.parametrize("state", ["active", "activating", "deactivating"])
def test_適用_同じ名前のユニットが動いていれば断る(ctx: Ctx, state: str) -> None:
    ctx.fake.script(IS_ACTIVE_DEPLOY, stdout=f"{state}\n")
    report = ctx.one(*_apply_argv(TAG_A))
    assert report == {"ok": False, "reason": "apply_running"}
    assert all(argv[0] != "systemd-run" for argv in ctx.fake.argvs)


def test_適用_ユニットが動いているか分からなければ断る(ctx: Ctx) -> None:
    ctx.fake.raise_on(IS_ACTIVE_DEPLOY, UnexpectedOutput("systemctl"))
    report = ctx.one(*_apply_argv(TAG_A))
    assert report == {"ok": False, "reason": "apply_unknown"}
    assert all(argv[0] != "systemd-run" for argv in ctx.fake.argvs)


def test_適用_ロックが取られていれば断る(ctx: Ctx) -> None:
    ctx.fake.script(IS_ACTIVE_DEPLOY, returncode=3, stdout="inactive\n")
    lock = DeployLock(ctx.layout.lock_file)
    assert lock.try_acquire()
    try:
        report = ctx.one(*_apply_argv(TAG_A))
    finally:
        lock.release()
    assert report == {"ok": False, "reason": "lock_busy"}
    assert all(argv[0] != "systemd-run" for argv in ctx.fake.argvs)


def test_適用_始められなければ理由を返す(ctx: Ctx) -> None:
    ctx.fake.script(IS_ACTIVE_DEPLOY, returncode=3, stdout="inactive\n")
    ctx.fake.script(("systemd-run",), returncode=1, stderr="ひみつ")
    code, lines = ctx.main(*_apply_argv(TAG_A))
    assert code == 0
    assert lines == [{"ok": False, "reason": "start_failed"}]


def test_適用_リポジトリの_URL_がなければ使い方の誤り(ctx: Ctx) -> None:
    code, lines = ctx.main("apply", "--target", _target_json(TAG_A), "--deploy-id", DEPLOY_ID)
    assert code == 2
    assert ctx.fake.argvs == []


# --- run-apply ---


def _run_apply_argv(target: Target, *extra: str) -> list[str]:
    return ["run-apply", *_apply_argv(target, *extra)[1:]]


def test_切り離された適用はロックを持って流れを行う(ctx: Ctx) -> None:
    report = ctx.one(*_run_apply_argv(TAG_A))
    assert report == {
        "ok": True,
        "reason": "",
        "result": result_to_dict(DeployResult(DEPLOY_ID, "succeeded", TAG_A, ())),
    }
    assert ctx.urls == [URL]
    assert ctx.transaction.calls == [(TAG_A, DEPLOY_ID, False, URL)]
    assert ctx.transaction.lock_held == [True]
    lock = DeployLock(ctx.layout.lock_file)
    assert lock.try_acquire()  # 終わったら外す
    lock.release()


def test_切り離された適用_ロックが取られていれば結果の行を書いて終える(ctx: Ctx) -> None:
    lock = DeployLock(ctx.layout.lock_file)
    assert lock.try_acquire()
    try:
        report = ctx.one(*_run_apply_argv(TAG_A))
    finally:
        lock.release()
    assert report == {"ok": False, "reason": "lock_busy"}
    assert ctx.transaction.calls == []
    result = read_log_result(ctx.layout.log_dir, DEPLOY_ID)
    assert result == DeployResult(DEPLOY_ID, "aborted", None, ("lock_busy",))


def test_切り離された適用_思わぬ失敗は型の名前だけを記録して終える(ctx: Ctx) -> None:
    ctx.transaction.result = RuntimeError("ひみつの文言")
    code, lines = ctx.main(*_run_apply_argv(TAG_A))
    assert code == 1
    assert lines == [{"ok": False, "reason": "unexpected", "error": "RuntimeError"}]
    text = (ctx.layout.log_dir / f"{DEPLOY_ID}.jsonl").read_text(encoding="utf-8")
    assert "ひみつ" not in text
    assert "RuntimeError" in text
    assert read_log_result(ctx.layout.log_dir, DEPLOY_ID) is None
    lock = DeployLock(ctx.layout.lock_file)
    assert lock.try_acquire()
    lock.release()


# --- follow ---


def _log(ctx: Ctx) -> DeployLog:
    return DeployLog(ctx.layout.log_dir, DEPLOY_ID, clock=lambda: NOW)


def test_追跡は結果の行で終わる(ctx: Ctx) -> None:
    ctx.fake.script(IS_ACTIVE_DEPLOY, stdout="active\n")
    result = DeployResult(DEPLOY_ID, "rolled_back", SAMPLE, ("alive",))
    log = _log(ctx)
    log.event("prepare", "start")
    ctx.clock.on_sleep = [
        lambda: log.event("prepare", "ok"),
        lambda: None,
        lambda: log.result(result),
    ]
    code, lines = ctx.main("follow", DEPLOY_ID)
    assert code == 0
    assert lines == [
        {"t": NOW, "step": "prepare", "status": "start", "reason": ""},
        {"t": NOW, "step": "prepare", "status": "ok", "reason": ""},
        {"t": NOW, "result": result_to_dict(result)},
    ]
    assert ctx.clock.sleeps == 3


def test_追跡_終わった記録はすぐに全部流す(ctx: Ctx) -> None:
    log = _log(ctx)
    log.event("prepare", "ok")
    log.result(DeployResult(DEPLOY_ID, "succeeded", TAG_A, ()))
    code, lines = ctx.main("follow", DEPLOY_ID)
    assert code == 0
    assert len(lines) == 2
    assert "result" in lines[-1]
    assert ctx.clock.sleeps == 0
    assert ctx.fake.argvs == []


def test_追跡_記録ができるのを待つ(ctx: Ctx) -> None:
    ctx.fake.script(IS_ACTIVE_DEPLOY, stdout="active\n")
    result = DeployResult(DEPLOY_ID, "succeeded", TAG_A, ())

    def write() -> None:
        _log(ctx).result(result)

    ctx.clock.on_sleep = [lambda: None, write]
    code, lines = ctx.main("follow", DEPLOY_ID)
    assert lines == [{"t": NOW, "result": result_to_dict(result)}]


def test_追跡_記録ができなければ_no_log(ctx: Ctx) -> None:
    code, lines = ctx.main("follow", DEPLOY_ID, "--start-timeout", "5")
    assert code == 0
    assert lines == [{"error": "no_log"}]
    assert ctx.clock.t == 5.0


def test_追跡_結果の行が壊れていれば_broken_result(ctx: Ctx) -> None:
    log = _log(ctx)
    log.event("prepare", "ok")
    with (ctx.layout.log_dir / f"{DEPLOY_ID}.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"t": 1, "result": {"outcome": "nope"}}\n')
    code, lines = ctx.main("follow", DEPLOY_ID)
    assert lines[-1] == {"error": "broken_result"}
    assert len(lines) == 2


def test_追跡_適用が結果を書かずに終われば知らせる(ctx: Ctx) -> None:
    ctx.fake.script(IS_ACTIVE_DEPLOY, stdout="active\n")
    ctx.fake.script(IS_ACTIVE_DEPLOY, returncode=3, stdout="inactive\n")
    log = _log(ctx)
    log.event("prepare", "start")
    code, lines = ctx.main("follow", DEPLOY_ID)
    assert code == 0
    assert lines == [
        {"t": NOW, "step": "prepare", "status": "start", "reason": ""},
        {"error": "ended_without_result"},
    ]


def test_追跡_終わる直前の行を読み落とさない(ctx: Ctx) -> None:
    result = DeployResult(DEPLOY_ID, "succeeded", TAG_A, ())
    log = _log(ctx)
    log.event("prepare", "start")

    class Racing(FakeRunner):
        def __call__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            log.result(result)  # 調べている間に結果を書いて終わる
            return super().__call__(argv, **kwargs)

    racing = Racing()
    racing.script(IS_ACTIVE_DEPLOY, returncode=3, stdout="inactive\n")
    ctx.context.run = racing
    code, lines = ctx.main("follow", DEPLOY_ID)
    assert lines[-1] == {"t": NOW, "result": result_to_dict(result)}


def test_追跡_ユニットが分からなくても続ける(ctx: Ctx) -> None:
    ctx.fake.raise_on(IS_ACTIVE_DEPLOY, UnexpectedOutput("systemctl"))
    log = _log(ctx)
    log.event("prepare", "start")
    ctx.clock.on_sleep = [lambda: log.result(DeployResult(DEPLOY_ID, "succeeded", TAG_A, ()))]
    code, lines = ctx.main("follow", DEPLOY_ID)
    assert "result" in lines[-1]


def test_追跡_時間切れ(ctx: Ctx) -> None:
    ctx.fake.script(IS_ACTIVE_DEPLOY, stdout="active\n")
    _log(ctx).event("prepare", "start")
    code, lines = ctx.main("follow", DEPLOY_ID, "--timeout", "3")
    assert lines[-1] == {"error": "timeout"}
    assert ctx.clock.t == 3.0


# --- 入口 ---


def test_思わぬ例外は型の名前だけを返す(ctx: Ctx) -> None:
    def broken(run: object, layout: object) -> SafetyVerdict:
        raise KeyError("ひみつ")

    ctx.context.check_safety = broken
    code, lines = ctx.main("preflight", "--target", _target_json(TAG_A))
    assert code == 1
    assert lines == [{"ok": False, "reason": "unexpected", "error": "KeyError"}]


def test_既定の置き場所は環境変数から作る(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    context = agent.default_context()
    assert context.layout.home == tmp_path
    assert context.layout.state_dir == tmp_path / "state/osekkai-deploy"
    assert context.env["XDG_STATE_HOME"] == str(tmp_path / "state")
