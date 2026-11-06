"""初期設定の手順のテスト（8.1〜8.8）。偽の Runner と一時のディレクトリで、実機には触れない。"""

import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import pytest
from fake_runner import FakeRunner

from rover_deploy.common import Target
from rover_deploy.layout import CORE_UNIT_NAME, SAMPLE_UNIT_NAME, Layout
from rover_deploy.provision import (
    APT_PACKAGES,
    ASSETS,
    BOOT_LINES,
    PROVISION_STEPS,
    REMINDER_TIMER,
    REMINDER_UNITS,
    SUPPORTED_OS_TEXT,
    Asset,
    ProvisionContext,
    ProvisionReport,
    Step,
    StepFailed,
    check_os,
    provision_steps,
    run_provision,
    secrets_guidance,
)
from rover_deploy.releases import PrepareFailed, Releases
from rover_deploy.system import CommandFailed

USER = "pi-user"
SHA = "c" * 40
URL = "https://example.invalid/owner/repo.git"
SECRET = "ひみつの値"
NOW = 1_800_000_000.0

TRIXIE = 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nVERSION_CODENAME=trixie\nID=debian\n'
BOOKWORM = "VERSION_CODENAME=bookworm\nID=debian\n"
BULLSEYE = "VERSION_CODENAME=bullseye\nID=debian\n"

ALL_GROUPS = "pi-user adm gpio i2c audio spi\n"


def target() -> Target:
    return Target(kind="release", ref="v0.2.0", ref_kind="tag", sha=SHA)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_release(layout: Layout, sha: str = SHA) -> Path:
    """版のディレクトリの形だけを作る（worktree の印・ユニットのファイル・仮想環境の python）。"""
    directory = layout.release_dir(sha)
    write(directory / ".git", "gitdir: x\n")
    write(directory / "deploy/systemd" / CORE_UNIT_NAME, "[Unit]\nDescription=core\n")
    for name in REMINDER_UNITS:
        write(directory / "deploy/systemd" / name, f"# {name}\n")
    write(directory / ".venv/bin/python", "")
    return directory


class FakeReleases(Releases):
    """準備だけを偽物にする。切り替えとユニットの写しは本物を使う。"""

    def __init__(self, layout: Layout, run: FakeRunner) -> None:
        super().__init__(layout, run)
        self._layout_ = layout
        self.prepared: list[tuple[Target, str]] = []

    def prepare(self, target: Target, repo_url: str) -> Path:
        self.prepared.append((target, repo_url))
        return make_release(self._layout_, target.sha or SHA)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


VOICE_DATA = b"voice-data"
MODEL_DATA = b"model-data"
TEST_ASSETS = (
    Asset("voices", "a.htsvoice", "https://example.invalid/a.htsvoice", sha(VOICE_DATA)),
    Asset("models", "m.onnx", "https://example.invalid/m.onnx", sha(MODEL_DATA)),
)


class FakeFetch:
    """URL ごとに決めた中身を書く偽の取得。呼ばれた URL を記録する。"""

    def __init__(self) -> None:
        self.contents: dict[str, bytes] = {}
        self.errors: dict[str, BaseException] = {}
        self.urls: list[str] = []
        self.timeouts: list[float] = []

    def __call__(self, url: str, out: BinaryIO, timeout: float) -> None:
        self.urls.append(url)
        self.timeouts.append(timeout)
        if url in self.errors:
            out.write(b"half")  # 途中まで書いてから失敗する
            raise self.errors[url]
        out.write(self.contents[url])


