"""ラジコン（remote）の読み込みの向きを、ソースを読んで静的に確かめる（15.1）。

osekkai の中は決まった口だけを読む。ほかの機能は presence.api だけ、基盤のアダプタは
読まない。ハードのライブラリは決まったアダプタの関数の中だけ、Flask は web だけで読む。
web は本体（feature）の関数の中だけで読む（Flask やハードがなくても読み込みで失敗しない）。
"""

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

SRC_DIR = Path(__file__).resolve().parents[3] / "src"
REMOTE_DIR = SRC_DIR / "osekkai" / "features" / "remote"

REMOTE = "osekkai.features.remote"
OSEKKAI = "osekkai"
# osekkai の中で、どのモジュールも読んでよい口
ALLOWED = (
    REMOTE,
    "osekkai.core.feature",
    "osekkai.core.config",
    "osekkai.core.types",
    "osekkai.core.arbiter",
    "osekkai.core.clock",
    "osekkai.core.events",
    "osekkai.core.lcd_text",
    "osekkai.core.selfcheck",
    "osekkai.core.paths",
    "osekkai.features.presence.api",
)
# ロック・ログの支度・設定の読み込み・本物の時計は、道具（cli）だけが使う
# （設定のモデルの FeatureConfig や時計の型の Clock は誰でも読んでよい）
ENTRY_ONLY = (
    "osekkai.core.instance_lock",
    "osekkai.core.logging_setup",
    "osekkai.core.config.ConfigLoader",
    "osekkai.core.clock.SystemClock",
)
CLI = f"{REMOTE}.cli"
ENTRY_MODULES: frozenset[str] = frozenset({CLI})

# 決まったアダプタだけが、関数の中でハードのライブラリを読んでよい
SENSORS = f"{REMOTE}.adapters.sensors"
USB_CAMERA = f"{REMOTE}.adapters.usb_camera"
ADAPTER_LIBS: dict[str, frozenset[str]] = {
    SENSORS: frozenset({"smbus2", "lgpio"}),
    USB_CAMERA: frozenset({"cv2", "numpy"}),
}
# ハードと重い外のライブラリ（上の許しのほかは、どこでも読まない）
HARDWARE_LIBS = (
    "smbus2",
    "lgpio",
    "numpy",
    "cv2",
    "picamera2",
    "faster_whisper",
    "ctranslate2",
    "anthropic",
)

# Flask は web だけが読む（一番上でもよい）。web は本体の関数の中だけで読む
FLASK = "flask"
WEB = f"{REMOTE}.web"
FEATURE = f"{REMOTE}.feature"


class ImportRef(NamedTuple):
    name: str
    in_function: bool


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC_DIR).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_from(module: str, is_package: bool, node: ast.ImportFrom) -> list[str]:
    if node.level == 0:
        base = node.module or ""
    else:
        package = module.split(".") if is_package else module.split(".")[:-1]
        package = package[: len(package) - (node.level - 1)]
        base = ".".join([*package, *([node.module] if node.module else [])])
    # `from x import y` は x.y として見る（x の決まりも x.y の決まりも当てられる）
    return [f"{base}.{alias.name}" for alias in node.names]


