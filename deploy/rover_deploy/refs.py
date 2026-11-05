"""開発 PC: タグとブランチの解決と、push 済みの確認（1.1〜1.4）。

リモート（origin）のタグとブランチを `git ls-remote` で探す。タグとブランチの両方に
同じ名前があればタグを選び、その旨を返す（表示は devcli がする）。
git の標準エラーは例外の文言に写さない。文言に出すのは、利用者が打った版の名前だけ。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rover_deploy.common import ExitCode, Target
from rover_deploy.system import CommandError, Runner

QUERY_TIMEOUT_S = 15.0  # 手元だけで済む git
NETWORK_TIMEOUT_S = 60.0  # リモートに問い合わせる git
# 名前や鍵を端末で聞いて止まらないようにする
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_COUNT_RE = re.compile(r"[0-9]+")
# git check-ref-format で使えない文字（空白と制御文字は別に確かめる）
_BAD_CHARS = frozenset("~^:?*[\\")
_PEELED = "^{}"


class RefError(Exception):
    """版の名前を解決できない。終了コードは中止（10）。"""

    exit_code = ExitCode.ABORTED


class BadRefName(RefError):
    """版の名前の形が違う。名前そのものは文言に出さない（制御文字を含みうる）。"""

    def __init__(self) -> None:
        super().__init__("版の名前に使えない文字か形があります")


class RefNotFound(RefError):
    """版の名前がリモートにない（1.3）。手元にだけあれば、push が要ると添える。"""

    def __init__(self, name: str, *, local_only: bool = False) -> None:
        message = f"版 {name} がリポジトリ（origin）に見つかりません"
        if local_only:
            message += "。手元にだけあるので、先に push してください"
        super().__init__(message)
        self.name = name
        self.local_only = local_only


class UnpushedCommits(RefError):
    """手元のブランチに push していないコミットがある（1.4）。"""

    def __init__(self, name: str, count: int) -> None:
        super().__init__(
            f"ブランチ {name} に push していないコミットが {count} 件あります。"
            "先に push してください"
        )
        self.name = name
        self.count = count


class RemoteUnreachable(RefError):
    """リポジトリ（origin）に問い合わせられない。"""

    def __init__(self) -> None:
        super().__init__("リポジトリ（origin）に接続できません")


@dataclass(frozen=True)
class Resolution:
    """解決した版。`also_branch` はタグを選んだが同じ名前のブランチもあったとき True。"""

    target: Target
    also_branch: bool


def check_ref_name(name: str) -> None:
    """git に渡す前に名前の形を確かめる（git check-ref-format --allow-onelevel に近い決まり）。

    先頭の "-" は git のオプションと見なされるので断る。
    """
    if not name or name == "@" or name.startswith("-"):
        raise BadRefName
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in name):
        raise BadRefName
    if any(ch in _BAD_CHARS for ch in name) or ".." in name or "@{" in name:
        raise BadRefName
    if name.endswith((".", ".lock")):
        raise BadRefName
    for part in name.split("/"):
        if not part or part.startswith(".") or part.endswith(".lock"):
            raise BadRefName


def _git(repo_root: Path, *args: str) -> list[str]:
    return ["git", "-C", str(repo_root), *args]


def _remote_refs(name: str, repo_root: Path, run: Runner) -> dict[str, str]:
    """リモートの、ちょうどその名前の参照と sha。キーは refs/tags/<n> などの形。"""
    wanted = (f"refs/tags/{name}", f"refs/tags/{name}{_PEELED}", f"refs/heads/{name}")
    argv = _git(repo_root, "ls-remote", "--tags", "--heads", "origin", *wanted)
    try:
        result = run(argv, timeout=NETWORK_TIMEOUT_S, check=False, env=GIT_ENV)
    except CommandError:
        raise RemoteUnreachable from None
    if result.returncode != 0:
        raise RemoteUnreachable
    found: dict[str, str] = {}
    for line in result.text.splitlines():
        sha, sep, ref = line.partition("\t")
        # パターンは末尾で合うので、ちょうど同じ名前だけを使う
        if not sep or ref not in wanted:
            continue
        if not _SHA_RE.fullmatch(sha):
            raise RefError("リポジトリ（origin）の答えの形が違います")
        found[ref] = sha
    return found


def _has_local(ref: str, repo_root: Path, run: Runner) -> bool:
    argv = _git(repo_root, "rev-parse", "--verify", "--quiet", ref)
    try:
        result = run(argv, timeout=QUERY_TIMEOUT_S, check=False)
    except CommandError:
        raise RefError("手元のリポジトリを読めません") from None
    return result.returncode == 0


def _unpushed_count(name: str, repo_root: Path, run: Runner) -> int:
    """手元のブランチにあって、リモートのブランチにないコミットの数。"""
    tracking = f"refs/remotes/origin/{name}"
    # 追いかけの参照を今のリモートに合わせてから数える
    fetch = _git(repo_root, "fetch", "--quiet", "origin", f"+refs/heads/{name}:{tracking}")
    try:
        run(fetch, timeout=NETWORK_TIMEOUT_S, check=True, env=GIT_ENV)
    except CommandError:
        raise RemoteUnreachable from None
    argv = _git(repo_root, "rev-list", "--count", f"{tracking}..refs/heads/{name}")
    unreadable = "push していないコミットがあるかを確かめられません"
    try:
        text = run(argv, timeout=QUERY_TIMEOUT_S, check=True).text.strip()
    except CommandError:
        raise RefError(unreadable) from None
    if not _COUNT_RE.fullmatch(text):
        raise RefError(unreadable)
    return int(text)


def resolve_ref(name: str, repo_root: Path, run: Runner) -> Resolution:
    """版の名前をリモートのタグかブランチに解決する。

    タグは注釈付きなら先のコミットを使う（1.1）。ブランチはリモートの最新を使い、
    手元に push していないコミットがあれば `UnpushedCommits`（1.2、1.4）。
    どちらにもなければ `RefNotFound`（1.3）。
    """
    check_ref_name(name)
    remote = _remote_refs(name, repo_root, run)
    tag_ref, branch_ref = f"refs/tags/{name}", f"refs/heads/{name}"
    tag_sha = remote.get(f"{tag_ref}{_PEELED}") or remote.get(tag_ref)
    branch_sha = remote.get(branch_ref)
    if tag_sha is not None:
        return Resolution(Target("release", name, "tag", tag_sha), branch_sha is not None)
    if branch_sha is None:
        local_only = _has_local(tag_ref, repo_root, run) or _has_local(branch_ref, repo_root, run)
        raise RefNotFound(name, local_only=local_only)
    if _has_local(branch_ref, repo_root, run):
        count = _unpushed_count(name, repo_root, run)
        if count > 0:
            raise UnpushedCommits(name, count)
    # 手元が遅れているだけなら、リモートの最新を使う
    return Resolution(Target("release", name, "branch", branch_sha), also_branch=False)
