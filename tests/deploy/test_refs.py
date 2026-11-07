"""タグとブランチの解決と、push 済みの確認のテスト（1.1〜1.4）。"""

import shutil
import subprocess
from pathlib import Path

import pytest
from fake_runner import FakeRunner

from rover_deploy.common import ExitCode, Target
from rover_deploy.refs import (
    BadRefName,
    RefError,
    RefNotFound,
    RemoteUnreachable,
    Resolution,
    UnpushedCommits,
    resolve_ref,
)
from rover_deploy.system import CommandNotFound, CommandTimedOut, SubprocessRunner

ROOT = Path("/repo")
GIT = ("git", "-C", "/repo")
SHA_TAG = "a" * 40  # 注釈付きのタグそのもの
SHA_COMMIT = "b" * 40
SHA_BRANCH = "c" * 40


def ls_remote(name: str) -> tuple[str, ...]:
    return (
        *GIT,
        "ls-remote",
        "--tags",
        "--heads",
        "origin",
        f"refs/tags/{name}",
        f"refs/tags/{name}^{{}}",
        f"refs/heads/{name}",
    )


def local_ref(ref: str) -> tuple[str, ...]:
    return (*GIT, "rev-parse", "--verify", "--quiet", ref)


def fetch(name: str) -> tuple[str, ...]:
    return (*GIT, "fetch", "--quiet", "origin", f"+refs/heads/{name}:refs/remotes/origin/{name}")


def count(name: str) -> tuple[str, ...]:
    return (*GIT, "rev-list", "--count", f"refs/remotes/origin/{name}..refs/heads/{name}")


def lines(*rows: tuple[str, str]) -> str:
    return "".join(f"{sha}\t{ref}\n" for sha, ref in rows)


def test_annotated_tag_uses_peeled_commit(fake_runner: FakeRunner) -> None:
    fake_runner.script(
        ls_remote("v0.2.0"),
        stdout=lines((SHA_TAG, "refs/tags/v0.2.0"), (SHA_COMMIT, "refs/tags/v0.2.0^{}")),
    )
    result = resolve_ref("v0.2.0", ROOT, fake_runner)
    assert result == Resolution(Target("release", "v0.2.0", "tag", SHA_COMMIT), also_branch=False)
    # ブランチでないので、手元のブランチは見ない
    assert fake_runner.argvs == [ls_remote("v0.2.0")]
    # 端末で名前や鍵を聞いて止まらない
    assert fake_runner.calls[0].env == {"GIT_TERMINAL_PROMPT": "0"}


def test_lightweight_tag_uses_its_commit(fake_runner: FakeRunner) -> None:
    fake_runner.script(ls_remote("v1"), stdout=lines((SHA_COMMIT, "refs/tags/v1")))
    result = resolve_ref("v1", ROOT, fake_runner)
    assert result.target == Target("release", "v1", "tag", SHA_COMMIT)


def test_both_tag_and_branch_choose_tag(fake_runner: FakeRunner) -> None:
    fake_runner.script(
        ls_remote("v1"),
        stdout=lines(
            (SHA_BRANCH, "refs/heads/v1"),
            (SHA_TAG, "refs/tags/v1"),
            (SHA_COMMIT, "refs/tags/v1^{}"),
        ),
    )
    result = resolve_ref("v1", ROOT, fake_runner)
    assert result == Resolution(Target("release", "v1", "tag", SHA_COMMIT), also_branch=True)
    assert fake_runner.argvs == [ls_remote("v1")]


def test_only_exact_names_match(fake_runner: FakeRunner) -> None:
    """末尾だけが同じ名前（x/v1 や refs/heads/refs/tags/v1）は使わない。"""
    fake_runner.script(
        ls_remote("v1"),
        stdout=lines(
            (SHA_TAG, "refs/tags/x/v1"),
            (SHA_BRANCH, "refs/heads/foo/v1"),
            (SHA_COMMIT, "refs/heads/refs/tags/v1"),
        ),
    )
    fake_runner.script(local_ref("refs/tags/v1"), returncode=1)
    fake_runner.script(local_ref("refs/heads/v1"), returncode=1)
    with pytest.raises(RefNotFound) as caught:
        resolve_ref("v1", ROOT, fake_runner)
    assert caught.value.local_only is False


