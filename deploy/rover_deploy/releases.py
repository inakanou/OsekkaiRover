"""版のディレクトリの準備・切り替え・片付け（1.1、1.2、1.8、1.9、3.6、4.2、9.1）。

版は `~/osekkai-rover/releases/<sha の先頭 12 桁>/` に、
bare の写し（repo.git）の worktree として作る。
版ごとに仮想環境（`.venv`）を持つので、リンク（current）とユニットを戻せば、依存関係も戻る。
書くのは `~/osekkai-rover/` の下と、新しい基盤のユニットのファイルだけ。
サンプルの場所と、基盤の XDG のディレクトリ（設定・記録）には何も書かない。

外のコマンドの失敗は、決まった手順の名前だけを持つ例外にする（標準エラーは写さない）。
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from collections.abc import Collection
from pathlib import Path

from rover_deploy.common import Target
from rover_deploy.layout import CORE_UNIT_NAME, RELEASE_DIR_LEN, Layout
from rover_deploy.system import CommandError, Runner, daemon_reload, git_dirty_count

KEEP_RELEASES = 3  # 今・前・普段の版のほかに、新しい順に残す数

# bare の写しは、決まりを書かないとブランチを取らない。リモートの名前の下に取る
FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
# 依存関係まで入れ終えた印。仮想環境の中に置く（.venv は git の対象の外なので、手の変更に数えない）
READY_MARKER = ".osekkai-ready"
UNIT_SOURCE = Path("deploy/systemd") / CORE_UNIT_NAME
VENV_PYTHON = "python3"
# voice は音声の入口の聞き取りの依存（faster-whisper など）
# conversation はおしゃべりの依存（anthropic）
# remote はラジコンの端末向けの画面の依存（flask）
PIP_EXTRAS = "pi,voice,conversation,remote"

# 時間切れ（秒）。pip は Pi で依存をビルドすることがあるので長くとる
GIT_TIMEOUT_S = 60.0
FETCH_TIMEOUT_S = 600.0
VENV_TIMEOUT_S = 300.0
PIP_TIMEOUT_S = 1800.0

_RELEASE_NAME_RE = re.compile(rf"[0-9a-f]{{{RELEASE_DIR_LEN}}}")
_LINK_TMP_NAME = ".current.tmp"
_UNIT_TMP_NAME = f".{CORE_UNIT_NAME}.tmp"


# 版の準備の段階 → 文言（開発 PC の表示でも使う）
PREPARE_MESSAGES: dict[str, str] = {
    "fetch": "リポジトリから取得できませんでした",
    "commit": "指定のコミットがリポジトリにありません",
    "worktree": "版のディレクトリを作れませんでした",
    "unit": "その版には新しい基盤のユニットがありません（この道具より前の版です）",
    "venv": "仮想環境を作れませんでした",
    "pip": "依存関係を入れられませんでした",
}


def prepare_message(step: str) -> str:
    return PREPARE_MESSAGES.get(step, "版を準備できませんでした")


class ReleaseError(Exception):
    """版のディレクトリを扱えなかった。文言は決まったものだけ。"""


class PrepareFailed(ReleaseError):
    """版の準備の失敗。`step` は fetch・commit・worktree・unit・venv・pip のどれか。"""

    def __init__(self, step: str) -> None:
        super().__init__(prepare_message(step))
        self.step = step


def _fsync_dir(path: Path) -> None:
    """ディレクトリの中の名前の変更を、ディスクに書き切る。できなくても続ける。"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class Releases:
    """版のディレクトリを作り、切り替え、片付ける。"""

    def __init__(self, layout: Layout, run: Runner) -> None:
        self._layout = layout
        self._run = run

    # --- 置き場所 ---

    def _git(self, *args: str) -> list[str]:
        return ["git", "-C", str(self._layout.repo_git), *args]

    def _repo_exists(self) -> bool:
        return (self._layout.repo_git / "HEAD").is_file()

    def _is_release_dir(self, path: Path) -> bool:
        """版の置き場所のすぐ下の、版の名前の、本物のディレクトリか。"""
        return (
            path.parent == self._layout.releases_dir
            and _RELEASE_NAME_RE.fullmatch(path.name) is not None
            and path.is_dir()
            and not path.is_symlink()
        )

    def _check_release_dir(self, path: Path) -> None:
        if not self._is_release_dir(path):
            raise ValueError("版の置き場所の中の、版のディレクトリを指定します")

    @staticmethod
    def _has_worktree_link(directory: Path) -> bool:
        """worktree の印（.git のファイル）があるか。

        ないと、git は親のディレクトリをさかのぼって、別のリポジトリを見てしまう。
        """
        return (directory / ".git").is_file()

    def _same_path(self, a: Path, b: Path | None) -> bool:
        if b is None:
            return False
        return os.path.realpath(a) == os.path.realpath(b)

    # --- 準備（1.1、1.2、1.8） ---

    def _reusable(self, directory: Path, sha: str) -> bool:
        """同じ sha で、依存関係まで入れ終えていれば使い回せる。"""
        venv = directory / ".venv"
        marker = venv / READY_MARKER
        if not self._is_release_dir(directory):
            return False
        if not self._has_worktree_link(directory):
            return False
        if not (venv / "bin/python").exists() or not marker.is_file():
            return False
        try:
            if marker.read_text(encoding="utf-8").strip() != sha:
                return False
            argv = ["git", "-C", str(directory), "rev-parse", "HEAD"]
            head = self._run(argv, timeout=GIT_TIMEOUT_S, check=True).text.strip()
        except (OSError, CommandError):
            return False
        return head == sha

    def _fetch(self, repo_url: str) -> None:
        repo = self._layout.repo_git
        created = False
        try:
            if not self._repo_exists():
                created = not repo.exists()
                repo.parent.mkdir(parents=True, exist_ok=True)
                self._run(
                    ["git", "init", "--bare", "-q", str(repo)], timeout=GIT_TIMEOUT_S, check=True
                )
            # 毎回そろえる（作りかけの写しや、取得の先の変更にも効く）
            self._run(
                self._git("config", "remote.origin.url", repo_url),
                timeout=GIT_TIMEOUT_S,
                check=True,
            )
            self._run(
                self._git("config", "--replace-all", "remote.origin.fetch", FETCH_REFSPEC),
                timeout=GIT_TIMEOUT_S,
                check=True,
            )
        except (OSError, CommandError):
            if created:
                shutil.rmtree(repo, ignore_errors=True)  # 今回作った写しだけを消す
            raise PrepareFailed("fetch") from None
        try:
            self._run(
                # --force: リポジトリでタグを付け直しても、取得を断らない（git 2.20 から要る）
                self._git("fetch", "--prune", "--tags", "--force", "origin"),
                timeout=FETCH_TIMEOUT_S,
                check=True,
            )
        except CommandError:
            raise PrepareFailed("fetch") from None

    def _build(self, directory: Path, sha: str) -> None:
        """worktree・仮想環境・依存関係を作る。失敗は手順の名前で知らせる。"""
        step = "worktree"
        try:
            self._run(self._git("worktree", "prune"), timeout=GIT_TIMEOUT_S, check=True)
            self._run(
                self._git("worktree", "add", "--detach", str(directory), sha),
                timeout=GIT_TIMEOUT_S,
                check=True,
            )
            if not (directory / UNIT_SOURCE).is_file():
                # この道具より前の版。仮想環境を作る前に断る（切り替えてから気づかない）
                raise PrepareFailed("unit")
            step = "venv"
            venv = directory / ".venv"
            self._run(
                [VENV_PYTHON, "-m", "venv", "--system-site-packages", str(venv)],
                timeout=VENV_TIMEOUT_S,
                check=True,
            )
            step = "pip"
            self._run(
                [str(venv / "bin/pip"), "install", "-e", f"{directory}[{PIP_EXTRAS}]"],
                timeout=PIP_TIMEOUT_S,
                check=True,
                cwd=directory,
            )
            (venv / READY_MARKER).write_text(sha, encoding="utf-8")
        except (OSError, CommandError):
            raise PrepareFailed(step) from None

    def prepare(self, target: Target, repo_url: str) -> Path:
        """版のディレクトリを用意して、その道を返す。

        同じ sha でできていれば使い回す。途中で失敗したら、その版のディレクトリだけを消す
        （今の版とほかの版には触れない）。
        """
        if target.kind != "release" or target.sha is None:
            raise ValueError("版のディレクトリを作れるのは、版だけです")
        sha = target.sha
        directory = self._layout.release_dir(sha)
        if self._reusable(directory, sha):
            if not (directory / UNIT_SOURCE).is_file():
                raise PrepareFailed("unit")
            os.utime(directory)  # 使い回した版も、新しい順の前に来る
            return directory
        if self._same_path(directory, self.current()):
            # 今の版は作り直さない（動いているサービスの足元を消さない）
            raise PrepareFailed("worktree")
        self._fetch(repo_url)
        try:
            self._run(
                self._git("cat-file", "-e", f"{sha}^{{commit}}"),
                timeout=GIT_TIMEOUT_S,
                check=True,
            )
        except CommandError:
            raise PrepareFailed("commit") from None
        if directory.exists() or directory.is_symlink():
            self._remove_release(directory)  # 作りかけや、違う中身のもの
        self._layout.releases_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._build(directory, sha)
        except PrepareFailed:
            self._remove_release(directory)
            self._prune_worktrees()
            raise
        os.utime(directory)
        return directory

    # --- 切り替え（1.1、4.2） ---

    def current(self) -> Path | None:
        """current のリンクの先。リンクがなければ None。"""
        link = self._layout.current_link
        if not link.is_symlink():
            return None
        target = Path(os.readlink(link))
        return target if target.is_absolute() else link.parent / target

    def switch(self, release_dir: Path) -> Path | None:
        """current を `release_dir` に向け、前の先を返す。

        一時名のリンクを作ってから置き換えるので、途中で止まってもリンクは前か後のどちらか。
        """
        self._check_release_dir(release_dir)
        link = self._layout.current_link
        previous = self.current()
        tmp = link.parent / _LINK_TMP_NAME
        try:
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            os.symlink(release_dir, tmp)
            os.replace(tmp, link)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise ReleaseError("今の版のリンクを切り替えられませんでした") from None
        _fsync_dir(link.parent)
        return previous

    def install_unit(self, release_dir: Path) -> None:
        """版のユニットのファイルを写し、systemd に読み直させる。"""
        self._check_release_dir(release_dir)
        source = release_dir / UNIT_SOURCE
        if not source.is_file():
            raise ReleaseError("版にユニットのファイルがありません")
        unit_dir = self._layout.user_unit_dir
        tmp = unit_dir / _UNIT_TMP_NAME
        try:
            unit_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, tmp)
            os.replace(tmp, self._layout.core_unit_file)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise ReleaseError("ユニットのファイルを写せませんでした") from None
        daemon_reload(self._run)

    # --- 手の変更（1.9） ---

    def dirty_count(self, release_dir: Path) -> int:
        """版のディレクトリの、手の変更の件数。ファイルの名前は返さない。

        worktree の印がなければ数えられないので `ReleaseError` にする。
        """
        self._check_release_dir(release_dir)
        if not self._has_worktree_link(release_dir):
            raise ReleaseError("版のディレクトリが worktree ではありません")
        return git_dirty_count(self._run, release_dir)

    # --- 片付け（3.6） ---

    def _remove_release(self, directory: Path) -> None:
        """版のディレクトリを 1 つ消す。版の置き場所の外には触れない。"""
        if directory.parent != self._layout.releases_dir:
            raise ValueError("版の置き場所の外は消しません")
        if directory.is_symlink():
            directory.unlink()  # リンクはたどらない
            return
        if not directory.exists():
            return
        if self._repo_exists():
            with contextlib.suppress(CommandError):
                self._run(
                    self._git("worktree", "remove", "--force", str(directory)),
                    timeout=GIT_TIMEOUT_S,
                    check=True,
                )
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)

    def _prune_worktrees(self) -> None:
        if self._repo_exists():
            with contextlib.suppress(CommandError):
                self._run(self._git("worktree", "prune"), timeout=GIT_TIMEOUT_S, check=True)

    def prune(self, keep: Collection[Path]) -> None:
        """古い版を消す。

        残すのは、`keep`（前の版・普段の版など）と今の版と、新しい順に `KEEP_RELEASES` 個。
        版の名前でないもの・リンクには触れない。
        """
        releases_dir = self._layout.releases_dir
        if not releases_dir.is_dir():
            return
        candidates = [path for path in releases_dir.iterdir() if self._is_release_dir(path)]
        kept = {os.path.realpath(path) for path in keep}
        current = self.current()
        if current is not None:
            kept.add(os.path.realpath(current))
        newest = sorted(candidates, key=lambda path: path.lstat().st_mtime, reverse=True)
        kept.update(os.path.realpath(path) for path in newest[:KEEP_RELEASES])
        removed = False
        for path in sorted(candidates):
            if os.path.realpath(path) in kept:
                continue
            self._remove_release(path)
            removed = True
        if removed:
            self._prune_worktrees()
