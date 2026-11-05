"""開発 PC の道具（devcli）から実機の道具（agent）までを通しで動かす結合テスト。

要件（device-deployment）: 1.10、4.5、6.8、10.2。設計: Testing Strategy > Integration Tests。

本物の `Remote` に偽の ssh・scp をつなぎ、実機の道具を同じプロセスで呼ぶ
（tests/deploy_loopback.py）。
実機の側は一時ディレクトリの家と、ユニットの様子を持つ偽の Runner で動かす。
版の準備（git と pip）だけを偽物にし、切り替え・入れた後の確認・切り戻し・状態・記録・
バックアップ・知らせは本物の道具のコードで動かす。本物の ssh・systemd・ネットワークは使わない。
"""

from __future__ import annotations

import io
import json
import stat
import tarfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from deploy_loopback import HOST, Loopback, LoopbackDevice, loopback_remote, pushed_package_ok
from osekkai.core.notice import Notice
from rover_deploy import devcli
from rover_deploy.common import ExitCode, Target, version_label
from rover_deploy.devconfig import DevConfig, load_dev_config
from rover_deploy.layout import CORE_UNIT_NAME, SAMPLE_UNIT_NAME
from rover_deploy.notices import VERSION_NOTICE_S
from rover_deploy.refs import Resolution
from rover_deploy.state import (
    DeployState,
    StateStore,
    make_deploy_id,
    read_log_events,
    read_log_result,
)
from rover_deploy.system import Runner, RunResult, check_result

REPO_URL = "https://example.invalid/owner/osekkai-rover.git"
BRANCH = "feat/try-led"
BRANCH_SHA = "b" * 40
BRANCH_TARGET = Target(kind="release", ref=BRANCH, ref_kind="branch", sha=BRANCH_SHA)
BRANCH_LABEL = version_label(BRANCH_TARGET)  # "feat/try-led@bbbbbbb"
SAMPLE = Target(kind="sample", ref=None, ref_kind=None, sha=None)