def test_pushed_branch_without_local_branch(fake_runner: FakeRunner) -> None:
    fake_runner.script(ls_remote("feat/x"), stdout=lines((SHA_BRANCH, "refs/heads/feat/x")))
    fake_runner.script(local_ref("refs/heads/feat/x"), returncode=1)
    result = resolve_ref("feat/x", ROOT, fake_runner)
    assert result == Resolution(Target("release", "feat/x", "branch", SHA_BRANCH), False)


def test_branch_behind_remote_uses_remote_latest(fake_runner: FakeRunner) -> None:
    fake_runner.script(ls_remote("feat/x"), stdout=lines((SHA_BRANCH, "refs/heads/feat/x")))
    fake_runner.script(local_ref("refs/heads/feat/x"), stdout="d" * 40 + "\n")
    fake_runner.script(fetch("feat/x"))
    fake_runner.script(count("feat/x"), stdout="0\n")
    result = resolve_ref("feat/x", ROOT, fake_runner)
    assert result.target == Target("release", "feat/x", "branch", SHA_BRANCH)
    assert fake_runner.argvs[-2:] == [fetch("feat/x"), count("feat/x")]
    assert fake_runner.calls[-2].env == {"GIT_TERMINAL_PROMPT": "0"}


def test_unpushed_commits_abort(fake_runner: FakeRunner) -> None:
    fake_runner.script(ls_remote("feat/x"), stdout=lines((SHA_BRANCH, "refs/heads/feat/x")))
    fake_runner.script(local_ref("refs/heads/feat/x"), stdout="d" * 40 + "\n")
    fake_runner.script(fetch("feat/x"))
    fake_runner.script(count("feat/x"), stdout="3\n")
    with pytest.raises(UnpushedCommits) as caught:
        resolve_ref("feat/x", ROOT, fake_runner)
    assert (caught.value.name, caught.value.count) == ("feat/x", 3)
    assert "push" in str(caught.value)
    assert caught.value.exit_code == ExitCode.ABORTED


def test_local_only_tag_is_not_found_with_push_hint(fake_runner: FakeRunner) -> None:
    fake_runner.script(ls_remote("v9"), stdout="")
    fake_runner.script(local_ref("refs/tags/v9"), stdout=SHA_TAG + "\n")
    with pytest.raises(RefNotFound) as caught:
        resolve_ref("v9", ROOT, fake_runner)
    assert caught.value.name == "v9"
    assert caught.value.local_only is True
    assert "push" in str(caught.value)


def test_local_only_branch_is_not_found_with_push_hint(fake_runner: FakeRunner) -> None:
    fake_runner.script(ls_remote("feat/y"), stdout="")
    fake_runner.script(local_ref("refs/tags/feat/y"), returncode=1)
    fake_runner.script(local_ref("refs/heads/feat/y"), stdout=SHA_BRANCH + "\n")
    with pytest.raises(RefNotFound) as caught:
        resolve_ref("feat/y", ROOT, fake_runner)
    assert caught.value.local_only is True


def test_not_found_anywhere(fake_runner: FakeRunner) -> None:
    fake_runner.script(ls_remote("nope"), stdout="")
    fake_runner.script(local_ref("refs/tags/nope"), returncode=1)
    fake_runner.script(local_ref("refs/heads/nope"), returncode=1)
    with pytest.raises(RefNotFound) as caught:
        resolve_ref("nope", ROOT, fake_runner)
    assert caught.value.local_only is False
    assert "見つかりません" in str(caught.value)
    assert "push" not in str(caught.value)
    assert caught.value.exit_code == ExitCode.ABORTED


@pytest.mark.parametrize(
    "error", [CommandTimedOut("git"), CommandNotFound("git")], ids=["timeout", "no_git"]
)
def test_ls_remote_errors_are_unreachable(fake_runner: FakeRunner, error: Exception) -> None:
    fake_runner.raise_on(ls_remote("v1"), error)
    with pytest.raises(RemoteUnreachable):
        resolve_ref("v1", ROOT, fake_runner)


def test_ls_remote_failure_hides_stderr(fake_runner: FakeRunner) -> None:
    fake_runner.script(ls_remote("v1"), returncode=128, stderr="fatal: hidden-host denied")
    with pytest.raises(RemoteUnreachable) as caught:
        resolve_ref("v1", ROOT, fake_runner)
    assert "hidden-host" not in str(caught.value)
    assert caught.value.exit_code == ExitCode.ABORTED


