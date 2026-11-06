"""実機の初期設定（8.1〜8.8）。

手順ごとに「済んでいるか」と「行う」を持ち、何度実行してもよいようにする。
済んでいる手順は飛ばし、失敗したらそこで止めて、やり直し方を出す。
再起動が要る手順を行ったら最後に知らせる。自分では再起動しない。

書かないもの（8.5、8.6、9.1）:
- サンプルの場所（作業コピー・仮想環境・ユニット）
- 基盤の設定のディレクトリ（`~/.config/osekkai/`）。秘密のファイルは作らず、中も読まない
- 声のファイルと顔のモデルのうち、すでにあるもの（sha256 が違っても上書きしない）

`sudo` のパスワードは、端末を割り当てた ssh で利用者が入力する。道具はパスワードを扱わない。
表示には、固定の文言と、例外の型の名前だけを出す（外のコマンドの出力は写さない）。
"""

from __future__ import annotations

import contextlib
import filecmp
import hashlib
import http.client
import os
import shutil
import tempfile
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from rover_deploy.common import Running, Target
from rover_deploy.layout import Layout, ensure_private_dir
from rover_deploy.releases import UNIT_SOURCE, PrepareFailed, Releases
from rover_deploy.service_status import detect_running
from rover_deploy.state import exception_reason, make_deploy_id
from rover_deploy.system import (
    CHANGE_TIMEOUT_S,
    QUERY_TIMEOUT_S,
    SYSTEMCTL,
    CommandError,
    Runner,
    daemon_reload,
    enable_unit,
    is_active,
    start_unit,
)
from rover_deploy.verify import SELF_CHECK_TIMEOUT_S, parse_self_check

# 手順の並び（設計の Provision）。reminder は版のユニットのファイルを写すので、release の後に置く
PROVISION_STEPS = (
    "os",
    "apt",
    "boot_config",
    "groups",
    "linger",
    "layout",
    "voices",
    "models",
    "release",
    "config",
    "reminder",
    "secrets",
    "self_check",
)

SUPPORTED_CODENAMES = ("trixie", "bookworm")
SUPPORTED_MACHINE = "aarch64"
SUPPORTED_OS_TEXT = "対応している OS: Debian 13（trixie）か 12（bookworm）の aarch64（64 ビット）"

DEFAULT_OS_RELEASE = Path("/etc/os-release")
DEFAULT_BOOT_CONFIG = Path("/boot/firmware/config.txt")

APT_PACKAGES = (
    "git",
    "python3-venv",
    "python3-lgpio",
    "python3-numpy",
    "python3-smbus2",
    "python3-yaml",
    "python3-opencv",
    "python3-picamera2",
    "i2c-tools",
    "pipewire-pulse",
    "pulseaudio-utils",
    "open-jtalk",
    "open-jtalk-mecab-naist-jdic",
)
APT_INSTALLED = "install ok installed"
APT_TIMEOUT_S = 1800.0
# 入れる・消すと、カーネルや起動の区画のファイル（initramfs を含む）を書き換えるパッケージ
# （名前の頭）。これが入るか消えるなら止める。既にあるものが新しくなるときも、すべて止める
BOOT_PACKAGE_PREFIXES = (
    "linux-image",
    "raspi-firmware",
    "raspberrypi-kernel",
    "raspberrypi-bootloader",
    "rpi-eeprom",
    "initramfs-tools",
    "dracut",
    "firmware-",
    "udev",
    "systemd",
    "kmod",
    "busybox",
    "klibc-utils",
    "plymouth",
)
MSG_APT_TOUCHES_BOOT = (
    "パッケージを入れると、既にあるものか、カーネル・起動のファイルも変わります。入れずに止めました"
)

# sudo は環境変数を渡さないので、env で付ける（設定の問い合わせで止まらない）
APT_GET = ("sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get")

BOOT_LINES = ("dtparam=i2c_arm=on", "dtoverlay=hifiberry-dac")
BOOT_BACKUP_INFIX = ".osekkai-bak-"
# 条件の節（[pi4] など）の中に入らないよう、足す行の前に置く
BOOT_SECTION_ALL = "[all]"