class Env:
    """1 つのテストの置き場所と偽物の一式。"""

    def __init__(self, tmp_path: Path, fake: FakeRunner) -> None:
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.layout = Layout({}, home=self.home, uid=1000)
        self.fake = fake
        self.os_release = write(tmp_path / "etc/os-release", TRIXIE)
        self.boot_config = write(
            tmp_path / "boot/config.txt",
            "# 元の設定\ndtparam=i2c_arm=on\ndtoverlay=hifiberry-dac\n",
        )
        self.data_dir = self.home / ".local/share/osekkai"
        self.releases = FakeReleases(self.layout, fake)
        self.said: list[tuple[str, str]] = []
        self.target: Target | None = target()
        self.fetch = FakeFetch()
        self.fetch.contents = {
            TEST_ASSETS[0].url: VOICE_DATA,
            TEST_ASSETS[1].url: MODEL_DATA,
        }

    def context(self) -> ProvisionContext:
        return ProvisionContext(
            run=self.fake,
            layout=self.layout,
            releases=self.releases,
            user=USER,
            repo_url=URL,
            target=self.target,
            data_dir=self.data_dir,
            say=lambda name, text: self.said.append((name, text)),
            fetch=self.fetch,
            assets=TEST_ASSETS,
            os_release=self.os_release,
            boot_config=self.boot_config,
            now=lambda: NOW,
        )

    def steps(self) -> dict[str, Step]:
        return {step.name: step for step in provision_steps(self.context())}

    # --- 済んでいる状態を作る ---

    def script_os(self, machine: str = "aarch64") -> None:
        self.fake.script(["uname", "-m"], stdout=f"{machine}\n")

    def script_apt_installed(self, missing: tuple[str, ...] = ()) -> None:
        for name in APT_PACKAGES:
            if name in missing:
                self.fake.script(
                    ["dpkg-query", "-W", "-f=${Status}", name],
                    returncode=1,
                )
            else:
                self.fake.script(
                    ["dpkg-query", "-W", "-f=${Status}", name], stdout="install ok installed"
                )

    def script_groups(self, text: str = ALL_GROUPS) -> None:
        self.fake.script(["id", "-nG", USER], stdout=text)

    def script_linger(self, value: str = "yes") -> None:
        self.fake.script(["loginctl", "show-user", USER, "-p", "Linger", "--value"], stdout=value)

    def make_layout_dirs(self) -> None:
        for path in (self.layout.releases_dir, self.layout.tool_dir, self.layout.state_dir):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        os.chmod(self.layout.root, 0o700)

    def make_assets(self) -> None:
        write(self.data_dir / "voices/a.htsvoice", VOICE_DATA.decode())
        write(self.data_dir / "models/m.onnx", MODEL_DATA.decode())

    def make_current(self) -> Path:
        directory = make_release(self.layout)
        self.layout.current_link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(directory, self.layout.current_link)
        write(self.layout.core_unit_file, "[Unit]\nDescription=core\n")
        return directory

    def install_reminder(self, current: Path) -> None:
        for name in REMINDER_UNITS:
            write(
                self.layout.user_unit_dir / name,
                (current / "deploy/systemd" / name).read_text(encoding="utf-8"),
            )

    def script_reminder_enabled(self, enabled: str = "enabled", active: str = "active") -> None:
        self.fake.script(["systemctl", "--user", "is-enabled", REMINDER_TIMER], stdout=enabled)
        self.fake.script(["systemctl", "--user", "is-active", REMINDER_TIMER], stdout=active)

    def script_running(self, core: str = "inactive", sample: str = "inactive") -> None:
        self.fake.script(["systemctl", "--user", "is-active", CORE_UNIT_NAME], stdout=core)
        self.fake.script(["systemctl", "--user", "is-active", SAMPLE_UNIT_NAME], stdout=sample)

    def script_self_check(self, current: Path, *, failed: tuple[str, ...] = ()) -> None:
        results = [{"name": "config", "status": "ok", "reason": ""}]
        results += [{"name": n, "status": "failed", "reason": "x"} for n in failed]
        out = json.dumps({"schema": 1, "ok": not failed, "results": results})
        self.fake.script(
            [str(current / ".venv/bin/python"), "-m", "osekkai", "--self-check"],
            returncode=1 if failed else 0,
            stdout=out,
        )

    def all_done(self) -> Path:
        """済んでいる実機（2 回目の初期設定）の状態を作る。"""
        self.script_os()
        self.script_apt_installed()
        self.script_groups()
        self.script_linger()
        self.make_layout_dirs()
        self.make_assets()
        current = self.make_current()
        self.install_reminder(current)
        self.script_reminder_enabled()
        write(self.layout.core_config_dir / "env", f"KEY={SECRET}\n")
        return current


@pytest.fixture
def env(tmp_path: Path, fake_runner: FakeRunner) -> Env:
    return Env(tmp_path, fake_runner)


def collect() -> tuple[list[tuple[str, str]], Callable[[str, str], None]]:
    lines: list[tuple[str, str]] = []

    def log(name: str, text: str) -> None:
        lines.append((name, text))

    return lines, log


def joined(lines: list[tuple[str, str]]) -> str:
    return "\n".join(f"{name} {text}" for name, text in lines)