class DevRunner:
    """開発 PC の Runner。バックアップの置き場所が作業コピーの外かの確かめだけに答える。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

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
        self.calls.append(args)
        if args[:2] == ("git", "-C") and args[3:] == ("rev-parse", "--is-inside-work-tree"):
            # git の作業コピーの外（git は 128 で終わる）
            return check_result(args, RunResult(128, b"", b""), check=check)
        raise AssertionError("開発 PC で台本にないコマンド")


@dataclass
class World:
    """開発 PC と実機をつないだ一式。"""

    root: Path
    device: LoopbackDevice
    loopback: Loopback
    dev_home: Path
    repo_root: Path
    out: io.StringIO = field(default_factory=io.StringIO)
    configs: list[DevConfig] = field(default_factory=list)

    def context(self) -> devcli.DevContext:
        dev_run = DevRunner()

        def load_config(env: Mapping[str, str], repo_root: Path, run: Runner) -> DevConfig:
            # 本物の設定の読み込み（作業コピーの外の確かめも本物）。家だけを一時の場所にする
            config = load_dev_config(env, repo_root, run, home=self.dev_home)
            self.configs.append(config)
            return config

        def resolve(name: str, repo_root: Path, run: Runner) -> Resolution:
            assert name == BRANCH
            return Resolution(BRANCH_TARGET, also_branch=False)

        def make_remote(host: str, run: Runner) -> devcli.RemotePort:
            assert host == HOST
            return loopback_remote(self.loopback)

        return devcli.DevContext(
            env={},
            repo_root=self.repo_root,
            run=dev_run,
            out=self.out,
            load_config=load_config,
            resolve=resolve,
            make_remote=make_remote,
            # 開発 PC と実機の時計をそろえる（入れ替えの識別子が実機の時刻と合う）
            now=self.device.clock.now,
        )

    def dev(self, *argv: str) -> tuple[int, list[str]]:
        """`rover-deploy <argv>` を動かし、終了コードと、この回に出た行を返す。"""
        start = len(self.out.getvalue())
        code = devcli.main(list(argv), context=self.context())
        return code, self.out.getvalue()[start:].splitlines()

    @property
    def backup_dir(self) -> Path:
        return self.dev_home / "rover-backups"

    def state(self) -> DeployState:
        return StateStore(self.device.layout).load()


@pytest.fixture
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    # scp に渡す実機の置き場所は ASCII だけを受け付けるので、名前に日本語を含まない一時の場所を使う
    root = tmp_path_factory.mktemp("e2e")
    dev_home = root / "dev-home"
    config = dev_home / ".config/osekkai-deploy/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'host = "{HOST}"\nbackup_dir = "~/rover-backups"\nrepo_url = "{REPO_URL}"\n',
        encoding="utf-8",
    )
    repo_root = root / "repo"
    repo_root.mkdir()
    device = LoopbackDevice(root)
    built = World(root, device, Loopback(device), dev_home, repo_root)
    yield built
    # 実機の側で台本にないコマンドが呼ばれていない
    assert device.unexpected == []


def _after(lines: list[str], *expected: str) -> None:
    """`expected` の行が、この順に出ている（あいだにほかの行があってよい）。"""
    position = 0
    for text in expected:
        assert text in lines[position:], f"{text!r} が {lines[position:]!r} にない"
        position = lines.index(text, position) + 1


def _no_device_details(world: World, lines: list[str]) -> None:
    """表示に、実機の置き場所・リポジトリの URL・接続先の設定を出さない（10.3）。"""
    text = "\n".join(lines)
    assert str(world.device.home) not in text
    assert REPO_URL not in text
    assert "device-run" not in text


def _steps(world: World, deploy_id: str) -> list[tuple[object, object]]:
    events = read_log_events(world.device.layout.log_dir, deploy_id)
    return [(e.get("step"), e.get("status")) for e in events if "result" not in e]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- 入れ替えの成功 ---


def test_branch_deploy_succeeds_and_shows_trial_on_lcd(world: World) -> None:
    device = world.device
    deploy_id = make_deploy_id(device.clock.now())

    code, lines = world.dev("deploy", BRANCH)

    assert code == ExitCode.OK
    dest = world.backup_dir / deploy_id
    backup = dest / f"backup-{deploy_id}.tar.gz"
    sample_backup = dest / f"sample-{deploy_id}.tar.gz"
    _after(
        lines,
        f"今の版: 止まっています / これからの版: {BRANCH_LABEL}",
        f"バックアップを受け取りました: {backup}（0.0 MB）",
        f"サンプルのバックアップを受け取りました: {sample_backup}（0.0 MB）",
        "サンプルのバックアップを受け取れたことを、実機に記録しました",
        f"入れ替えを始めました（識別子: {deploy_id}）",
        "  準備: よい",
        "  安全の確認: よい",
        "  切り替え: よい",
        "  起動: よい",
        "  確認: よい",
        "結果: 成功",
        f"動いている版: {BRANCH_LABEL}",
    )
    assert not any(line.startswith("失敗した確認") for line in lines)
    _no_device_details(world, lines)

    # 実機: 新しい基盤が試しの版で動き、サンプルは止まったまま。試しの版は有効にしない
    assert device.run_apply_codes == [0]
    assert device.running_units() == {CORE_UNIT_NAME}
    assert device.units[CORE_UNIT_NAME].enabled is False
    assert device.units[SAMPLE_UNIT_NAME].enabled is True
    release_dir = device.layout.release_dir(BRANCH_SHA)
    assert device.layout.current_link.resolve() == release_dir.resolve()
    assert device.layout.core_unit_file.is_file()

    state = world.state()
    assert state.current == BRANCH_TARGET
    assert state.everyday == SAMPLE
    assert state.previous == SAMPLE
    assert state.trial_since is not None
    assert state.sample_saved == deploy_id
    assert state.in_progress is None
    assert state.last_deploy is not None and state.last_deploy.outcome == "succeeded"

    # 記録: 準備から確認まで順に進み、結果の行は成功
    steps = _steps(world, deploy_id)
    for step in ("prepare", "recheck", "calibration", "switch", "start", "verify", "prune"):
        assert (step, "ok") in steps, step
    assert ("rollback", "start") not in steps
    result = read_log_result(device.layout.log_dir, deploy_id)
    assert result is not None
    assert (result.outcome, result.running, result.failed_checks) == (
        "succeeded",
        BRANCH_TARGET,
        (),
    )

    # LCD の知らせ: 道具が書いたものを、基盤の Notice がそのまま読める（7.1）
    notice = Notice.from_dict(json.loads(device.notice_file.read_text(encoding="utf-8")))
    # 1 行目は LCD の 16 セルに切り詰めた版の名前
    assert notice.rows == (BRANCH_LABEL[:16], "ためし ふだん:さんぷる")
    # 期限は切り替えのときから VERSION_NOTICE_S 秒（起動の後の確認より前に書く）
    assert notice.until is not None
    assert notice.until - VERSION_NOTICE_S <= state.trial_since

    # 開発 PC のバックアップ: 作業コピーの外の <置き場所>/<識別子>/ に、0700 と 0600 で（10.2、3.4）
    assert world.configs and world.configs[-1].backup_dir == world.backup_dir
    assert not world.backup_dir.is_relative_to(world.repo_root)
    assert sorted(p.name for p in dest.iterdir()) == sorted([backup.name, sample_backup.name])
    assert _mode(dest) == 0o700
    assert _mode(backup) == 0o600 and _mode(sample_backup) == 0o600
    with tarfile.open(sample_backup) as tar:
        names = tar.getnames()
    assert any(name.endswith("data/memo.txt") for name in names)
    assert world.loopback.fetched == [backup.name, sample_backup.name]

    # 道具のパッケージを実機に写してから呼んでいる
    assert world.loopback.pushed and pushed_package_ok(world.loopback.pushed[0])
    names_called = [args[0] for args in world.loopback.agent_calls]
    assert names_called == [
        "status",
        "preflight",
        "backup",
        "mark-sample-saved",
        "apply",
        "follow",
    ]


# --- 確認が失敗して切り戻す ---


def test_failed_verification_rolls_back_to_sample_with_exit_11(world: World) -> None:
    device = world.device
    device.self_check_fixture = "selfcheck_failed.json"  # core.lcd がだめ
    deploy_id = make_deploy_id(device.clock.now())

    code, lines = world.dev("deploy", BRANCH)

    assert code == ExitCode.ROLLED_BACK
    _after(
        lines,
        "  確認: 失敗",
        "  切り戻しの切り替え: よい",
        "  切り戻しの起動: よい",
        "  切り戻しの確認: よい",
        "  切り戻し: よい",
        "結果: 切り戻した（前の版に戻しました）",
        "動いている版: sample",
        "失敗した確認: self_check:core.lcd",
    )
    _no_device_details(world, lines)

    # 実機: サンプルに戻って動き、新しい基盤は止まっている。知らせと状態のファイルも残らない
    assert device.running_units() == {SAMPLE_UNIT_NAME}
    assert not device.notice_file.exists()
    assert not device.status_file.exists()

    state = world.state()
    assert state.current == SAMPLE
    assert state.in_progress is None
    assert state.last_deploy is not None and state.last_deploy.outcome == "rolled_back"
    result = read_log_result(device.layout.log_dir, deploy_id)
    assert result is not None
    assert result.outcome == "rolled_back"
    assert result.running == SAMPLE
    assert result.failed_checks == ("self_check:core.lcd",)
    assert ("verify", "failed") in _steps(world, deploy_id)


# --- 普段の版に戻す ---


def test_back_returns_to_sample_and_clears_reminder(world: World) -> None:
    device = world.device
    assert world.dev("deploy", BRANCH)[0] == ExitCode.OK

    # 4 時間たって、タイマーが戻し忘れを知らせる
    device.clock.advance(4 * 3600)
    code, text = device.agent(["remind"])
    assert code == 0 and json.loads(text)["reminded"] is True
    reminder = Notice.from_dict(json.loads(device.notice_file.read_text(encoding="utf-8")))
    assert reminder.rows == (None, "ためしのばん もどしてね")

    code, lines = world.dev("status")
    assert code == ExitCode.OK
    _after(
        lines,
        devcli.MSG_TRIAL_REMINDER,
        f"動いている版: {BRANCH_LABEL}",
        "普段の版: sample",
    )
    assert any(line.startswith("試しの版です（入れてから 4 時間") for line in lines)

    back_id = make_deploy_id(device.clock.now())
    code, lines = world.dev("back")

    assert code == ExitCode.OK
    _after(
        lines,
        f"今の版: {BRANCH_LABEL} / これからの版: sample",
        f"入れ替えを始めました（識別子: {back_id}）",
        "  切り替え: よい",
        "  起動: よい",
        "  確認: よい",
        "  自動の起動の設定: よい",
        "結果: 成功",
        "動いている版: sample",
    )
    assert not any("サンプルのバックアップ" in line for line in lines)
    _no_device_details(world, lines)

    # 実機: サンプルが動き、普段の版として有効。新しい基盤は止まって無効
    assert device.running_units() == {SAMPLE_UNIT_NAME}
    assert device.units[SAMPLE_UNIT_NAME].enabled is True
    assert device.units[CORE_UNIT_NAME].enabled is False
    assert not device.notice_file.exists()  # 戻し忘れの知らせも消える
    assert not device.status_file.exists()

    state = world.state()
    assert state.current == SAMPLE
    assert state.everyday == SAMPLE
    assert state.trial_since is None
    assert state.reminded_at is None
    assert state.last_deploy is not None and state.last_deploy.outcome == "succeeded"
    result = read_log_result(device.layout.log_dir, back_id)
    assert result is not None and result.running == SAMPLE

    # 2 回目はサンプルのバックアップを取らず、普段のバックアップだけを受け取る
    assert sorted(p.name for p in (world.backup_dir / back_id).iterdir()) == [
        f"backup-{back_id}.tar.gz"
    ]

    # もう普段の版で動いていれば、back は何もせずに 0
    calls = len(world.loopback.agent_calls)
    code, lines = world.dev("back")
    assert code == ExitCode.OK
    assert devcli.MSG_ALREADY_SAMPLE in lines
    assert [a[0] for a in world.loopback.agent_calls[calls:]] == ["status", "preflight"]


# --- 追跡の途中で接続が切れる ---


def test_disconnect_during_follow_exits_13_and_status_shows_result(world: World) -> None:
    device = world.device
    world.loopback.drop_stream_after = 2  # 記録の 2 行を流したところで ssh が切れる
    deploy_id = make_deploy_id(device.clock.now())

    code, lines = world.dev("deploy", BRANCH)

    assert code == ExitCode.UNKNOWN
    assert lines[-1] == devcli.MSG_DISCONNECTED
    assert "結果: 成功" not in lines
    _no_device_details(world, lines)

    # 実機の側は切れても最後まで進んでいる（4.5）
    assert device.run_apply_codes == [0]
    assert device.running_units() == {CORE_UNIT_NAME}

    world.loopback.drop_stream_after = None
    code, lines = world.dev("status")
    assert code == ExitCode.OK
    _after(lines, f"動いている版: {BRANCH_LABEL}", "普段の版: sample")
    assert any(line.startswith(f"最後の入れ替え: {deploy_id} 成功") for line in lines)
    assert not any("途中で終わっています" in line for line in lines)

    # 記録は rover-deploy log で後から読める
    code, lines = world.dev("log", deploy_id)
    assert code == ExitCode.OK
    _after(lines, f"入れ替え {deploy_id} の記録:", "  確認: よい", "結果: 成功")


# --- 事前の確認で中止する ---


def test_preflight_aborts_when_sample_is_running(tmp_path_factory: pytest.TempPathFactory) -> None:
    root = tmp_path_factory.mktemp("e2e-sample")
    dev_home = root / "dev-home"
    config = dev_home / ".config/osekkai-deploy/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'host = "{HOST}"\nbackup_dir = "~/rover-backups"\nrepo_url = "{REPO_URL}"\n',
        encoding="utf-8",
    )
    (root / "repo").mkdir()
    device = LoopbackDevice(root, sample_running=True)
    world = World(root, device, Loopback(device), dev_home, root / "repo")
    sample_pid = device.units[SAMPLE_UNIT_NAME].main_pid

    code, lines = world.dev("deploy", BRANCH)

    assert code == ExitCode.ABORTED
    _after(
        lines,
        f"今の版: sample / これからの版: {BRANCH_LABEL}",
        devcli.REASON_MESSAGES["sample_running"],
    )
    _no_device_details(world, lines)

    # 何も変えていない: 状態・記録・バックアップ・リンク・知らせがなく、サンプルは同じ pid で動く
    assert [a[0] for a in world.loopback.agent_calls] == ["status", "preflight"]
    assert world.loopback.fetched == []
    assert not world.backup_dir.exists()
    layout = device.layout
    assert not layout.state_file.exists()
    assert not layout.log_dir.exists()
    assert not layout.backups_dir.exists()
    assert not layout.current_link.is_symlink()
    assert not device.notice_file.exists()
    assert device.running_units() == {SAMPLE_UNIT_NAME}
    assert device.units[SAMPLE_UNIT_NAME].main_pid == sample_pid
    assert not any(args[0] == "systemd-run" for args in device.calls)
    assert device.unexpected == []