GROUPS = ("gpio", "i2c", "audio")
# journal を読めるグループ。どちらかに入っていればよい。なければ前のものに入れる
JOURNAL_GROUPS = ("systemd-journal", "adm")

# 声のファイルと顔のモデル（データのディレクトリの下）
VOICES_DIR_NAME = "voices"
MODELS_DIR_NAME = "models"
FETCH_TIMEOUT_S = 60.0  # 取得の 1 回の読み書きを待つ時間（全体ではない）
_FETCH_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Asset:
    """取得元と sha256 を固定したファイル。"""

    dir_name: str  # データのディレクトリの下の、置き場所の名前
    file_name: str
    url: str
    sha256: str


_VOICE_BASE = "https://raw.githubusercontent.com/icn-lab/htsvoice-tohoku-f01/master"
_ZOO_BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"

# 実機にあるファイルと同じ sha256（2026-11-05 に確かめた）
ASSETS = (
    Asset(
        VOICES_DIR_NAME,
        "tohoku-f01-angry.htsvoice",
        f"{_VOICE_BASE}/tohoku-f01-angry.htsvoice",
        "c246afac0d41ada4010db80ca673b6b51cb8e16e9f3a47487d0df3d2ca5b7ec8",
    ),
    Asset(
        VOICES_DIR_NAME,
        "tohoku-f01-happy.htsvoice",
        f"{_VOICE_BASE}/tohoku-f01-happy.htsvoice",
        "6bf9559a0757c5250165de40dda034a7e95504f3a3ce4abce58532cee1040cc0",
    ),
    Asset(
        VOICES_DIR_NAME,
        "tohoku-f01-neutral.htsvoice",
        f"{_VOICE_BASE}/tohoku-f01-neutral.htsvoice",
        "ded6acb4243b93cc50896199d67b00a7c487662bfcdcd3f9f30a4fb0444b0b46",
    ),
    Asset(
        VOICES_DIR_NAME,
        "tohoku-f01-sad.htsvoice",
        f"{_VOICE_BASE}/tohoku-f01-sad.htsvoice",
        "1c76d4d5646c46e13410651c4232b95af8b878e67afd370a605b5de792ba17ee",
    ),
    Asset(
        VOICES_DIR_NAME,
        "COPYRIGHT.txt",
        f"{_VOICE_BASE}/COPYRIGHT.txt",
        "f7f7a1a696f062704825fcc495d330beeeac8c0ed38d8f5bdd8e90bdc3471c95",
    ),
    Asset(
        MODELS_DIR_NAME,
        "face_detection_yunet_2023mar.onnx",
        f"{_ZOO_BASE}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    Asset(
        MODELS_DIR_NAME,
        "face_recognition_sface_2021dec.onnx",
        f"{_ZOO_BASE}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
)

# 取得の口: URL の中身を out に書く。テストでは偽物に差し替える
Fetch = Callable[[str, BinaryIO, float], None]


def urllib_fetch(url: str, out: BinaryIO, timeout: float) -> None:
    """標準ライブラリで https から取る（転送の先もたどる）。"""
    request = urllib.request.Request(url, headers={"User-Agent": "rover-deploy"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # 固定の https だけ
        shutil.copyfileobj(response, out, _FETCH_CHUNK)


REMINDER_SERVICE = "osekkai-trial-reminder.service"
REMINDER_TIMER = "osekkai-trial-reminder.timer"
REMINDER_UNITS = (REMINDER_SERVICE, REMINDER_TIMER)
UNIT_SOURCE_DIR = Path("deploy/systemd")

SECRETS_FILE_NAME = "env"

REBOOT_STEP = "reboot"
REBOOT_TEXT = (
    "再起動が要ります。準備ができたら、実機で sudo reboot を実行します（道具は再起動しません）"
)
RECHECK_TEXT = (
    "再起動の前の self-check なので、再起動の後に rover-deploy self-check で確かめ直します"
)


def shown_path(path: Path, home: Path) -> str:
    """表示に使う道。家のディレクトリは ~ にする（利用者の名前を出さない）。"""
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


class StepFailed(Exception):
    """手順を行えなかった。文言は決まったものだけ（設定の値・名前・出力を入れない）。"""


@dataclass(frozen=True)
class Step:
    name: str
    is_done: Callable[[], bool]
    apply: Callable[[], None]
    needs_reboot: bool = False
    retry_hint: str = ""  # 失敗したときの、やり直し方
    # 行った後に結果を返す口（self-check のよい・だめ）。行っていなければ None を返す
    outcome: Callable[[], bool | None] | None = None


@dataclass(frozen=True)
class ProvisionReport:
    ok: bool
    skipped: tuple[str, ...]  # 済んでいたので飛ばした手順
    applied: tuple[str, ...]  # 行った手順
    failed_step: str | None
    retry_hint: str
    needs_reboot: bool
    self_check_ok: bool | None = None  # self-check がすべてよいか。行えなければ None


def run_provision(steps: Sequence[Step], log: Callable[[str, str], None]) -> ProvisionReport:
    """手順を順に行う。済んでいるものは飛ばし、最初の失敗で止める（8.2〜8.4）。"""
    skipped: list[str] = []
    applied: list[str] = []
    reboot = False
    self_check_ok: bool | None = None
    failed: Step | None = None
    for step in steps:
        try:
            if step.is_done():
                skipped.append(step.name)
                log(step.name, "済み")
                continue
            log(step.name, "行います")
            step.apply()
        except StepFailed as error:
            log(step.name, f"失敗しました: {error}")
            failed = step
        except Exception as error:  # 型の名前だけを出して止める
            log(step.name, exception_reason("失敗しました", error))
            failed = step
        if failed is not None:
            if step.retry_hint:
                log(step.name, f"やり直し方: {step.retry_hint}")
            break
        applied.append(step.name)
        reboot = reboot or step.needs_reboot
        if step.outcome is not None:
            self_check_ok = step.outcome()
        log(step.name, "できました")
    if reboot:
        log(REBOOT_STEP, REBOOT_TEXT)
        if self_check_ok is not None:
            log(REBOOT_STEP, RECHECK_TEXT)
    return ProvisionReport(
        ok=failed is None,
        skipped=tuple(skipped),
        applied=tuple(applied),
        failed_step=None if failed is None else failed.name,
        retry_hint="" if failed is None else failed.retry_hint,
        needs_reboot=reboot,
        self_check_ok=self_check_ok,
    )


@dataclass(frozen=True)
class ProvisionContext:
    """初期設定に要るもの。テストでは置き場所と Runner を差し替える。"""

    run: Runner
    layout: Layout
    releases: Releases
    user: str  # 実機の利用者の名前（表示には出さない）
    repo_url: str
    target: Target | None  # 最初の版。開発 PC が解決して渡す
    data_dir: Path  # 基盤のデータのディレクトリ（`backups.core_data_dir`）
    say: Callable[[str, str], None]  # 手順の中の案内を出す口（手順の名前, 文）
    os_release: Path = DEFAULT_OS_RELEASE
    boot_config: Path = DEFAULT_BOOT_CONFIG
    now: Callable[[], float] = field(default=time.time)
    fetch: Fetch = field(default=urllib_fetch)
    assets: tuple[Asset, ...] = ASSETS


# --- os（8.7） ---


def _os_codename(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "VERSION_CODENAME":
            return value.strip().strip("\"'")
    return None


def check_os(ctx: ProvisionContext) -> bool:
    """対応している OS か。読めなければ対応していないとする。"""
    try:
        machine = ctx.run(["uname", "-m"], timeout=QUERY_TIMEOUT_S, check=True).text.strip()
    except CommandError:
        return False
    return machine == SUPPORTED_MACHINE and _os_codename(ctx.os_release) in SUPPORTED_CODENAMES


def _os_step(ctx: ProvisionContext) -> Step:
    def apply() -> None:
        raise StepFailed(f"対応していない OS なので、何も変えずに止めます。{SUPPORTED_OS_TEXT}")

    return Step(
        "os",
        lambda: check_os(ctx),
        apply,
        retry_hint="対応している OS を SD カードに入れ直してから、もう一度 provision を実行します",
    )


# --- apt ---


def _missing_packages(ctx: ProvisionContext) -> list[str]:
    missing: list[str] = []
    for name in APT_PACKAGES:
        result = ctx.run(["dpkg-query", "-W", "-f=${Status}", name], timeout=QUERY_TIMEOUT_S)
        if result.returncode != 0 or result.text.strip() != APT_INSTALLED:
            missing.append(name)
    return missing


def _touches_boot(ctx: ProvisionContext, missing: list[str]) -> bool:
    """入れると、既にあるものが新しくなるか、起動に関わるものが入る・消えるか。

    sudo なしの試し（`apt-get -s`）で調べる。

    実機確認で、足りないパッケージを入れたときにカーネルなども更新され、
    その後の再起動で起動の区画が壊れていた。起動に関わる更新は、人が見ている所で行う。
    """
    result = ctx.run(["apt-get", "-s", "install", *missing], timeout=APT_TIMEOUT_S, check=True)
    for line in result.text.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] not in ("Inst", "Remv"):
            continue
        if parts[1].startswith(BOOT_PACKAGE_PREFIXES):
            return True
        # 既にあるものが新しくなる（"Inst 名前 [今の版] (新しい版 ...)"）。
        # udev などの更新でも initramfs が作り直され、起動の区画に書かれるので止める
        if parts[0] == "Inst" and len(parts) >= 3 and parts[2].startswith("["):
            return True
    return False


def _apt_step(ctx: ProvisionContext) -> Step:
    def apply() -> None:
        missing = _missing_packages(ctx)
        if not missing:
            return
        ctx.run([*APT_GET, "update"], timeout=APT_TIMEOUT_S, check=True)
        if _touches_boot(ctx, missing):
            raise StepFailed(MSG_APT_TOUCHES_BOOT)
        ctx.run([*APT_GET, "install", "-y", *missing], timeout=APT_TIMEOUT_S, check=True)

    return Step(
        "apt",
        lambda: not _missing_packages(ctx),
        apply,
        retry_hint="ネットワークを確かめ、実機で sudo apt-get update を行ってから、"
        "もう一度 provision を実行します。カーネルか起動のファイルで止まったときは、"
        "子どもが使っていない時間に、実機で sudo apt full-upgrade と再起動を済ませてから行います",
    )


# --- boot_config ---


def _active_boot_lines(text: str) -> set[str]:
    """どの機種でも効く行（注釈を除く）。

    最初の `[...]` の前か、`[all]` の後（次の条件の節まで）の行だけを数える。
    """
    active: set[str] = set()
    in_all = True  # 最初の節の前は、どの機種でも効く
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            in_all = line.lower() == BOOT_SECTION_ALL
            continue
        if in_all:
            active.add(line)
    return active


def _boot_missing(path: Path) -> list[str] | None:
    """足りない行。ファイルが読めなければ None。"""
    try:
        active = _active_boot_lines(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    return [line for line in BOOT_LINES if line not in active]


def _boot_step(ctx: ProvisionContext) -> Step:
    path = ctx.boot_config

    def apply() -> None:
        missing = _boot_missing(path)
        if missing is None:
            raise StepFailed("起動の設定のファイルを読めません")
        if not missing:
            return
        backup = f"{path}{BOOT_BACKUP_INFIX}{make_deploy_id(ctx.now())}"
        ctx.run(["sudo", "cp", "-p", str(path), backup], timeout=CHANGE_TIMEOUT_S, check=True)
        added = "\n" + "\n".join([BOOT_SECTION_ALL, *missing]) + "\n"
        ctx.run(
            ["sudo", "tee", "-a", str(path)],
            input=added.encode("utf-8"),
            timeout=CHANGE_TIMEOUT_S,
            check=True,
        )

    return Step(
        "boot_config",
        lambda: _boot_missing(path) == [],
        apply,
        needs_reboot=True,
        retry_hint=f"{path} があるかを確かめてから、もう一度 provision を実行します"
        f"（足す前の写しは {path.name}{BOOT_BACKUP_INFIX}<時刻> にあります）",
    )


# --- groups ---


def _missing_groups(ctx: ProvisionContext) -> list[str]:
    text = ctx.run(["id", "-nG", ctx.user], timeout=QUERY_TIMEOUT_S, check=True).text
    current = set(text.split())
    missing = [name for name in GROUPS if name not in current]
    if not current.intersection(JOURNAL_GROUPS):
        missing.append(JOURNAL_GROUPS[0])
    return missing


def _groups_step(ctx: ProvisionContext) -> Step:
    def apply() -> None:
        missing = _missing_groups(ctx)
        if not missing:
            return
        ctx.run(
            ["sudo", "usermod", "-aG", ",".join(missing), ctx.user],
            timeout=CHANGE_TIMEOUT_S,
            check=True,
        )

    return Step(
        "groups",
        lambda: not _missing_groups(ctx),
        apply,
        needs_reboot=True,
        retry_hint="sudo のパスワードを確かめてから、もう一度 provision を実行します",
    )


# --- linger ---


def _linger_on(ctx: ProvisionContext) -> bool:
    argv = ["loginctl", "show-user", ctx.user, "-p", "Linger", "--value"]
    return ctx.run(argv, timeout=QUERY_TIMEOUT_S, check=True).text.strip() == "yes"


def _linger_step(ctx: ProvisionContext) -> Step:
    def apply() -> None:
        ctx.run(
            ["sudo", "loginctl", "enable-linger", ctx.user], timeout=CHANGE_TIMEOUT_S, check=True
        )

    return Step(
        "linger",
        lambda: _linger_on(ctx),
        apply,
        retry_hint="sudo のパスワードを確かめてから、もう一度 provision を実行します",
    )


# --- layout ---


def _layout_dirs(layout: Layout) -> tuple[Path, ...]:
    return (layout.root, layout.releases_dir, layout.tool_dir, layout.state_dir)


def _is_private_dir(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return path.is_dir() and not path.is_symlink() and (info.st_mode & 0o777) == 0o700


def _layout_step(ctx: ProvisionContext) -> Step:
    def apply() -> None:
        for path in _layout_dirs(ctx.layout):
            ensure_private_dir(path)  # 中のファイルには触れない

    return Step(
        "layout",
        lambda: all(_is_private_dir(path) for path in _layout_dirs(ctx.layout)),
        apply,
        retry_hint="家のディレクトリの空きと権限を確かめてから、もう一度 provision を実行します",
    )


# --- voices・models（取得元と sha256 を固定。8.5: すでにあるファイルは上書きしない） ---


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_FETCH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(ctx: ProvisionContext, asset: Asset, directory: Path) -> None:
    """一時のファイルに取り、sha256 が合えば置き換える。合わなければ一時のファイルを消す。"""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{asset.file_name}.", suffix=".part")
    tmp = Path(tmp_name)
    try:
        try:
            with os.fdopen(fd, "wb") as out:
                ctx.fetch(asset.url, out, FETCH_TIMEOUT_S)
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise StepFailed(
                exception_reason(f"{asset.file_name} を取得できませんでした", error)
            ) from None
        if _file_sha256(tmp) != asset.sha256:
            raise StepFailed(f"取得した {asset.file_name} の sha256 が違うので、置きません")
        os.replace(tmp, directory / asset.file_name)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _assets_step(ctx: ProvisionContext, name: str) -> Step:
    directory = ctx.data_dir / name
    assets = [asset for asset in ctx.assets if asset.dir_name == name]
    warned: set[str] = set()

    def present(asset: Asset) -> bool:
        path = directory / asset.file_name
        if not path.is_file():
            return False
        if asset.file_name not in warned and _file_sha256(path) != asset.sha256:
            warned.add(asset.file_name)
            ctx.say(name, f"{asset.file_name} は sha256 が違いますが、上書きせずにそのまま使います")
        return True

    def is_done() -> bool:
        return all([present(asset) for asset in assets])  # どれも確かめて警告を出す

    def apply() -> None:
        for asset in assets:
            if not present(asset):
                _download(ctx, asset, directory)

    return Step(
        name,
        is_done,
        apply,
        retry_hint="ネットワークを確かめてから、もう一度 provision を実行します",
    )


# --- release ---


def _current(ctx: ProvisionContext) -> Path | None:
    current = ctx.releases.current()
    if current is None or not current.is_dir():
        return None
    return current


def _release_step(ctx: ProvisionContext) -> Step:
    def usable(current: Path | None) -> bool:
        # 前の実行でユニットのない版に向いたまま止まったときは、指定の版で作り直す
        return current is not None and (current / UNIT_SOURCE).is_file()

    def is_done() -> bool:
        return usable(_current(ctx)) and ctx.layout.core_unit_file.is_file()

    def apply() -> None:
        current = _current(ctx)
        if current is None or not usable(current):
            if ctx.target is None or ctx.target.kind != "release":
                raise StepFailed("最初の版の指定がありません")
            try:
                current = ctx.releases.prepare(ctx.target, ctx.repo_url)
            except PrepareFailed as error:
                raise StepFailed(str(error)) from None  # 決まった文言だけを出す
            ctx.releases.switch(current)
        # ユニットを置いて読み直させるだけ。起動も有効化もしない
        ctx.releases.install_unit(current)

    return Step(
        "release",
        is_done,
        apply,
        retry_hint=(
            "ネットワークを確かめるか、新しい基盤に対応した版を "
            "provision --ref <タグかブランチ> で指定して、もう一度実行します"
        ),
    )


# --- config ---

CONFIG_FILE_NAME = "config.yaml"
# 基盤の設定の見本（版の中の場所。rover-core の EXAMPLE_RESOURCE）
CONFIG_EXAMPLE = Path("src/osekkai/core/example_config.yaml")


def _config_step(ctx: ProvisionContext) -> Step:
    """基盤の設定ファイルがなければ、今の版の見本から置く。あれば触らない。

    self-check は確かめるだけでファイルを作らないので、初期設定が置く（validate-impl で決めた）。
    """
    config = ctx.layout.core_config_dir / CONFIG_FILE_NAME

    def apply() -> None:
        current = _current(ctx)
        example = None if current is None else current / CONFIG_EXAMPLE
        if example is None or not example.is_file():
            raise StepFailed("今の版に設定の見本がありません")
        ensure_private_dir(ctx.layout.core_config_dir)
        tmp = config.with_name(f".{CONFIG_FILE_NAME}.tmp")
        shutil.copyfile(example, tmp)
        os.replace(tmp, config)

    return Step(
        "config",
        config.is_file,
        apply,
        retry_hint="release の手順が済んでいるかを確かめてから、もう一度 provision を実行します",
    )


# --- reminder ---


def _reminder_sources(ctx: ProvisionContext) -> list[Path] | None:
    current = _current(ctx)
    if current is None:
        return None
    return [current / UNIT_SOURCE_DIR / name for name in REMINDER_UNITS]


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.is_file() and b.is_file() and filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def _reminder_done(ctx: ProvisionContext) -> bool:
    sources = _reminder_sources(ctx)
    if sources is None:
        return False
    unit_dir = ctx.layout.user_unit_dir
    if not all(_same_file(source, unit_dir / source.name) for source in sources):
        return False
    argv = [*SYSTEMCTL, "is-enabled", REMINDER_TIMER]
    enabled = ctx.run(argv, timeout=QUERY_TIMEOUT_S).text.strip() == "enabled"
    return enabled and is_active(ctx.run, REMINDER_TIMER)


def _reminder_step(ctx: ProvisionContext) -> Step:
    def apply() -> None:
        sources = _reminder_sources(ctx)
        if sources is None:
            raise StepFailed("今の版がないので、タイマーのファイルを写せません")
        if not all(source.is_file() for source in sources):
            raise StepFailed("今の版に、タイマーのファイルがありません")
        unit_dir = ctx.layout.user_unit_dir
        unit_dir.mkdir(parents=True, exist_ok=True)
        for source in sources:
            tmp = unit_dir / f".{source.name}.tmp"
            tmp.write_bytes(source.read_bytes())
            os.replace(tmp, unit_dir / source.name)
        daemon_reload(ctx.run)
        enable_unit(ctx.run, REMINDER_TIMER)
        start_unit(ctx.run, REMINDER_TIMER)

    return Step(
        "reminder",
        lambda: _reminder_done(ctx),
        apply,
        retry_hint="release の手順が済んでいるかを確かめてから、もう一度 provision を実行します",
    )


# --- secrets（8.6） ---


def secrets_file(layout: Layout) -> Path:
    return layout.core_config_dir / SECRETS_FILE_NAME


def secrets_guidance(layout: Layout) -> list[str]:
    """API キーなどを置く場所と手順。道具は作らない・中を読まない。"""
    path = shown_path(secrets_file(layout), layout.home)
    parent = shown_path(secrets_file(layout).parent, layout.home)
    return [
        f"API キーなどは {path} に、1 行に 1 つ「名前=値」で書きます",
        f"本人だけが読めるようにします: mkdir -p -m 700 {parent} && "
        f"touch {path} && chmod 0600 {path}",
        "このファイルはリポジトリに入れません。道具は作らず、中も読みません",
    ]


def _secrets_step(ctx: ProvisionContext) -> Step:
    def apply() -> None:
        for line in secrets_guidance(ctx.layout):
            ctx.say("secrets", line)

    return Step(
        "secrets",
        lambda: secrets_file(ctx.layout).is_file(),  # あるかだけを見る。中は読まない
        apply,
        retry_hint="表示された場所に、開発者が自分でファイルを置きます",
    )


# --- self_check（8.8） ---


def _sample_may_run(ctx: ProvisionContext) -> bool:
    """サンプルが動いているかもしれないか。分からなければ動いているとする。"""
    try:
        running = detect_running(ctx.run)
    except CommandError:
        return True
    return running in (Running.SAMPLE, Running.BOTH)


def run_self_check(ctx: ProvisionContext) -> tuple[str, ...]:
    """新しい基盤の self-check を行い、だめの項目の名前を返す。

    サンプルが動いていれば `--no-hardware` を付ける。基盤のサービスは起動しない
    （self-check がロックを持っている間に起動すると、サービスが 75 で終わるため）。
    """
    current = _current(ctx)
    if current is None:
        raise StepFailed("今の版がないので、self-check を行えません")
    argv = [str(current / ".venv/bin/python"), "-m", "osekkai", "--self-check", "--json"]
    if _sample_may_run(ctx):
        argv.append("--no-hardware")
    try:
        result = ctx.run(argv, timeout=SELF_CHECK_TIMEOUT_S, cwd=current)
    except CommandError as error:
        raise StepFailed(exception_reason("self-check を行えませんでした", error)) from None
    failed = parse_self_check(result.text)
    if failed is None or result.returncode != (1 if failed else 0):
        raise StepFailed("self-check の結果を読めませんでした")
    return failed


def _self_check_step(ctx: ProvisionContext) -> Step:
    """self-check を行い、結果を出す（8.8）。

    だめの項目があっても手順は失敗にしない（結果は報告の self_check_ok に入れる）。
    失敗にするのは、行えないときと結果を読めないときだけ。
    """
    result: list[bool] = []

    def apply() -> None:
        result.clear()
        failed = run_self_check(ctx)
        result.append(not failed)
        if failed:
            ctx.say("self_check", f"self-check にだめの項目があります: {', '.join(failed)}")
        else:
            ctx.say("self_check", "self-check はすべてよいでした")

    return Step(
        "self_check",
        lambda: False,  # 毎回行う
        apply,
        retry_hint="release の手順が済んでいるかを確かめてから、"
        "rover-deploy self-check で確かめます",
        outcome=lambda: result[-1] if result else None,
    )


def provision_steps(ctx: ProvisionContext) -> list[Step]:
    """初期設定の手順の並び（`PROVISION_STEPS` の順）。"""
    return [
        _os_step(ctx),
        _apt_step(ctx),
        _boot_step(ctx),
        _groups_step(ctx),
        _linger_step(ctx),
        _layout_step(ctx),
        _assets_step(ctx, VOICES_DIR_NAME),
        _assets_step(ctx, MODELS_DIR_NAME),
        _release_step(ctx),
        _config_step(ctx),
        _reminder_step(ctx),
        _secrets_step(ctx),
        _self_check_step(ctx),
    ]