def no_change_commands(fake: FakeRunner) -> None:
    """変える側のコマンド（sudo・enable・start）を呼んでいない。"""
    for argv in fake.argvs:
        assert argv[0] != "sudo"
        assert "enable" not in argv and "start" not in argv


# --- run_provision（8.2〜8.4） ---


def step(
    name: str,
    calls: list[str],
    *,
    done: bool = False,
    reboot: bool = False,
    error: BaseException | None = None,
    hint: str = "",
) -> Step:
    def is_done() -> bool:
        calls.append(f"{name}?")
        return done

    def apply() -> None:
        calls.append(name)
        if error is not None:
            raise error

    return Step(name, is_done, apply, needs_reboot=reboot, retry_hint=hint)


def test_run_provision_skips_done_steps_and_applies_the_rest() -> None:
    calls: list[str] = []
    lines, log = collect()
    report = run_provision([step("a", calls, done=True), step("b", calls)], log)
    assert calls == ["a?", "b?", "b"]
    assert report == ProvisionReport(
        ok=True,
        skipped=("a",),
        applied=("b",),
        failed_step=None,
        retry_hint="",
        needs_reboot=False,
        self_check_ok=None,
    )
    assert ("a", "済み") in lines
    assert not any("再起動" in text for _, text in lines)


def test_run_provision_stops_at_first_failure_with_hint() -> None:
    calls: list[str] = []
    lines, log = collect()
    steps = [
        step("a", calls),
        step("b", calls, error=StepFailed("固定の理由"), hint="b をやり直します"),
        step("c", calls),
    ]
    report = run_provision(steps, log)
    assert calls == ["a?", "a", "b?", "b"]  # c は確かめもしない
    assert not report.ok
    assert report.failed_step == "b"
    assert report.retry_hint == "b をやり直します"
    assert report.applied == ("a",)
    text = joined(lines)
    assert "固定の理由" in text
    assert "b をやり直します" in text


def test_run_provision_shows_only_exception_type_for_other_errors() -> None:
    calls: list[str] = []
    lines, log = collect()

    class Leaky(Exception):
        pass

    steps = [step("a", calls, error=Leaky(SECRET), hint="もう一度")]
    report = run_provision(steps, log)
    assert report.failed_step == "a"
    text = joined(lines)
    assert "Leaky" in text
    assert SECRET not in text


def test_run_provision_failure_in_is_done_stops_too() -> None:
    lines, log = collect()

    def broken() -> bool:
        raise CommandFailed("dpkg-query", 2)

    applied: list[str] = []
    steps = [
        Step("a", broken, lambda: applied.append("a"), retry_hint="h"),
        Step("b", lambda: False, lambda: applied.append("b")),
    ]
    report = run_provision(steps, log)
    assert report.failed_step == "a"
    assert applied == []


def test_run_provision_tells_reboot_at_the_end_only_when_applied() -> None:
    calls: list[str] = []
    lines, log = collect()
    report = run_provision(
        [step("a", calls, reboot=True), step("b", calls, done=True, reboot=True)],
        log,
    )
    assert report.needs_reboot
    assert "再起動" in lines[-1][1]

    calls.clear()
    lines2, log2 = collect()
    report2 = run_provision([step("b", calls, done=True, reboot=True)], log2)
    assert not report2.needs_reboot
    assert not any("再起動" in text for _, text in lines2)


def test_run_provision_tells_reboot_even_when_a_later_step_failed() -> None:
    calls: list[str] = []
    lines, log = collect()
    report = run_provision(
        [step("a", calls, reboot=True), step("b", calls, error=StepFailed("x"))],
        log,
    )
    assert not report.ok
    assert report.needs_reboot
    assert any("再起動" in text for _, text in lines)


# --- 手順の並び（8.1） ---


def test_steps_follow_the_design_order(env: Env) -> None:
    names = [s.name for s in provision_steps(env.context())]
    assert names == list(PROVISION_STEPS)
    assert names[0] == "os"
    assert names[-1] == "self_check"
    reboot = {s.name for s in provision_steps(env.context()) if s.needs_reboot}
    assert reboot == {"boot_config", "groups"}
    assert all(s.retry_hint for s in provision_steps(env.context()))


def test_apt_packages_cover_design_list() -> None:
    for name in ("git", "python3-venv", "open-jtalk", "open-jtalk-mecab-naist-jdic", "i2c-tools"):
        assert name in APT_PACKAGES


