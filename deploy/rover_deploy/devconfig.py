"""開発 PC の利用者の設定（3.3、10.1、10.2）。

接続先の別名・バックアップの置き場所・リポジトリの URL を、リポジトリの外の
利用者の設定ファイルから読む。誤りの文言は決まった文だけにし、設定の値や
ファイルの場所は出さない。
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from rover_deploy.common import ExitCode
from rover_deploy.system import QUERY_TIMEOUT_S, CommandError, Runner

CONFIG_ENV = "OSEKKAI_DEPLOY_CONFIG"
DEFAULT_CONFIG = Path(".config") / "osekkai-deploy" / "config.toml"  # 家のディレクトリから
DEFAULT_BACKUP_DIR = Path(".local") / "share" / "osekkai-deploy" / "backups"

GUIDANCE = (
    "実機の接続先が設定されていません。\n"
    f"設定のファイル（環境変数 {CONFIG_ENV} の場所、なければ ~/.config/osekkai-deploy/config.toml）"
    "に、次のように書いてください:\n"
    '  host = "rover"  # ssh の接続先。~/.ssh/config の別名を勧めます。必須\n'
    '  backup_dir = "~/.local/share/osekkai-deploy/backups"  # 省略できます\n'
    '  repo_url = "https://github.com/<owner>/<repo>.git"  # 省略できます。'
    "省略なら origin から作ります"
)

# ssh の引数に渡すので、使える文字を絞る。先頭の "-" は ssh のオプションと見なされるので断る
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,252}")
_HOST_AFTER_AT_RE = re.compile(r"[A-Za-z0-9]")
_URL_HOST_RE = re.compile(r"[A-Za-z0-9.-]+")
# git@github.com:owner/repo.git の形（scp の形）
_SCP_RE = re.compile(r"(?:[^@/:\s]+@)?(?P<host>[^@/:\s]+):(?P<path>[^/\s]\S*)")


_UNREADABLE_DIR = (
    "バックアップの置き場所（backup_dir）を確かめられません（権限やリンクを見てください）"
)


class ConfigError(Exception):
    """設定の誤り。文言は決まった文だけで、終了コードは中止（10）。"""

    exit_code = ExitCode.ABORTED


@dataclass(frozen=True)
class DevConfig:
    """開発 PC の利用者の設定。"""

    host: str  # ssh の接続先
    backup_dir: Path  # 開発 PC のバックアップの置き場所（作業コピーの外）
    repo_url: str  # 実機が版を取る https の URL


def _expand_home(value: str, home: Path) -> Path | None:
    """先頭の "~" を家のディレクトリにする。"~other" の形は扱わず None。"""
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    if value.startswith("~"):
        return None
    return Path(value)


def _config_path(env: Mapping[str, str], home: Path) -> Path:
    value = env.get(CONFIG_ENV, "")
    if not value:
        return home / DEFAULT_CONFIG
    path = _expand_home(value, home)
    if path is None:
        raise ConfigError(f"環境変数 {CONFIG_ENV} の場所が読めません")
    return path


def _read_config(path: Path) -> dict[str, object]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise ConfigError(GUIDANCE) from None
    except OSError:
        raise ConfigError("設定のファイルを読めません") from None
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ConfigError("設定のファイルの書き方が違います（TOML として読めません）") from None


def _check_host(value: object) -> str:
    if value is None or value == "":
        raise ConfigError(GUIDANCE)
    if (
        not isinstance(value, str)
        or not _HOST_RE.fullmatch(value)
        or not _HOST_AFTER_AT_RE.match(value.rsplit("@", 1)[-1])
    ):
        raise ConfigError(
            "接続先（host）に使えない文字があります。英数字と . _ - @ だけにし、"
            "先頭は英数字にしてください"
        )
    return value


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):  # 権限がない・リンクの輪（3.11）
        raise ConfigError(_UNREADABLE_DIR) from None


def _backup_dir(value: object, home: Path) -> Path:
    if value is None:
        return _resolve(home / DEFAULT_BACKUP_DIR)
    if not isinstance(value, str) or not value:
        raise ConfigError("バックアップの置き場所（backup_dir）は文字で書いてください")
    path = _expand_home(value, home)
    if path is None or not path.is_absolute():
        raise ConfigError(
            "バックアップの置き場所（backup_dir）は、/ か ~/ で始まる場所にしてください"
        )
    return _resolve(path)


def _nearest_existing(path: Path) -> Path:
    """いちばん近いある親。ディレクトリでなければ ConfigError。"""
    current = path
    try:
        while not current.exists() and current != current.parent:
            current = current.parent
        is_dir = current.is_dir()
    except (OSError, RuntimeError):
        raise ConfigError(_UNREADABLE_DIR) from None
    if not is_dir:
        raise ConfigError("バックアップの置き場所（backup_dir）がディレクトリではありません")
    return current


def _check_outside_work_tree(backup_dir: Path, repo_root: Path, run: Runner) -> None:
    """バックアップの置き場所が git の作業コピーの中なら断る（10.2）。"""
    inside = (
        "バックアップの置き場所（backup_dir、書かないときの既定の場所）が git の作業コピーの"
        "中です。設定のファイルに backup_dir を書いて、作業コピーの外にしてください"
    )
    if backup_dir.is_relative_to(_resolve(repo_root)):
        raise ConfigError(inside)
    existing = _nearest_existing(backup_dir)
    argv = ["git", "-C", str(existing), "rev-parse", "--is-inside-work-tree"]
    try:
        result = run(argv, timeout=QUERY_TIMEOUT_S, check=False)
    except CommandError:
        raise ConfigError(
            "バックアップの置き場所が git の作業コピーの外かを確かめられません"
        ) from None
    # 0 で終われば作業コピーの中（"true"）か、.git や bare の写しの中（"false"）
    if result.returncode == 0:
        raise ConfigError(inside)


def _url_host(parts: SplitResult) -> str | None:
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host or not _URL_HOST_RE.fullmatch(host):
        return None
    return host if port is None else f"{host}:{port}"


def _has_control(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def _https(host: str, path: str) -> str | None:
    if not path.strip("/") or any(ch.isspace() for ch in path):
        return None
    return f"https://{host}/{path.lstrip('/')}"


def _is_plain_https(value: str) -> bool:
    """名前や鍵・問い合わせ・断片・制御文字を含まない https:// の URL か。"""
    if not value.startswith("https://") or _has_control(value):
        return False
    parts = urlsplit(value)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return False
    if parts.query or parts.fragment or "?" in value or "#" in value:
        return False
    host = _url_host(parts)
    return host is not None and _https(host, parts.path) is not None