def scan_imports(source: str, module: str, *, is_package: bool = False) -> list[ImportRef]:
    tree = ast.parse(source)
    found: list[ImportRef] = []

    def visit(node: ast.AST, in_func: bool) -> None:
        if isinstance(node, ast.Import):
            found.extend(ImportRef(a.name, in_func) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.extend(ImportRef(n, in_func) for n in _resolve_from(module, is_package, node))
        is_func = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
        for child in ast.iter_child_nodes(node):
            visit(child, in_func or is_func)

    visit(tree, False)
    return found


def _imports(path: Path) -> list[ImportRef]:
    return scan_imports(
        path.read_text(encoding="utf-8"),
        _module_name(path),
        is_package=path.name == "__init__.py",
    )


def _matches(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def _hardware_violation(module: str, ref: ImportRef) -> bool:
    allowed = ADAPTER_LIBS.get(module, frozenset())
    if ref.in_function and any(_matches(ref.name, lib) for lib in allowed):
        return False
    return any(_matches(ref.name, lib) for lib in HARDWARE_LIBS)


def _violation(module: str, ref: ImportRef) -> bool:
    name = ref.name
    if _matches(name, FLASK):
        return module != WEB
    if _hardware_violation(module, ref):
        return True
    if _matches(name, WEB):
        return not (module == FEATURE and ref.in_function)
    if any(_matches(name, entry) for entry in ENTRY_ONLY):
        return module not in ENTRY_MODULES
    if _matches(name, OSEKKAI):
        return not any(_matches(name, allowed) for allowed in ALLOWED)
    return False


def remote_violations(module: str, refs: list[ImportRef]) -> list[str]:
    return [ref.name for ref in refs if _violation(module, ref)]


REMOTE_FILES = sorted(REMOTE_DIR.rglob("*.py"))


def test_remote_のモジュールが見つかる() -> None:
    names = {_module_name(p) for p in REMOTE_FILES}
    assert {REMOTE, f"{REMOTE}.adapters"} <= names


@pytest.mark.parametrize("path", REMOTE_FILES, ids=_module_name)
def test_remote_は決まった向きだけを読み込む(path: Path) -> None:
    assert remote_violations(_module_name(path), _imports(path)) == []


# --- 見張りの仕組みそのものの確かめ ---

SERVICE = f"{REMOTE}.service"


def _check(source: str, module: str = SERVICE, *, is_package: bool = False) -> list[str]:
    return remote_violations(module, scan_imports(source, module, is_package=is_package))


def test_自分のパッケージは読んでよい() -> None:
    source = (
        "from . import config\n"
        "from .ports import SensorReader\n"
        "from osekkai.features.remote.texts import Texts\n"
        "import osekkai.features.remote.drive_rules\n"
    )
    assert _check(source) == []
    assert _check("from .feature import X\n", REMOTE, is_package=True) == []
    assert _check("from ..ports import Ports\n", SENSORS) == []


def test_基盤の決まった口は読んでよい() -> None:
    source = (
        "from osekkai.core.feature import FeatureContext, FeatureSpec\n"
        "from osekkai.core.config import FeatureConfig\n"
        "from osekkai.core.types import Priority, Resource, Face, Glyph\n"
        "from osekkai.core.arbiter import Lease\n"
        "from osekkai.core.clock import Clock\n"
        "from osekkai.core.events import QuietChanged\n"
        "from osekkai.core.lcd_text import center\n"
        "from osekkai.core import selfcheck\n"
        "from osekkai.core.paths import Paths, ensure_private_dir\n"
    )
    assert _check(source) == []


def test_見つけてあいさつの公開の口は読んでよい() -> None:
    source = (
        "from osekkai.features.presence.api import borrow_camera, CameraBusy\n"
        "from osekkai.features.presence import api as presence_api\n"
    )
    assert _check(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "import osekkai.features.presence\n",
        "from osekkai.features.presence.feature import PresenceFeature\n",
        "from osekkai.features.routine.api import RoutineStatus\n",
        "from osekkai.features.daruma import DarumaFeature\n",
        "from osekkai.voice.api import Heard\n",
        "from osekkai import features\n",
        "from .. import presence\n",
        "from ..routine.feature import RoutineFeature\n",
        "def f():\n    import osekkai.features.presence.feature\n",
    ],
)
def test_ほかの機能と音声の入口の読み込みを見つける(source: str) -> None:
    assert _check(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "from osekkai.adapters.pi import hat\n",
        "import osekkai.adapters.fake\n",
        "from osekkai import adapters\n",
        "from ...adapters.fake import clock\n",
        "def f():\n    from osekkai.adapters.fake.devices import FakeDevices\n",
    ],
)
def test_基盤のアダプタの読み込みを見つける(source: str) -> None:
    for module in (SERVICE, SENSORS, USB_CAMERA, CLI, FEATURE):
        assert _check(source, module) != [], module


