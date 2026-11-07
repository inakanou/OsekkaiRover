"""見つけてあいさつ（presence）の読み込みの向きを、ソースを読んで静的に確かめる。"""

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

SRC_DIR = Path(__file__).resolve().parents[3] / "src"
PRESENCE_DIR = SRC_DIR / "osekkai" / "features" / "presence"

PRESENCE = "osekkai.features.presence"
FEATURES = "osekkai.features"
ADAPTERS = "osekkai.adapters"
# OpenCV と numpy は OpenCV のアダプタの中だけ、picamera2 はカメラのアダプタの中だけで、
# 関数の中（か型の確認の中）で読む
OPENCV_MODULE = "osekkai.features.presence.adapters.opencv"
OPENCV_LIBS = ("cv2", "numpy")
PICAMERA_MODULE = "osekkai.features.presence.adapters.picamera"
PICAMERA_LIBS = ("picamera2",)
# ほかのハードのライブラリとサブプロセスは、どこでも読まない
HARDWARE_LIBS = ("smbus2", "lgpio", "faster_whisper", "anthropic")
SUBPROCESS = "subprocess"
# 基盤の起動・登録・ロック・設定の読み込みは、コマンドの入口だけが使う
# （設定のモデルの FeatureConfig や時計の型の Clock は誰でも読んでよい）
ENTRY_MODULES = frozenset({"osekkai.features.presence.cli", "osekkai.features.presence.__main__"})
ENTRY_ONLY = (
    "osekkai.__main__",
    "osekkai.core.runtime",
    "osekkai.core.registry",
    "osekkai.core.instance_lock",
    "osekkai.core.config.ConfigLoader",
    "osekkai.core.clock.SystemClock",
)