def https_from_origin(origin: str) -> str:
    """origin の URL から、実機が取る https の URL を作る。作れなければ ConfigError。

    ssh の形は https に直し、https の形は名前と鍵（user:token@）を取り除く。
    """
    need = "origin から https の URL を作れません。設定のファイルに repo_url を書いてください"
    text = origin.strip()
    url: str | None = None
    match = _SCP_RE.fullmatch(text)
    if match and "://" not in text:
        url = _from_ssh_host(match["host"], match["path"])
    elif (
        text.startswith(("ssh://", "https://"))
        and not _has_control(text)
        and "?" not in text
        and "#" not in text
    ):
        parts = urlsplit(text)
        host = _url_host(parts)
        if host is not None and parts.scheme == "ssh":
            # ssh の port は https には使わない
            url = _from_ssh_host(host.split(":")[0], parts.path)
        elif host is not None:
            url = _https(host, parts.path)
    # 作った URL も repo_url と同じ決まりで確かめる
    if url is None or not _is_plain_https(url):
        raise ConfigError(need)
    return url


def _from_ssh_host(host: str, path: str) -> str | None:
    # 点のない名前は ~/.ssh/config の別名かもしれず、https の名前にできない
    if not _URL_HOST_RE.fullmatch(host) or "." not in host:
        return None
    return _https(host, path)


def _check_repo_url(value: object) -> str:
    message = "リポジトリの URL（repo_url）は、名前や鍵を含まない https:// の URL にしてください"
    if not isinstance(value, str) or not _is_plain_https(value):
        raise ConfigError(message)
    return value


def _origin_url(repo_root: Path, run: Runner) -> str:
    argv = ["git", "-C", str(repo_root), "remote", "get-url", "origin"]
    try:
        text = run(argv, timeout=QUERY_TIMEOUT_S, check=True).text
    except CommandError:
        raise ConfigError(
            "origin の URL を読めません。設定のファイルに repo_url を書いてください"
        ) from None
    return https_from_origin(text)


def load_dev_config(
    env: Mapping[str, str], repo_root: Path, run: Runner, home: Path | None = None
) -> DevConfig:
    """利用者の設定を読む。誤りは ConfigError（決まった文言、終了コード 10）。"""
    home = Path.home() if home is None else home
    data = _read_config(_config_path(env, home))
    host = _check_host(data.get("host"))
    backup_dir = _backup_dir(data.get("backup_dir"), home)
    repo_value = data.get("repo_url")
    repo_url = _check_repo_url(repo_value) if repo_value is not None else None
    _check_outside_work_tree(backup_dir, repo_root, run)
    if repo_url is None:
        repo_url = _origin_url(repo_root, run)
    return DevConfig(host=host, backup_dir=backup_dir, repo_url=repo_url)