@pytest.mark.parametrize(
    "source",
    [
        "from osekkai.core.handles import NeckHandle\n",
        "from osekkai.core import storage\n",
        "import osekkai.core\n",
        "from osekkai import core\n",
        "from osekkai.__main__ import build_devices\n",
        "from osekkai.core.runtime import Runtime\n",
        "from osekkai.core.registry import FeatureRegistry\n",
    ],
)
def test_基盤の決まった口のほかはどこでも断る(source: str) -> None:
    for module in (SERVICE, CLI, FEATURE, REMOTE):
        assert _check(source, module) != [], module


@pytest.mark.parametrize(
    "source",
    [
        "from osekkai.core.instance_lock import InstanceLock\n",
        "from osekkai.core.logging_setup import setup\n",
        "from osekkai.core.config import ConfigLoader\n",
        "from osekkai.core.clock import SystemClock\n",
        "def f():\n    from osekkai.core.clock import SystemClock\n",
    ],
)
def test_道具だけの口はほかのモジュールでは断る(source: str) -> None:
    for module in (SERVICE, FEATURE, WEB, SENSORS, f"{REMOTE}.__main__", REMOTE):
        assert _check(source, module) != [], module
    assert _check(source, CLI) == []  # 道具だけは読んでよい


@pytest.mark.parametrize(("module", "libs"), sorted(ADAPTER_LIBS.items()))
def test_決まったアダプタだけは関数の中でハードのライブラリを読める(
    module: str, libs: frozenset[str]
) -> None:
    for lib in sorted(libs):
        assert _check(f"def f():\n    import {lib}\n", module) == []
        assert _check(f"def f():\n    from {lib} import x\n", module) == []
        # 一番上での読み込みは断る
        assert _check(f"import {lib}\n", module) != []
        # ほかのモジュールは関数の中でも断る
        for other in (SERVICE, FEATURE, WEB, CLI, *(m for m in ADAPTER_LIBS if m != module)):
            assert _check(f"def f():\n    import {lib}\n", other) != [], (lib, other)


@pytest.mark.parametrize("lib", HARDWARE_LIBS)
def test_許しのないハードのライブラリはどこでも断る(lib: str) -> None:
    modules = [SERVICE, FEATURE, WEB, CLI, REMOTE]
    modules += [m for m, libs in ADAPTER_LIBS.items() if lib not in libs]
    for module in modules:
        assert _check(f"import {lib}\n", module) != [], module
        assert _check(f"def f():\n    import {lib}\n", module) != [], module
        assert _check(f"from {lib} import x\n", module) != [], module


def test_Flask_は_web_だけが読める() -> None:
    source = "from flask import Flask, Response\nimport flask\n"
    assert _check(source, WEB) == []
    for module in (SERVICE, FEATURE, CLI, SENSORS, USB_CAMERA, REMOTE):
        assert _check(source, module) != [], module
        assert _check("def f():\n    import flask\n", module) != [], module


def test_web_は本体の関数の中だけで読める() -> None:
    lazy = "def run():\n    from .web import build_app\n"
    assert _check(lazy, FEATURE) == []
    assert _check("def run():\n    from . import web\n", FEATURE) == []
    # 本体の一番上・ほかのモジュールは断る
    assert _check("from .web import build_app\n", FEATURE) != []
    assert _check("from . import web\n", FEATURE) != []
    for module in (CLI, SERVICE, f"{REMOTE}.__main__"):
        assert _check(lazy, module) != [], module
    assert _check("from .web import x\n", REMOTE, is_package=True) != []


def test_外のふつうのライブラリは読んでよい() -> None:
    source = "import threading\nfrom dataclasses import dataclass\nfrom pydantic import Field\n"
    assert _check(source) == []