class ImportRef(NamedTuple):
    name: str
    in_function: bool
    in_type_checking: bool


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC_DIR).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


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

    def visit(node: ast.AST, in_func: bool, in_tc: bool) -> None:
        if isinstance(node, ast.Import):
            found.extend(ImportRef(a.name, in_func, in_tc) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names = _resolve_from(module, is_package, node)
            found.extend(ImportRef(n, in_func, in_tc) for n in names)
        is_func = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
        # `if TYPE_CHECKING:` の本体だけが型の確認の中（else は実行時）
        tc_body: list[ast.stmt] = []
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            tc_body = node.body
        for child in ast.iter_child_nodes(node):
            child_tc = any(child is stmt for stmt in tc_body)
            visit(child, in_func or is_func, in_tc or child_tc)

    visit(tree, False, False)
    return found


def _imports(path: Path) -> list[ImportRef]:
    return scan_imports(
        path.read_text(encoding="utf-8"),
        _module_name(path),
        is_package=path.name == "__init__.py",
    )


def _matches(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def _other_feature_api(name: str) -> bool:
    # osekkai.features.<ほかの機能>.api（とその中の名前）だけは読んでよい
    parts = name.split(".")
    return len(parts) >= 4 and parts[3] == "api"


def _violation(module: str, ref: ImportRef) -> bool:
    name = ref.name
    if _matches(name, FEATURES):
        return not (_matches(name, PRESENCE) or _other_feature_api(name))
    if _matches(name, ADAPTERS):
        return True
    lazy = ref.in_function or ref.in_type_checking
    if any(_matches(name, lib) for lib in OPENCV_LIBS):
        return not (module == OPENCV_MODULE and lazy)
    if any(_matches(name, lib) for lib in PICAMERA_LIBS):
        return not (module == PICAMERA_MODULE and lazy)
    if any(_matches(name, lib) for lib in HARDWARE_LIBS) or _matches(name, SUBPROCESS):
        return True
    if any(_matches(name, m) for m in ENTRY_ONLY):
        return module not in ENTRY_MODULES
    return False


def presence_violations(module: str, refs: list[ImportRef]) -> list[str]:
    return [ref.name for ref in refs if _violation(module, ref)]


PRESENCE_FILES = sorted(PRESENCE_DIR.rglob("*.py"))


def test_presence_のモジュールが見つかる() -> None:
    names = {_module_name(p) for p in PRESENCE_FILES}
    assert {PRESENCE, f"{PRESENCE}.adapters"} <= names


@pytest.mark.parametrize("path", PRESENCE_FILES, ids=_module_name)
def test_presence_は決まった向きだけを読み込む(path: Path) -> None:
    assert presence_violations(_module_name(path), _imports(path)) == []


# --- 見張りの仕組みそのものの確かめ ---


def _check(source: str, module: str, *, is_package: bool = False) -> list[str]:
    return presence_violations(module, scan_imports(source, module, is_package=is_package))


def test_基盤のアダプタの読み込みを見つける() -> None:
    assert _check("from osekkai.adapters.pi import hat\n", f"{PRESENCE}.watcher") != []
    assert _check("import osekkai.adapters.fake\n", f"{PRESENCE}.cli") != []
    assert _check("from osekkai import adapters\n", f"{PRESENCE}.feature") != []


def test_相対の読み込みで基盤のアダプタを見つける() -> None:
    source = "from ....adapters.fake import FakeClock\n"
    assert _check(source, f"{PRESENCE}.adapters.opencv") != []


def test_関数の中の基盤のアダプタも見つける() -> None:
    source = "def f():\n    from osekkai.adapters.fake.clock import FakeClock\n"
    assert _check(source, f"{PRESENCE}.adapters.picamera") != []


def test_ほかの機能の読み込みを見つける() -> None:
    assert _check("import osekkai.features.routine\n", f"{PRESENCE}.feature") != []
    source = "from osekkai.features.routine.config import RoutineConfig\n"
    assert _check(source, f"{PRESENCE}.feature") != []
    assert _check("from osekkai import features\n", f"{PRESENCE}.feature") != []
    assert _check("from .. import routine\n", f"{PRESENCE}.feature") != []


def test_ほかの機能の_api_だけは読んでよい() -> None:
    assert _check("from osekkai.features.daruma import api\n", f"{PRESENCE}.feature") == []
    source = "from osekkai.features.daruma.api import Something\n"
    assert _check(source, f"{PRESENCE}.feature") == []
    assert _check("import osekkai.features.daruma.api\n", f"{PRESENCE}.feature") == []


def test_自分のパッケージは読んでよい() -> None:
    source = (
        "from . import config\n"
        "from .tracking import FaceBox\n"
        "from osekkai.features.presence.ports import CameraPort\n"
        "import osekkai.features.presence.texts\n"
    )
    assert _check(source, f"{PRESENCE}.watcher") == []
    assert _check("from ..ports import CameraPort\n", f"{PRESENCE}.adapters.opencv") == []


@pytest.mark.parametrize("lib", ["cv2", "numpy"])
def test_OpenCV_のアダプタでは関数の中で_cv2_と_numpy_を読んでよい(lib: str) -> None:
    assert _check(f"def load():\n    import {lib}\n", OPENCV_MODULE) == []
    assert _check(f"def load():\n    from {lib} import x\n", OPENCV_MODULE) == []


def test_OpenCV_のアダプタでは型の確認の中で_numpy_を読んでよい() -> None:
    source = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import numpy\n"
    assert _check(source, OPENCV_MODULE) == []


@pytest.mark.parametrize("lib", ["cv2", "numpy"])
def test_OpenCV_のアダプタでも一番上での_cv2_と_numpy_は断る(lib: str) -> None:
    assert _check(f"import {lib}\n", OPENCV_MODULE) != []
    assert _check(f"from {lib} import x\n", OPENCV_MODULE) != []


@pytest.mark.parametrize("lib", ["cv2", "numpy", "numpy.linalg"])
def test_OpenCV_のアダプタのほかは関数の中でも_cv2_と_numpy_を断る(lib: str) -> None:
    source = f"def f():\n    import {lib}\n"
    for module in (PICAMERA_MODULE, f"{PRESENCE}.faces", f"{PRESENCE}.cli", PRESENCE):
        assert _check(source, module) != [], module


def test_picamera_のアダプタでは関数の中で_picamera2_を読んでよい() -> None:
    assert _check("def start():\n    from picamera2 import Picamera2\n", PICAMERA_MODULE) == []


def test_picamera_のアダプタでも一番上での_picamera2_は断る() -> None:
    assert _check("from picamera2 import Picamera2\n", PICAMERA_MODULE) != []
    assert _check("import picamera2\n", PICAMERA_MODULE) != []


def test_picamera_のアダプタのほかは関数の中でも_picamera2_を断る() -> None:
    source = "def f():\n    import picamera2\n"
    for module in (OPENCV_MODULE, f"{PRESENCE}.feature", f"{PRESENCE}.__main__"):
        assert _check(source, module) != [], module


@pytest.mark.parametrize("lib", ["smbus2", "lgpio", "faster_whisper", "anthropic"])
def test_ほかのハードのライブラリはどこでも断る(lib: str) -> None:
    source = f"def f():\n    import {lib}.sub\n"
    for module in (OPENCV_MODULE, PICAMERA_MODULE, f"{PRESENCE}.cli", f"{PRESENCE}.__main__"):
        assert _check(source, module) != [], module
    assert _check(f"from {lib} import x\n", f"{PRESENCE}.feature") != []


def test_サブプロセスはどこでも断る() -> None:
    for module in (OPENCV_MODULE, PICAMERA_MODULE, f"{PRESENCE}.cli", f"{PRESENCE}.__main__"):
        assert _check("import subprocess\n", module) != [], module
        assert _check("def f():\n    from subprocess import run\n", module) != [], module


@pytest.mark.parametrize(
    "source",
    [
        "from osekkai.__main__ import build_devices\n",
        "from osekkai import __main__\n",
        "from osekkai.core.runtime import Runtime\n",
        "from osekkai.core import registry\n",
        "from osekkai.core.registry import FeatureRegistry\n",
        "import osekkai.core.instance_lock\n",
        "from osekkai.core.config import ConfigLoader\n",
        "from osekkai.core.clock import SystemClock\n",
    ],
)
def test_基盤の起動と登録とロックと設定の読み込みはコマンドの入口だけが使う(source: str) -> None:
    assert _check(source, f"{PRESENCE}.cli") == []
    assert _check(source, f"{PRESENCE}.__main__") == []
    for module in (PRESENCE, f"{PRESENCE}.feature", f"{PRESENCE}.checks", OPENCV_MODULE):
        assert _check(source, module) != [], module
    lazy = "def f():\n" + "".join(f"    {line}\n" for line in source.splitlines())
    assert _check(lazy, f"{PRESENCE}.watcher") != []


def test_相対の読み込みで基盤の起動を見つける() -> None:
    assert _check("from ...core.runtime import Runtime\n", f"{PRESENCE}.feature") != []
    assert _check("from ...core.runtime import Runtime\n", f"{PRESENCE}.cli") == []


def test_core_の公開の口は誰でも読んでよい() -> None:
    source = (
        "from osekkai.core.feature import Feature, FeatureContext\n"
        "from osekkai.core.arbiter import Lease\n"
        "from osekkai.core.config import FeatureConfig\n"
        "from osekkai.core.clock import Clock\n"
        "from osekkai.core import selfcheck, storage, paths, lcd_text\n"
        "from osekkai.core.events import QuietChanged\n"
    )
    assert _check(source, f"{PRESENCE}.feature") == []
    assert _check(source, OPENCV_MODULE) == []


def test_相対の読み込みを解決する() -> None:
    refs = scan_imports("from ..ports import CameraPort\n", OPENCV_MODULE)
    assert [r.name for r in refs] == [f"{PRESENCE}.ports.CameraPort"]
    pkg_refs = scan_imports("from . import opencv\n", f"{PRESENCE}.adapters", is_package=True)
    assert [r.name for r in pkg_refs] == [f"{PRESENCE}.adapters.opencv"]
    top_refs = scan_imports("from .feature import PresenceFeature\n", PRESENCE, is_package=True)
    assert [r.name for r in top_refs] == [f"{PRESENCE}.feature.PresenceFeature"]
    up_refs = scan_imports("from ...core import clock\n", f"{PRESENCE}.feature")
    assert [r.name for r in up_refs] == ["osekkai.core.clock"]


def test_関数の中と_TYPE_CHECKING_の中を見分ける() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "import typing\n"
        "if TYPE_CHECKING:\n"
        "    import numpy\n"
        "else:\n"
        "    import json\n"
        "if typing.TYPE_CHECKING:\n"
        "    import cv2\n"
        "def f():\n"
        "    import picamera2\n"
    )
    refs = {r.name: (r.in_function, r.in_type_checking) for r in scan_imports(source, PRESENCE)}
    assert refs["numpy"] == (False, True)
    assert refs["cv2"] == (False, True)
    assert refs["json"] == (False, False)
    assert refs["picamera2"] == (True, False)
    assert refs["typing.TYPE_CHECKING"] == (False, False)