# --- os（8.7） ---


@pytest.mark.parametrize("text", [TRIXIE, BOOKWORM])
def test_check_os_accepts_supported(env: Env, text: str) -> None:
    write(env.os_release, text)
    env.script_os()
    assert check_os(env.context())


@pytest.mark.parametrize(("text", "machine"), [(BULLSEYE, "aarch64"), (TRIXIE, "armv7l")])
def test_unsupported_os_aborts_before_any_change(
    env: Env, text: str, machine: str, tmp_path: Path
) -> None:
    write(env.os_release, text)
    env.script_os(machine)
    before = env.boot_config.read_text(encoding="utf-8")
    lines, log = collect()
    report = run_provision(provision_steps(env.context()), log)
    assert report.failed_step == "os"
    assert report.applied == ()
    assert set(env.fake.argvs) <= {("uname", "-m")}  # 確かめたのは機械の種類だけ
    assert env.boot_config.read_text(encoding="utf-8") == before
    assert not env.layout.root.exists()
    assert SUPPORTED_OS_TEXT in joined(lines)
    assert "trixie" in SUPPORTED_OS_TEXT and "bookworm" in SUPPORTED_OS_TEXT


def test_missing_os_release_is_unsupported(env: Env) -> None:
    env.os_release.unlink()
    env.script_os()
    assert not check_os(env.context())


# --- apt ---


