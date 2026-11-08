"""だるまさん（daruma）の読み込みの向きを、ソースを読んで静的に確かめる（9.2）。

osekkai の中は決まった口だけを読む。ほかの機能は api だけ、基盤のアダプタと
ハードのライブラリはどこでも読まない（ハードがなくても読み込みで失敗しない）。
"""

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

SRC_DIR = Path(__file__).resolve().parents[3] / "src"
DARUMA_DIR = SRC_DIR / "osekkai" / "features" / "daruma"

DARUMA = "osekkai.features.daruma"
OSEKKAI = "osekkai"
# osekkai の中で、どのモジュールも読んでよい口
ALLOWED = (
    DARUMA,
    "osekkai.core.feature",
    "osekkai.core.config",
    "osekkai.core.types",
    "osekkai.core.arbiter",
    "osekkai.core.clock",
    "osekkai.core.lcd_text",
    "osekkai.core.selfcheck",
    "osekkai.voice.api",
    "osekkai.features.presence.api",
    "osekkai.features.routine.api",
)
# 基盤の起動・登録・ロック・設定の読み込みは、コマンドの入口だけが使う
# （設定のモデルの FeatureConfig や時計の型の Clock は誰でも読んでよい）
ENTRY_ONLY = (
    "osekkai.__main__",
    "osekkai.core.runtime",
    "osekkai.core.registry",
    "osekkai.core.instance_lock",
    "osekkai.core.logging_setup",
    "osekkai.core.config.ConfigLoader",
    "osekkai.core.clock.SystemClock",
)
# ENTRY_ONLY を読んでよいコマンドの入口（道具ができたら足す）
ENTRY_MODULES: frozenset[str] = frozenset()
# ハードのライブラリは、どこでも（関数の中でも）読まない
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


def _violation(module: str, ref: ImportRef) -> bool:
    name = ref.name
    if any(_matches(name, lib) for lib in HARDWARE_LIBS):
        return True
    if any(_matches(name, entry) for entry in ENTRY_ONLY):
        return module not in ENTRY_MODULES
    if _matches(name, OSEKKAI):
        return not any(_matches(name, allowed) for allowed in ALLOWED)
    return False


def daruma_violations(module: str, refs: list[ImportRef]) -> list[str]:
    return [ref.name for ref in refs if _violation(module, ref)]


DARUMA_FILES = sorted(DARUMA_DIR.rglob("*.py"))


def test_daruma_のモジュールが見つかる() -> None:
    assert DARUMA in {_module_name(p) for p in DARUMA_FILES}


@pytest.mark.parametrize("path", DARUMA_FILES, ids=_module_name)
def test_daruma_は決まった向きだけを読み込む(path: Path) -> None:
    assert daruma_violations(_module_name(path), _imports(path)) == []


# --- 見張りの仕組みそのものの確かめ ---

FLOW = f"{DARUMA}.flow"


def _check(source: str, module: str = FLOW, *, is_package: bool = False) -> list[str]:
    return daruma_violations(module, scan_imports(source, module, is_package=is_package))


def test_自分のパッケージは読んでよい() -> None:
    source = (
        "from . import config\n"
        "from .rules import Rules\n"
        "from osekkai.features.daruma.texts import Chant\n"
        "import osekkai.features.daruma.camera\n"
    )
    assert _check(source) == []
    assert _check("from .feature import X\n", DARUMA, is_package=True) == []


def test_基盤の決まった口は読んでよい() -> None:
    source = (
        "from osekkai.core.feature import FeatureContext, FeatureSpec\n"
        "from osekkai.core.config import FeatureConfig\n"
        "from osekkai.core.types import Priority, Resource, Face\n"
        "from osekkai.core.arbiter import Lease\n"
        "from osekkai.core.clock import Clock\n"
        "from osekkai.core.lcd_text import center\n"
        "from osekkai.core import selfcheck\n"
    )
    assert _check(source) == []


def test_ほかの機能と音声の入口は公開の口だけを読んでよい() -> None:
    source = (
        "from osekkai.voice.api import Heard, register_words\n"
        "from osekkai.voice import api\n"
        "from osekkai.features.presence.api import borrow_camera, CameraBusy\n"
        "from osekkai.features.routine.api import RoutineStatus, current_status\n"
        "from osekkai.features.routine import api as routine_api\n"
    )
    assert _check(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "import osekkai.features.presence\n",
        "from osekkai.features.presence.feature import PresenceFeature\n",
        "from osekkai.features.routine.trial import main\n",
        "from osekkai.features.conversation.api import x\n",
        "from osekkai.features.conversation import ConversationFeature\n",
        "from osekkai import features\n",
        "from .. import presence\n",
        "from ..routine.feature import RoutineFeature\n",
        "def f():\n    import osekkai.features.presence.feature\n",
    ],
)
def test_ほかの機能の公開の口のほかを見つける(source: str) -> None:
    assert _check(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "from osekkai.voice.feature import VoiceGatewayFeature\n",
        "import osekkai.voice.adapters.typed\n",
        "from osekkai import voice\n",
    ],
)
def test_音声の入口の公開の口のほかを見つける(source: str) -> None:
    assert _check(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "from osekkai.adapters.pi import hat\n",
        "import osekkai.adapters.fake\n",
        "from osekkai import adapters\n",
        "from osekkai.adapters.fake.console import typed_input_active\n",
        "from ...adapters.fake import clock\n",
        "def f():\n    from osekkai.adapters.fake.devices import FakeDevices\n",
    ],
)
def test_基盤のアダプタの読み込みを見つける(source: str) -> None:
    assert _check(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "from osekkai.core.handles import NeckHandle\n",
        "from osekkai.core import storage\n",
        "import osekkai.core\n",
        "from osekkai import core\n",
    ],
)
def test_基盤の決まった口のほかを見つける(source: str) -> None:
    assert _check(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "from osekkai.__main__ import build_devices\n",
        "from osekkai.core.runtime import Runtime\n",
        "from osekkai.core.registry import FeatureRegistry\n",
        "from osekkai.core.instance_lock import InstanceLock\n",
        "from osekkai.core.logging_setup import setup\n",
        "from osekkai.core.config import ConfigLoader\n",
        "from osekkai.core.clock import SystemClock\n",
        "def f():\n    from osekkai.core.runtime import Runtime\n",
    ],
)
def test_入口だけの口はほかのモジュールでは断る(source: str) -> None:
    for module in (FLOW, f"{DARUMA}.feature", f"{DARUMA}.cli", DARUMA):
        assert _check(source, module) != [], module


@pytest.mark.parametrize("lib", HARDWARE_LIBS)
def test_ハードのライブラリはどこでも断る(lib: str) -> None:
    for module in (FLOW, f"{DARUMA}.camera", f"{DARUMA}.cli", DARUMA):
        assert _check(f"import {lib}\n", module) != [], module
        assert _check(f"def f():\n    import {lib}\n", module) != [], module
        assert _check(f"from {lib} import x\n", module) != [], module


def test_外のふつうのライブラリは読んでよい() -> None:
    source = "import threading\nfrom dataclasses import dataclass\nfrom pydantic import Field\n"
    assert _check(source) == []
