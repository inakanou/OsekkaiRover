"""版のディレクトリの準備・切り替え・片付けのテスト（1.1、1.2、1.8、1.9、3.6、4.2、9.1）。"""

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from fake_runner import FakeRunner

from rover_deploy.common import Target
from rover_deploy.layout import Layout
from rover_deploy.releases import (
    FETCH_REFSPEC,
    KEEP_RELEASES,
    READY_MARKER,
    PrepareFailed,
    ReleaseError,
    Releases,
)
from rover_deploy.system import CommandTimedOut, RunResult, SubprocessRunner

SHA_A = "a" * 40
SHA_B = "b" * 40
URL = "https://example.invalid/owner/repo.git"
SECRET = "ひみつの出力"  # 外のコマンドの出力。例外の文言に出てはいけない


def tag(sha: str) -> Target:
    return Target(kind="release", ref="v0.2.0", ref_kind="tag", sha=sha)


def sha_of(n: int) -> str:
    """先頭 12 桁が違う sha（版のディレクトリの名前が重ならない）。"""
    return f"{n:012x}" + "0" * 28


class Sim:
    """偽の Runner に、git・venv の結果（ディレクトリができる・消える）を足したもの。

    呼び出しは FakeRunner に記録し、決めた失敗はそのまま起こす。成功したときだけ、結果を作る。
    """

    def __init__(self, fake: FakeRunner) -> None:
        self.fake = fake
        self.heads: dict[Path, str] = {}
        self.without_unit: set[str] = set()  # ユニットのファイルがない版（この道具より前の版）

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
        result = self.fake(args, input=input, timeout=timeout, check=check, cwd=cwd, env=env)
        if args[:3] == ("git", "init", "--bare"):
            repo = Path(args[-1])
            repo.mkdir(parents=True, exist_ok=True)
            (repo / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        elif args[:1] == ("git",) and args[3:6] == ("worktree", "add", "--detach"):
            directory, sha = Path(args[6]), args[7]
            directory.mkdir(parents=True)
            (directory / ".git").write_text("gitdir: x\n", encoding="utf-8")
            if sha not in self.without_unit:
                unit = directory / "deploy/systemd/osekkai.service"
                unit.parent.mkdir(parents=True)
                unit.write_text(f"unit {sha}\n", encoding="utf-8")
            self.heads[directory] = sha
        elif args[:1] == ("git",) and args[3:6] == ("worktree", "remove", "--force"):
            shutil.rmtree(args[6], ignore_errors=True)
        elif args[:1] == ("git",) and args[3:5] == ("rev-parse", "HEAD"):
            head = self.heads.get(Path(args[2]), "")
            return RunResult(0, f"{head}\n".encode())
        elif args[:3] == ("python3", "-m", "venv"):
            bin_dir = Path(args[-1]) / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("", encoding="utf-8")
            (bin_dir / "pip").write_text("", encoding="utf-8")
        return result


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(env={}, home=tmp_path / "home", uid=1000)


@pytest.fixture
def fake() -> FakeRunner:
    return FakeRunner(strict=False)


@pytest.fixture
def sim(fake: FakeRunner) -> Sim:
    return Sim(fake)


@pytest.fixture
def rel(layout: Layout, sim: Sim) -> Releases:
    return Releases(layout, sim)


def git(layout: Layout, *args: str) -> tuple[str, ...]:
    return ("git", "-C", str(layout.repo_git), *args)


def make_release(layout: Layout, sha: str, *, mtime: float | None = None) -> Path:
    """準備ずみの版のディレクトリを、直に作る。"""
    directory = layout.release_dir(sha)
    (directory / ".venv/bin").mkdir(parents=True)
    (directory / ".git").write_text("gitdir: x\n", encoding="utf-8")
    (directory / ".venv/bin/python").write_text("", encoding="utf-8")
    (directory / ".venv" / READY_MARKER).write_text(sha, encoding="utf-8")
    unit = directory / "deploy/systemd/osekkai.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(f"unit {sha}\n", encoding="utf-8")
    if mtime is not None:
        os.utime(directory, (mtime, mtime))
    return directory


def make_repo(layout: Layout) -> None:
    """bare の写しを、直に作る（中身は HEAD だけ）。"""
    layout.repo_git.mkdir(parents=True)
    (layout.repo_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


# --- 準備（1.1、1.2、1.8） ---


def test_初めての準備は写しを作り_取得し_worktree_と仮想環境と依存を入れる(
    layout: Layout, rel: Releases, fake: FakeRunner
) -> None:
    directory = rel.prepare(tag(SHA_A), URL)

    assert directory == layout.release_dir(SHA_A)
    venv = directory / ".venv"
    assert fake.argvs == [
        ("git", "init", "--bare", "-q", str(layout.repo_git)),
        git(layout, "config", "remote.origin.url", URL),
        git(layout, "config", "--replace-all", "remote.origin.fetch", FETCH_REFSPEC),
        git(layout, "fetch", "--prune", "--tags", "--force", "origin"),
        git(layout, "cat-file", "-e", f"{SHA_A}^{{commit}}"),
        git(layout, "worktree", "prune"),
        git(layout, "worktree", "add", "--detach", str(directory), SHA_A),
        ("python3", "-m", "venv", "--system-site-packages", str(venv)),
        (str(venv / "bin/pip"), "install", "-e", f"{directory}[pi]"),
    ]
    assert all(call.check for call in fake.calls)
    pip_call = fake.calls[-1]
    assert pip_call.timeout is not None and pip_call.timeout >= 600
    assert pip_call.cwd == directory
    # pip のキャッシュを使う（切る指定をしない）
    assert "--no-cache-dir" not in pip_call.argv
    assert (venv / READY_MARKER).read_text(encoding="utf-8") == SHA_A


def test_ブランチは作業の名前の参照を_取得の決まりに入れる() -> None:
    # bare の写しは決まりがないとブランチを取らない。リモートの名前の下に取る
    assert FETCH_REFSPEC == "+refs/heads/*:refs/remotes/origin/*"


def test_写しがあれば作り直さず_取得の先を合わせる(
    layout: Layout, rel: Releases, fake: FakeRunner
) -> None:
    rel.prepare(tag(SHA_A), URL)
    fake.calls.clear()

    rel.prepare(tag(SHA_B), URL)

    assert ("git", "init", "--bare", "-q", str(layout.repo_git)) not in fake.argvs
    assert fake.argvs[0] == git(layout, "config", "remote.origin.url", URL)
    assert git(layout, "fetch", "--prune", "--tags", "--force", "origin") in fake.argvs


def test_同じ版ができていれば使い回す(layout: Layout, rel: Releases, fake: FakeRunner) -> None:
    first = rel.prepare(tag(SHA_A), URL)
    fake.calls.clear()

    again = rel.prepare(tag(SHA_A), URL)

    assert again == first
    # HEAD を確かめるだけで、取得も依存の導入もしない
    assert fake.argvs == [("git", "-C", str(first), "rev-parse", "HEAD")]


def test_使い回した版は新しい順で前に来る(layout: Layout, rel: Releases) -> None:
    first = rel.prepare(tag(SHA_A), URL)
    os.utime(first, (1000.0, 1000.0))

    rel.prepare(tag(SHA_A), URL)

    assert first.stat().st_mtime > 1000.0


def test_仕上げの印がない作りかけは使い回さず作り直す(
    layout: Layout, rel: Releases, fake: FakeRunner, sim: Sim
) -> None:
    directory = rel.prepare(tag(SHA_A), URL)
    (directory / ".venv" / READY_MARKER).unlink()
    (directory / "half.txt").write_text("作りかけ", encoding="utf-8")
    fake.calls.clear()

    rel.prepare(tag(SHA_A), URL)

    assert git(layout, "worktree", "remove", "--force", str(directory)) in fake.argvs
    assert git(layout, "worktree", "add", "--detach", str(directory), SHA_A) in fake.argvs
    assert not (directory / "half.txt").exists()
    assert (directory / ".venv" / READY_MARKER).exists()


def test_HEAD_が違うディレクトリは使い回さない(
    layout: Layout, rel: Releases, fake: FakeRunner, sim: Sim
) -> None:
    directory = rel.prepare(tag(SHA_A), URL)
    sim.heads[directory] = SHA_B
    fake.calls.clear()

    rel.prepare(tag(SHA_A), URL)

    assert any(argv[3:5] == ("worktree", "add") for argv in fake.argvs)


def test_仮想環境の_python_がなければ使い回さない(
    layout: Layout, rel: Releases, fake: FakeRunner
) -> None:
    directory = rel.prepare(tag(SHA_A), URL)
    (directory / ".venv/bin/python").unlink()
    fake.calls.clear()

    rel.prepare(tag(SHA_A), URL)

    assert any(argv[:3] == ("python3", "-m", "venv") for argv in fake.argvs)


def test_サンプルは準備できない(rel: Releases) -> None:
    with pytest.raises(ValueError):
        rel.prepare(Target(kind="sample", ref=None, ref_kind=None, sha=None), URL)


# --- 失敗したときの片付け（3.6、4.2） ---


@pytest.mark.parametrize(
    ("prefix", "step"),
    [
        (("python3", "-m", "venv"), "venv"),
        (("PIP",), "pip"),
        (("WORKTREE_ADD",), "worktree"),
    ],
)
def test_途中で失敗すれば作りかけだけを消す(
    layout: Layout,
    rel: Releases,
    fake: FakeRunner,
    prefix: tuple[str, ...],
    step: str,
) -> None:
    other = make_release(layout, SHA_B)
    rel.switch(other)
    directory = layout.release_dir(SHA_A)
    if prefix == ("PIP",):
        prefix = (str(directory / ".venv/bin/pip"),)
    elif prefix == ("WORKTREE_ADD",):
        prefix = git(layout, "worktree", "add")
    fake.script(prefix, returncode=1, stderr=SECRET)

    with pytest.raises(PrepareFailed) as info:
        rel.prepare(tag(SHA_A), URL)

    assert info.value.step == step
    assert SECRET not in str(info.value)
    assert not directory.exists()
    assert git(layout, "worktree", "prune") in fake.argvs[-1:]
    # 今の版とほかの版には触れない
    assert rel.current() == other
    assert (other / ".venv" / READY_MARKER).exists()


def test_時間切れも決まった手順の名前で知らせる(
    layout: Layout, rel: Releases, fake: FakeRunner
) -> None:
    directory = layout.release_dir(SHA_A)
    fake.raise_on((str(directory / ".venv/bin/pip"),), CommandTimedOut("pip"))

    with pytest.raises(PrepareFailed) as info:
        rel.prepare(tag(SHA_A), URL)

    assert info.value.step == "pip"
    assert not directory.exists()


def test_取得に失敗すれば版のディレクトリを作らない(
    layout: Layout, rel: Releases, fake: FakeRunner
) -> None:
    other = make_release(layout, SHA_B)
    fake.script(git(layout, "fetch"), returncode=128, stderr=SECRET)

    with pytest.raises(PrepareFailed) as info:
        rel.prepare(tag(SHA_A), URL)

    assert info.value.step == "fetch"
    assert SECRET not in str(info.value)
    assert not layout.release_dir(SHA_A).exists()
    assert other.exists()


def test_写しを作れなければ作りかけの写しを消す(
    layout: Layout, rel: Releases, fake: FakeRunner
) -> None:
    fake.script(git(layout, "config"), returncode=1)

    with pytest.raises(PrepareFailed) as info:
        rel.prepare(tag(SHA_A), URL)

    assert info.value.step == "fetch"
    assert not layout.repo_git.exists()


def test_コミットがなければ版のディレクトリを作らない(
    layout: Layout, rel: Releases, fake: FakeRunner
) -> None:
    fake.script(git(layout, "cat-file"), returncode=128)

    with pytest.raises(PrepareFailed) as info:
        rel.prepare(tag(SHA_A), URL)

    assert info.value.step == "commit"
    assert not layout.release_dir(SHA_A).exists()
    assert not any(argv[3:5] == ("worktree", "add") for argv in fake.argvs)


def test_今の版のディレクトリは作り直さない(
    layout: Layout, rel: Releases, fake: FakeRunner
) -> None:
    directory = make_release(layout, SHA_A)
    rel.switch(directory)
    (directory / ".venv" / READY_MARKER).unlink()

    with pytest.raises(PrepareFailed) as info:
        rel.prepare(tag(SHA_A), URL)

    assert info.value.step == "worktree"
    assert directory.exists()
    assert rel.current() == directory
    assert not any(argv[3:5] == ("worktree", "remove") for argv in fake.argvs)


# --- 切り替え（1.1、4.2） ---


def test_初めての切り替えは前の先がない(layout: Layout, rel: Releases) -> None:
    directory = make_release(layout, SHA_A)

    assert rel.current() is None
    assert rel.switch(directory) is None
    assert layout.current_link.is_symlink()
    assert rel.current() == directory


def test_切り替えは前の先を返し_一時名を残さない(layout: Layout, rel: Releases) -> None:
    first = make_release(layout, SHA_A)
    second = make_release(layout, SHA_B)
    rel.switch(first)

    assert rel.switch(second) == first
    assert rel.current() == second
    assert sorted(p.name for p in layout.root.iterdir()) == ["current", "releases"]
    # 戻すときも同じ口で戻せる（4.2）
    assert rel.switch(first) == second
    assert rel.current() == first


def test_切り替えは一時名のリンクからの置き換えで行う(
    layout: Layout, rel: Releases, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = make_release(layout, SHA_A)
    replaced: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(src: Path, dst: Path) -> None:
        replaced.append((Path(src), Path(dst)))
        assert Path(src).is_symlink()
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)

    rel.switch(directory)

    assert len(replaced) == 1
    src, dst = replaced[0]
    assert dst == layout.current_link
    assert src.parent == layout.root and src != dst


def test_版の置き場所の外へは切り替えない(layout: Layout, rel: Releases, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(ValueError):
        rel.switch(elsewhere)
    with pytest.raises(ValueError):
        rel.switch(layout.release_dir(SHA_A))  # まだない
    assert not layout.current_link.exists()


# --- ユニット ---


def test_版のユニットを写して読み直す(layout: Layout, rel: Releases, fake: FakeRunner) -> None:
    directory = make_release(layout, SHA_A)

    rel.install_unit(directory)

    assert layout.core_unit_file.read_text(encoding="utf-8") == f"unit {SHA_A}\n"
    assert fake.argvs == [("systemctl", "--user", "daemon-reload")]
    assert [p.name for p in layout.user_unit_dir.iterdir()] == ["osekkai.service"]


def test_ユニットは上書きする(layout: Layout, rel: Releases) -> None:
    rel.install_unit(make_release(layout, SHA_A))
    rel.install_unit(make_release(layout, SHA_B))

    assert layout.core_unit_file.read_text(encoding="utf-8") == f"unit {SHA_B}\n"


def test_ユニットのファイルがなければ読み直さない(
    layout: Layout, rel: Releases, fake: FakeRunner
) -> None:
    directory = make_release(layout, SHA_A)
    (directory / "deploy/systemd/osekkai.service").unlink()

    with pytest.raises(ReleaseError):
        rel.install_unit(directory)

    assert fake.argvs == []
    assert not layout.core_unit_file.exists()


# --- 手の変更（1.9） ---


def test_手の変更は件数だけを数える(layout: Layout, fake: FakeRunner) -> None:
    directory = make_release(layout, SHA_A)
    fake.script(
        ("git", "-C", str(directory), "status", "--porcelain"),
        stdout=" M src/a.py\n?? secret-name.txt\n",
    )

    count = Releases(layout, fake).dirty_count(directory)

    assert count == 2
    assert fake.argvs == [("git", "-C", str(directory), "status", "--porcelain")]


def test_worktree_の印がなければ手の変更を数えない(layout: Layout, fake: FakeRunner) -> None:
    # 印がないと、git は親をさかのぼって別のリポジトリを数えてしまう
    directory = make_release(layout, SHA_A)
    (directory / ".git").unlink()

    with pytest.raises(ReleaseError):
        Releases(layout, fake).dirty_count(directory)

    assert fake.argvs == []


def test_worktree_の印がなければ使い回さない(
    layout: Layout, rel: Releases, fake: FakeRunner
) -> None:
    directory = rel.prepare(tag(SHA_A), URL)
    (directory / ".git").unlink()
    fake.calls.clear()

    rel.prepare(tag(SHA_A), URL)

    assert ("git", "-C", str(directory), "rev-parse", "HEAD") not in fake.argvs
    assert any(argv[3:5] == ("worktree", "add") for argv in fake.argvs)
    assert (directory / ".git").is_file()


def test_手の変更がなければ_0(layout: Layout, fake: FakeRunner) -> None:
    directory = make_release(layout, SHA_A)
    assert Releases(layout, fake).dirty_count(directory) == 0


# --- 古い版の削除（3.6） ---


def test_削除は今_前_普段の版と新しい順に_3_つを残す(
    layout: Layout, rel: Releases, fake: FakeRunner
) -> None:
    make_repo(layout)
    dirs = [make_release(layout, sha_of(n), mtime=1000.0 + n) for n in range(1, 8)]
    rel.switch(dirs[0])  # 今の版はいちばん古い
    keep = [dirs[1], dirs[2]]  # 前の版と普段の版
    not_release = layout.releases_dir / "notes"
    not_release.mkdir()
    fake.calls.clear()

    rel.prune(keep)

    remaining = {p for p in layout.releases_dir.iterdir()}
    assert remaining == {dirs[0], dirs[1], dirs[2], *dirs[-KEEP_RELEASES:], not_release}
    removed = [dirs[3]]
    assert fake.argvs == [
        *(git(layout, "worktree", "remove", "--force", str(d)) for d in removed),
        git(layout, "worktree", "prune"),
    ]


def test_残すものはリンクの道でも分かる(layout: Layout, rel: Releases) -> None:
    dirs = [make_release(layout, sha_of(n), mtime=1000.0 + n) for n in range(1, 6)]
    rel.switch(dirs[0])

    rel.prune([layout.current_link])

    assert dirs[0].exists()
    assert not dirs[1].exists()


def test_worktree_で消せなければディレクトリごと消す(
    layout: Layout, rel: Releases, fake: FakeRunner
) -> None:
    make_repo(layout)
    dirs = [make_release(layout, sha_of(n), mtime=1000.0 + n) for n in range(1, 5)]
    fake.script(git(layout, "worktree", "remove"), returncode=128)

    rel.prune([])

    assert not dirs[0].exists()
    assert all(d.exists() for d in dirs[1:])


def test_版の置き場所がなければ何もしない(rel: Releases, fake: FakeRunner) -> None:
    rel.prune([])
    assert fake.argvs == []


def test_置き場所の中のリンクはたどらない(layout: Layout, rel: Releases, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("外のもの", encoding="utf-8")
    for n in range(1, 5):
        make_release(layout, sha_of(n), mtime=1000.0 + n)
    link = layout.releases_dir / ("f" * 12)
    link.symlink_to(outside)
    os.utime(link, (1.0, 1.0), follow_symlinks=False)

    rel.prune([])

    assert (outside / "keep.txt").exists()


# --- サンプルと XDG の場所に触れない（3.6、9.1） ---


def _snapshot(root: Path) -> dict[str, tuple[float, bytes]]:
    snap: dict[str, tuple[float, bytes]] = {}
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        content = path.read_bytes() if path.is_file() else b""
        snap[str(path.relative_to(root))] = (stat.st_mtime, content)
    return snap


def test_どの操作もサンプルと基盤の_XDG_の場所に触れない(
    layout: Layout, rel: Releases, fake: FakeRunner
) -> None:
    watched = [
        layout.sample_dir,
        layout.sample_venv,
        layout.core_config_dir,
        layout.core_state_dir,
    ]
    for root in watched:
        (root / "sub").mkdir(parents=True)
        (root / "sub/file.txt").write_text("もとのまま", encoding="utf-8")
    (layout.sample_dir / ".git").mkdir()
    layout.user_unit_dir.mkdir(parents=True)
    layout.sample_unit_file.write_text("sample unit\n", encoding="utf-8")
    before = {str(root): _snapshot(root) for root in watched}
    sample_unit = layout.sample_unit_file.read_bytes()

    first = rel.prepare(tag(SHA_A), URL)
    rel.prepare(tag(SHA_A), URL)  # 使い回し
    rel.switch(first)
    rel.install_unit(first)
    rel.dirty_count(first)
    fake.script((str(layout.release_dir(SHA_B) / ".venv/bin/pip"),), returncode=1)
    with pytest.raises(PrepareFailed):
        rel.prepare(tag(SHA_B), URL)
    for n in range(1, 6):
        make_release(layout, sha_of(n), mtime=1000.0 + n)
    rel.prune([])

    assert {str(root): _snapshot(root) for root in watched} == before
    assert layout.sample_unit_file.read_bytes() == sample_unit
    sample_paths = {str(layout.sample_dir), str(layout.sample_venv)}
    for argv in fake.argvs:
        assert not any(arg.startswith(p) for arg in argv for p in sample_paths)


# --- 本物の git で、写し・取得・worktree の手順を確かめる ---


class HybridRunner:
    """git は本物で呼び、仮想環境と pip は結果だけを作る。"""

    def __init__(self) -> None:
        self.real = SubprocessRunner()
        self.argvs: list[tuple[str, ...]] = []

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
        self.argvs.append(args)
        if args[0] == "git":
            return self.real(args, input=input, timeout=timeout, check=check, cwd=cwd, env=env)
        if args[:3] == ("python3", "-m", "venv"):
            (Path(args[-1]) / "bin").mkdir(parents=True)
            (Path(args[-1]) / "bin/python").write_text("", encoding="utf-8")
        return RunResult(0)


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return completed.stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git がない")
def test_本物の_git_でタグとブランチの版を準備して片付ける(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (origin / "deploy/systemd").mkdir(parents=True)
    (origin / "deploy/systemd/osekkai.service").write_text("unit 1\n", encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "first")
    _git(origin, "tag", "v0.1.0")
    tag_sha = _git(origin, "rev-parse", "HEAD")
    _git(origin, "switch", "-q", "-c", "feat/x")
    (origin / "deploy/systemd/osekkai.service").write_text("unit 2\n", encoding="utf-8")
    _git(origin, "commit", "-q", "-am", "second")
    branch_sha = _git(origin, "rev-parse", "HEAD")
    _git(origin, "switch", "-q", "main")

    layout = Layout(env={}, home=tmp_path / "home", uid=1000)
    run = HybridRunner()
    rel = Releases(layout, run)

    first = rel.prepare(tag(tag_sha), str(origin))
    # リポジトリでタグを付け直しても、次の取得は止まらない
    _git(origin, "tag", "-f", "v0.1.0", branch_sha)
    branch = Target(kind="release", ref="feat/x", ref_kind="branch", sha=branch_sha)
    second = rel.prepare(branch, str(origin))

    assert _git(first, "rev-parse", "HEAD") == tag_sha
    assert _git(second, "rev-parse", "HEAD") == branch_sha
    assert (second / "deploy/systemd/osekkai.service").read_text(encoding="utf-8") == "unit 2\n"
    assert _git(layout.repo_git, "rev-parse", "refs/remotes/origin/feat/x") == branch_sha
    assert _git(layout.repo_git, "rev-parse", "refs/tags/v0.1.0^{commit}") == branch_sha
    assert rel.dirty_count(second) == 0

    # 使い回し
    run.argvs.clear()
    assert rel.prepare(tag(tag_sha), str(origin)) == first
    assert [a[3:5] for a in run.argvs] == [("rev-parse", "HEAD")]

    # ないコミット
    with pytest.raises(PrepareFailed) as info:
        rel.prepare(tag("c" * 40), str(origin))
    assert info.value.step == "commit"

    # 手の変更
    (second / "deploy/systemd/osekkai.service").write_text("changed\n", encoding="utf-8")
    assert rel.dirty_count(second) == 1

    # 片付け: 4 つ目を足すと、いちばん古いものが消え、worktree の登録も消える
    rel.switch(second)
    os.utime(first, (1000.0, 1000.0))
    for n, sha in enumerate([sha_of(1), sha_of(2), sha_of(3)]):
        make_release(layout, sha, mtime=2000.0 + n)
    rel.prune([])
    assert not first.exists()
    assert second.exists()
    assert str(first) not in _git(layout.repo_git, "worktree", "list", "--porcelain")


# --- ユニットのない版（実機確認で見つけた穴） ---


def test_prepare_refuses_release_without_core_unit_before_venv(
    layout: Layout, sim: Sim, rel: Releases
) -> None:
    """新しい基盤のユニットがない版（この道具より前の版）は、仮想環境を作る前に断る。"""
    sim.without_unit.add(SHA_A)
    with pytest.raises(PrepareFailed) as info:
        rel.prepare(tag(SHA_A), URL)
    assert info.value.step == "unit"
    assert not layout.release_dir(SHA_A).exists()  # 作りかけは消す
    assert not any(argv[:3] == ("python3", "-m", "venv") for argv in sim.fake.argvs)
    assert rel.current() is None


def test_prepare_refuses_reused_release_without_core_unit(
    layout: Layout, sim: Sim, rel: Releases
) -> None:
    directory = make_release(layout, SHA_A)
    (directory / "deploy/systemd/osekkai.service").unlink()
    sim.heads[directory] = SHA_A
    with pytest.raises(PrepareFailed) as info:
        rel.prepare(tag(SHA_A), URL)
    assert info.value.step == "unit"
    assert directory.is_dir()  # 使い回しの版は消さない
    assert rel.current() is None
    assert not any(
        "fetch" in argv or argv[:3] == ("python3", "-m", "venv") for argv in sim.fake.argvs
    )