def test_apt_installs_only_missing(env: Env) -> None:
    env.script_apt_installed(missing=("open-jtalk", "open-jtalk-mecab-naist-jdic"))
    env.fake.script(["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get"])
    s = env.steps()["apt"]
    assert not s.is_done()
    s.apply()
    apt = [a for a in env.fake.argvs if a[0] == "sudo"]
    assert apt == [
        ("sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "update"),
        (
            "sudo",
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "apt-get",
            "install",
            "-y",
            "open-jtalk",
            "open-jtalk-mecab-naist-jdic",
        ),
    ]


def test_apt_done_when_all_installed(env: Env) -> None:
    env.script_apt_installed()
    assert env.steps()["apt"].is_done()
    no_change_commands(env.fake)


def test_apt_half_installed_counts_as_missing(env: Env) -> None:
    env.fake.script(["dpkg-query", "-W", "-f=${Status}", "git"], stdout="deinstall ok config-files")
    env.script_apt_installed(missing=("git",))  # git は上の結果が先に返る
    assert not env.steps()["apt"].is_done()


# --- boot_config ---


def test_boot_config_done_when_lines_present(env: Env) -> None:
    assert env.steps()["boot_config"].is_done()


def test_boot_config_appends_missing_line_after_backup(env: Env) -> None:
    write(env.boot_config, "# 元の設定\ndtparam=i2c_arm=on\n[cm4]\notg_mode=1\n")
    env.fake.script(["sudo", "cp"])
    env.fake.script(["sudo", "tee"])
    s = env.steps()["boot_config"]
    assert not s.is_done()
    s.apply()
    cp, tee = env.fake.calls
    assert cp.argv[:3] == ("sudo", "cp", "-p")
    assert cp.argv[3] == str(env.boot_config)
    assert cp.argv[4].startswith(str(env.boot_config) + ".osekkai-bak-")
    assert tee.argv == ("sudo", "tee", "-a", str(env.boot_config))
    assert tee.input is not None
    added = tee.input.decode("utf-8")
    assert "dtoverlay=hifiberry-dac" in added
    assert "dtparam=i2c_arm=on" not in added  # あるものは足さない
    assert "[all]" in added  # 条件の節の中に入らないようにする
    assert s.needs_reboot


@pytest.mark.parametrize(
    ("text", "done"),
    [
        # [pi4] の下にしかない行は、ほかの機種で効かないので、ないとする
        ("dtparam=i2c_arm=on\n[pi4]\ndtoverlay=hifiberry-dac\n", False),
        # [all] の後なら効く
        ("[pi4]\narm_boost=1\n[all]\ndtparam=i2c_arm=on\ndtoverlay=hifiberry-dac\n", True),
        # 行の後ろの注釈はあってよい
        ("dtparam=i2c_arm=on # I2C\ndtoverlay=hifiberry-dac\n", True),
        # 注釈にした行は、ないとする
        ("#dtparam=i2c_arm=on\ndtoverlay=hifiberry-dac\n", False),
        ("# dtparam=i2c_arm=on\ndtoverlay=hifiberry-dac\n", False),
        # [all] の後でも、次の条件の節に入ったら効かない
        ("[all]\ndtparam=i2c_arm=on\n[cm4]\ndtoverlay=hifiberry-dac\n", False),
    ],
)
def test_boot_config_counts_only_active_lines(env: Env, text: str, done: bool) -> None:
    write(env.boot_config, text)
    assert env.steps()["boot_config"].is_done() is done


def test_boot_config_appends_line_found_only_in_conditional_section(env: Env) -> None:
    write(env.boot_config, "dtparam=i2c_arm=on\n[pi4]\ndtoverlay=hifiberry-dac\n")
    env.fake.script(["sudo", "cp"])
    env.fake.script(["sudo", "tee"])
    env.steps()["boot_config"].apply()
    tee = env.fake.calls[-1]
    assert tee.input == b"\n[all]\ndtoverlay=hifiberry-dac\n"


def test_boot_config_missing_file_fails(env: Env) -> None:
    env.boot_config.unlink()
    s = env.steps()["boot_config"]
    assert not s.is_done()
    with pytest.raises(StepFailed):
        s.apply()
    assert env.fake.argvs == []


def test_boot_lines_are_the_design_lines() -> None:
    assert BOOT_LINES == ("dtparam=i2c_arm=on", "dtoverlay=hifiberry-dac")


# --- groups ---


def test_groups_done_with_adm_for_journal(env: Env) -> None:
    env.script_groups()
    assert env.steps()["groups"].is_done()


def test_groups_adds_missing_and_journal_group(env: Env) -> None:
    env.script_groups("pi-user gpio audio\n")
    env.fake.script(["sudo", "usermod"])
    s = env.steps()["groups"]
    assert not s.is_done()
    s.apply()
    assert env.fake.argvs[-1] == ("sudo", "usermod", "-aG", "i2c,systemd-journal", USER)
    assert s.needs_reboot


# --- linger ---


def test_linger_enabled_when_no(env: Env) -> None:
    env.script_linger("no\n")
    env.fake.script(["sudo", "loginctl", "enable-linger", USER])
    s = env.steps()["linger"]
    assert not s.is_done()
    s.apply()
    assert env.fake.argvs[-1] == ("sudo", "loginctl", "enable-linger", USER)


def test_linger_done_when_yes(env: Env) -> None:
    env.script_linger("yes\n")
    assert env.steps()["linger"].is_done()


# --- layout ---


def test_layout_creates_private_dirs(env: Env) -> None:
    s = env.steps()["layout"]
    assert not s.is_done()
    s.apply()
    for path in (env.layout.releases_dir, env.layout.tool_dir, env.layout.state_dir):
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert s.is_done()


def test_layout_keeps_existing_contents(env: Env) -> None:
    keep = write(env.layout.state_dir / "state.json", "{}")
    s = env.steps()["layout"]
    s.apply()
    assert keep.read_text(encoding="utf-8") == "{}"


# --- voices・models（取得元と sha256 を固定。8.5: 上書きしない） ---


def test_real_assets_are_pinned() -> None:
    names = {(a.dir_name, a.file_name) for a in ASSETS}
    assert ("voices", "tohoku-f01-happy.htsvoice") in names  # 基盤の既定の声
    assert ("models", "face_detection_yunet_2023mar.onnx") in names
    assert ("models", "face_recognition_sface_2021dec.onnx") in names
    assert len(names) == len(ASSETS) == 7
    for asset in ASSETS:
        assert asset.dir_name in ("voices", "models")
        assert asset.url.startswith("https://")
        assert asset.url.endswith("/" + asset.file_name)
        assert len(asset.sha256) == 64
        assert all(c in "0123456789abcdef" for c in asset.sha256)


def test_assets_done_when_present_and_fetch_nothing(env: Env) -> None:
    env.make_assets()
    steps = env.steps()
    assert steps["voices"].is_done()
    assert steps["models"].is_done()
    assert env.fetch.urls == []
    assert env.said == []


@pytest.mark.parametrize(("name", "index"), [("voices", 0), ("models", 1)])
def test_missing_asset_is_fetched_and_checked(env: Env, name: str, index: int) -> None:
    asset = TEST_ASSETS[index]
    s = env.steps()[name]
    assert not s.is_done()
    s.apply()
    assert env.fetch.urls == [asset.url]
    assert env.fetch.timeouts[0] > 0
    path = env.data_dir / name / asset.file_name
    assert sha(path.read_bytes()) == asset.sha256
    assert os.listdir(path.parent) == [asset.file_name]  # 一時のファイルは残さない
    assert s.is_done()


def test_wrong_download_is_rejected_and_leaves_no_file(env: Env) -> None:
    env.fetch.contents[TEST_ASSETS[0].url] = b"tampered"
    s = env.steps()["voices"]
    with pytest.raises(StepFailed) as info:
        s.apply()
    assert "a.htsvoice" in str(info.value)
    assert "example.invalid" not in str(info.value)  # URL は出さない
    directory = env.data_dir / "voices"
    assert not directory.exists() or os.listdir(directory) == []
    assert s.retry_hint
    assert not s.is_done()


def test_network_error_fails_and_leaves_no_file(env: Env) -> None:
    env.fetch.errors[TEST_ASSETS[1].url] = TimeoutError(SECRET)
    s = env.steps()["models"]
    with pytest.raises(StepFailed) as info:
        s.apply()
    assert SECRET not in str(info.value)
    assert "TimeoutError" in str(info.value)
    directory = env.data_dir / "models"
    assert not directory.exists() or os.listdir(directory) == []


def test_existing_file_with_other_sha_is_kept_with_warning(env: Env) -> None:
    path = write(env.data_dir / "voices/a.htsvoice", "personal")
    s = env.steps()["voices"]
    assert s.is_done()  # あるので済み。上書きしない
    assert path.read_text(encoding="utf-8") == "personal"
    assert env.fetch.urls == []
    warnings = [t for n, t in env.said if n == "voices"]
    assert len(warnings) == 1
    assert "a.htsvoice" in warnings[0]
    assert str(env.home) not in warnings[0]
    assert "example.invalid" not in warnings[0]


def test_only_missing_files_are_fetched(env: Env) -> None:
    assets = (
        TEST_ASSETS[0],
        Asset("voices", "b.htsvoice", "https://example.invalid/b.htsvoice", sha(b"b")),
    )
    env.fetch.contents[assets[1].url] = b"b"
    write(env.data_dir / "voices/a.htsvoice", VOICE_DATA.decode())
    ctx = env.context()
    ctx = ProvisionContext(**{**ctx.__dict__, "assets": assets})
    s = {x.name: x for x in provision_steps(ctx)}["voices"]
    assert not s.is_done()
    s.apply()
    assert env.fetch.urls == [assets[1].url]
    assert s.is_done()
    env.fetch.urls.clear()
    s2 = {x.name: x for x in provision_steps(ctx)}["voices"]
    assert s2.is_done()  # 2 回目は取らない
    assert env.fetch.urls == []


# --- release ---


def test_release_prepares_switches_and_installs_unit_without_starting(env: Env) -> None:
    env.fake.script(["systemctl", "--user", "daemon-reload"])
    s = env.steps()["release"]
    assert not s.is_done()
    s.apply()
    assert env.releases.prepared == [(target(), URL)]
    assert env.releases.current() == env.layout.release_dir(SHA)
    assert env.layout.core_unit_file.is_file()
    assert env.fake.argvs == [("systemctl", "--user", "daemon-reload")]  # 起動も有効化もしない
    assert s.is_done()


def test_release_done_when_current_exists(env: Env) -> None:
    env.make_current()
    assert env.steps()["release"].is_done()
    assert env.fake.argvs == []


def test_release_redone_when_current_has_no_core_unit(env: Env) -> None:
    """前の実行で、ユニットのない版に current が向いたまま止まっても、--ref の版でやり直せる。"""
    old = env.make_current()
    (old / "deploy/systemd" / CORE_UNIT_NAME).unlink()
    env.layout.core_unit_file.unlink()
    new_sha = "c" * 40
    env.target = Target(kind="release", ref="feat/x", ref_kind="branch", sha=new_sha)
    env.fake.script(["systemctl", "--user", "daemon-reload"])
    s = env.steps()["release"]
    assert not s.is_done()
    s.apply()
    assert env.releases.current() == env.layout.release_dir(new_sha)
    assert env.layout.core_unit_file.is_file()
    assert s.is_done()


def test_release_not_done_when_current_has_no_core_unit(env: Env) -> None:
    old = env.make_current()
    (old / "deploy/systemd" / CORE_UNIT_NAME).unlink()
    assert not env.steps()["release"].is_done()


def test_release_shows_fixed_message_when_prepare_fails(env: Env) -> None:
    def refuse(target: Target, repo_url: str) -> Path:
        raise PrepareFailed("unit")

    env.releases.prepare = refuse  # type: ignore[method-assign]
    s = env.steps()["release"]
    with pytest.raises(StepFailed, match="新しい基盤のユニットがありません"):
        s.apply()
    assert env.releases.current() is None


def test_release_without_target_fails(env: Env) -> None:
    env.target = None
    s = env.steps()["release"]
    with pytest.raises(StepFailed):
        s.apply()
    assert env.releases.prepared == []


# --- reminder ---


def test_reminder_copies_units_from_current_and_enables_timer(env: Env) -> None:
    current = env.make_current()
    env.script_reminder_enabled("disabled", "inactive")
    env.fake.script(["systemctl", "--user", "daemon-reload"])
    env.fake.script(["systemctl", "--user", "enable", REMINDER_TIMER])
    env.fake.script(["systemctl", "--user", "start", REMINDER_TIMER])
    s = env.steps()["reminder"]
    assert not s.is_done()
    s.apply()
    for name in REMINDER_UNITS:
        copied = (env.layout.user_unit_dir / name).read_text(encoding="utf-8")
        assert copied == (current / "deploy/systemd" / name).read_text(encoding="utf-8")
    assert env.fake.argvs[-3:] == [
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", REMINDER_TIMER),
        ("systemctl", "--user", "start", REMINDER_TIMER),
    ]
    # サービスのほうは起動しない
    assert all(CORE_UNIT_NAME not in argv for argv in env.fake.argvs)


def test_reminder_done_when_same_and_enabled(env: Env) -> None:
    current = env.make_current()
    env.install_reminder(current)
    env.script_reminder_enabled()
    assert env.steps()["reminder"].is_done()


def test_reminder_without_current_fails(env: Env) -> None:
    s = env.steps()["reminder"]
    assert not s.is_done()
    with pytest.raises(StepFailed):
        s.apply()


# --- secrets（8.5、8.6） ---


def test_secrets_never_created_and_place_shown(env: Env) -> None:
    s = env.steps()["secrets"]
    assert not s.is_done()
    s.apply()
    secrets = env.layout.core_config_dir / "env"
    assert not secrets.exists()
    assert not env.layout.core_config_dir.exists()
    text = "\n".join(t for _, t in env.said)
    assert "~/.config/osekkai/env" in text
    assert "0600" in text
    assert str(env.home) not in text
    assert any("~/.config/osekkai/env" in line for line in secrets_guidance(env.layout))


def test_existing_secrets_untouched(env: Env) -> None:
    secrets = write(env.layout.core_config_dir / "env", f"KEY={SECRET}\n")
    os.chmod(secrets, 0o600)
    before = secrets.stat()
    s = env.steps()["secrets"]
    assert s.is_done()
    after = secrets.stat()
    assert secrets.read_text(encoding="utf-8") == f"KEY={SECRET}\n"
    assert (before.st_mtime_ns, before.st_mode) == (after.st_mtime_ns, after.st_mode)
    assert all(SECRET not in t for _, t in env.said)


# --- self_check（8.8） ---


def test_self_check_uses_no_hardware_when_sample_running(env: Env) -> None:
    current = env.make_current()
    env.script_running(sample="active")
    env.script_self_check(current)
    s = env.steps()["self_check"]
    assert not s.is_done()
    s.apply()
    call = env.fake.calls[-1]
    assert call.argv == (
        str(current / ".venv/bin/python"),
        "-m",
        "osekkai",
        "--self-check",
        "--json",
        "--no-hardware",
    )
    assert call.cwd == current
    assert any("よい" in t for n, t in env.said if n == "self_check")
    no_change_commands(env.fake)


def test_self_check_with_hardware_when_sample_stopped(env: Env) -> None:
    current = env.make_current()
    env.script_running()
    env.script_self_check(current)
    env.steps()["self_check"].apply()
    assert "--no-hardware" not in env.fake.argvs[-1]


def test_self_check_unknown_running_is_treated_as_sample_running(env: Env) -> None:
    current = env.make_current()
    env.fake.script(["systemctl", "--user", "is-active"], stdout="")
    env.script_self_check(current)
    env.steps()["self_check"].apply()
    assert env.fake.argvs[-1][-1] == "--no-hardware"


def test_self_check_failures_are_shown_but_do_not_fail_the_step(env: Env) -> None:
    current = env.make_current()
    env.script_running()
    env.script_self_check(current, failed=("hat", "voice_synth"))
    lines, log = collect()
    report = run_provision([env.steps()["self_check"]], log)
    assert report.ok
    assert report.applied == ("self_check",)
    assert report.self_check_ok is False
    shown = "\n".join(t for n, t in env.said if n == "self_check")
    assert "hat" in shown and "voice_synth" in shown
    assert "x" not in shown.replace("hat", "").replace("voice_synth", "")  # 理由は出さない


def test_self_check_ok_is_in_report(env: Env) -> None:
    current = env.make_current()
    env.script_running()
    env.script_self_check(current)
    lines, log = collect()
    report = run_provision([env.steps()["self_check"]], log)
    assert report.self_check_ok is True


def test_self_check_not_run_is_none_in_report(env: Env) -> None:
    lines, log = collect()
    report = run_provision([env.steps()["self_check"]], log)  # 今の版がない
    assert not report.ok
    assert report.failed_step == "self_check"
    assert report.self_check_ok is None


def test_self_check_unreadable_output_fails(env: Env) -> None:
    current = env.make_current()
    env.script_running()
    env.fake.script(
        [str(current / ".venv/bin/python"), "-m", "osekkai", "--self-check"], stdout=SECRET
    )
    with pytest.raises(StepFailed) as info:
        env.steps()["self_check"].apply()
    assert SECRET not in str(info.value)


# --- 通しの流れ ---


def test_second_run_skips_everything_already_done(env: Env) -> None:
    current = env.all_done()
    env.script_running(sample="active")
    env.script_self_check(current)
    boot_before = env.boot_config.read_text(encoding="utf-8")
    lines, log = collect()
    report = run_provision(provision_steps(env.context()), log)
    assert report.ok
    assert report.applied == ("self_check",)
    assert set(report.skipped) == set(PROVISION_STEPS) - {"self_check"}
    assert not report.needs_reboot
    assert report.self_check_ok is True
    assert "rover-deploy self-check" not in joined(lines)
    no_change_commands(env.fake)
    assert env.boot_config.read_text(encoding="utf-8") == boot_before
    assert (env.layout.core_config_dir / "env").read_text(encoding="utf-8") == f"KEY={SECRET}\n"
    assert SECRET not in joined(lines)
    assert SECRET not in "\n".join(t for _, t in env.said)


def test_first_run_on_fresh_os_needs_reboot_and_never_reboots(env: Env) -> None:
    write(env.boot_config, "# 元の設定\n")
    env.script_os()
    env.script_apt_installed(missing=("open-jtalk",))
    env.fake.script(["sudo"])  # sudo のコマンドはすべて成功
    env.script_groups("pi-user\n")
    env.script_linger("no")
    env.make_assets()
    env.fake.script(["systemctl", "--user", "daemon-reload"])
    env.script_reminder_enabled("disabled", "inactive")
    env.fake.script(["systemctl", "--user", "enable", REMINDER_TIMER])
    env.fake.script(["systemctl", "--user", "start", REMINDER_TIMER])
    env.script_running()
    # 再起動の前なので、グループや起動の設定が効いておらず、ハードの項目がだめになる
    env.script_self_check(env.layout.release_dir(SHA), failed=("hat",))
    lines, log = collect()
    report = run_provision(provision_steps(env.context()), log)
    assert report.ok, joined(lines)
    assert report.needs_reboot
    assert report.self_check_ok is False
    assert any("hat" in t for n, t in env.said if n == "self_check")
    assert "再起動" in lines[-1][1]
    assert any("rover-deploy self-check" in t for _, t in lines[-2:])
    for argv in env.fake.argvs:
        assert "reboot" not in argv and "shutdown" not in argv
        if CORE_UNIT_NAME in argv:  # 基盤のサービスは、動いているかを聞くだけ
            assert argv == ("systemctl", "--user", "is-active", CORE_UNIT_NAME)
    assert not (env.layout.core_config_dir / "env").exists()