def test_fetch_failure_is_unreachable(fake_runner: FakeRunner) -> None:
    fake_runner.script(ls_remote("feat/x"), stdout=lines((SHA_BRANCH, "refs/heads/feat/x")))
    fake_runner.script(local_ref("refs/heads/feat/x"), stdout="d" * 40 + "\n")
    fake_runner.script(fetch("feat/x"), returncode=128, stderr="fatal: hidden")
    with pytest.raises(RemoteUnreachable):
        resolve_ref("feat/x", ROOT, fake_runner)


@pytest.mark.parametrize("output", ["x\n", "", "-1\n"], ids=["word", "empty", "negative"])
def test_unreadable_count_is_error(fake_runner: FakeRunner, output: str) -> None:
    fake_runner.script(ls_remote("feat/x"), stdout=lines((SHA_BRANCH, "refs/heads/feat/x")))
    fake_runner.script(local_ref("refs/heads/feat/x"), stdout="d" * 40 + "\n")
    fake_runner.script(fetch("feat/x"))
    fake_runner.script(count("feat/x"), stdout=output)
    with pytest.raises(RefError):
        resolve_ref("feat/x", ROOT, fake_runner)


def test_broken_ls_remote_output_is_error(fake_runner: FakeRunner) -> None:
    fake_runner.script(ls_remote("v1"), stdout="zzz\trefs/tags/v1\n")
    with pytest.raises(RefError):
        resolve_ref("v1", ROOT, fake_runner)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "-v1",
        "--upload-pack=x",
        "a b",
        "a..b",
        "a\tb",
        "a\x7fb",
        "v1.lock",
        "a~1",
        "a^",
        "a:b",
        "a?",
        "a*",
        "a[b",
        "a\\b",
        "/a",
        "a/",
        "a//b",
        "a.",
        ".a",
        "a/.b",
        "a@{1}",
        "@",
    ],
)
def test_bad_names_never_reach_git(fake_runner: FakeRunner, name: str) -> None:
    with pytest.raises(BadRefName) as caught:
        resolve_ref(name, ROOT, fake_runner)
    assert fake_runner.calls == []
    assert caught.value.exit_code == ExitCode.ABORTED


# --- 本当の git と、手元の bare のリモートで確かめる ---


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git がない")
def test_real_git_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "commit.gpgsign", "false")
    _git(work, "config", "tag.gpgsign", "false")
    _git(work, "remote", "add", "origin", remote.as_uri())
    _git(work, "commit", "-q", "--allow-empty", "-m", "first")
    first = _git(work, "rev-parse", "HEAD")
    _git(work, "tag", "-a", "v1", "-m", "v1")
    _git(work, "tag", "light")
    _git(work, "branch", "both")
    _git(work, "tag", "both")
    _git(work, "branch", "feat/x")
    _git(work, "push", "-q", "origin", "main", "feat/x", "refs/heads/both", "--tags")
    run = SubprocessRunner()

    assert resolve_ref("v1", work, run).target == Target("release", "v1", "tag", first)
    assert resolve_ref("light", work, run).target == Target("release", "light", "tag", first)
    both = resolve_ref("both", work, run)
    assert both == Resolution(Target("release", "both", "tag", first), also_branch=True)
    assert resolve_ref("feat/x", work, run).target == Target("release", "feat/x", "branch", first)

    # 手元に push していないコミット
    _git(work, "switch", "-q", "feat/x")
    _git(work, "commit", "-q", "--allow-empty", "-m", "second")
    with pytest.raises(UnpushedCommits) as caught:
        resolve_ref("feat/x", work, run)
    assert caught.value.count == 1

    # push した後は、その最新
    _git(work, "push", "-q", "origin", "feat/x")
    second = _git(work, "rev-parse", "HEAD")
    assert resolve_ref("feat/x", work, run).target.sha == second

    # 手元が遅れているだけなら、リモートの最新
    _git(work, "switch", "-q", "main")
    _git(work, "branch", "-f", "feat/x", first)
    assert resolve_ref("feat/x", work, run).target.sha == second

    # 手元だけのタグ
    _git(work, "tag", "local-only")
    with pytest.raises(RefNotFound) as not_found:
        resolve_ref("local-only", work, run)
    assert not_found.value.local_only is True

    with pytest.raises(RefNotFound):
        resolve_ref("missing", work, run)
