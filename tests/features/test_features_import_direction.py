"""機能（features）の読み込みの向きを、ソースを読んで静的に確かめる（11.2）。

features の下のモジュールは、あとから足したものも自動で確かめる。
"""

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
FEATURES_DIR = SRC_DIR / "osekkai" / "features"

FEATURES_PACKAGE = "osekkai.features"
ADAPTERS_PACKAGE = "osekkai.adapters"
ENTRY_MODULE = "osekkai.__main__"
HARDWARE_LIBS = (
    "smbus2",
    "lgpio",
    "numpy",
    "cv2",
    "picamera2",
    "faster_whisper",
    "anthropic",
)

# 組み立ての口として osekkai.__main__（build_devices）を読み込んでよい試しの入口
ENTRY_ALLOWLIST = frozenset(
    {
        "osekkai.features.routine.trial",
        "osekkai.features.routine.__main__",
        "osekkai.features.presence.cli",
        "osekkai.features.daruma.cli",
    }
)

# 関数の中で遅れて読み込むことを条件に、外の依存を読み込んでよいモジュール
LAZY_ONLY_LIBS = {"jpholiday": frozenset({"osekkai.features.routine.calendar"})}

# 機能の中のアダプタ（見つけてあいさつのカメラと映像の処理）だけは、関数の中で遅れて
# 読み込むことを条件に、ハードのライブラリを読み込んでよい（presence の設計）
FEATURE_ADAPTER_LIBS = {
    "osekkai.features.presence.adapters.opencv": frozenset({"cv2", "numpy"}),
    "osekkai.features.presence.adapters.picamera": frozenset({"picamera2"}),
    # おしゃべりの Claude のアダプタだけが、関数の中で anthropic を読む（conversation の設計）
    "osekkai.features.conversation.adapters.claude": frozenset({"anthropic"}),
    # ラジコンのセンサーと USB のカメラのアダプタ（remote-control の設計）
    "osekkai.features.remote.adapters.sensors": frozenset({"smbus2", "lgpio"}),
    "osekkai.features.remote.adapters.usb_camera": frozenset({"cv2", "numpy"}),
}

# 基盤のアダプタのうち、決まったモジュールだけが読んでよいもの（狭い許し）。
# おしゃべりの本体だけが、端末の偽物が動いているかの判定（typed_input_active）を読む
ADAPTER_ALLOWLIST = {
    "osekkai.adapters.fake.console": frozenset({"osekkai.features.conversation.feature"}),
}


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
    # `from x import y` は x.y というモジュールかもしれないので、両方を候補にする
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


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


def _matches(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def _own_feature(module: str) -> str | None:
    """`osekkai.features.<x>...` の <x> を返す。features の直下の口なら None。"""
    parts = module.split(".")
    return parts[2] if len(parts) > 2 else None


def _is_other_feature(name: str, own: str | None) -> bool:
    """ほかの機能のパッケージへの読み込みか。ほかの機能の `api` は認める。"""
    if not name.startswith(FEATURES_PACKAGE + "."):
        return False
    parts = name.split(".")
    other = parts[2]
    if other == own:
        return False
    return not (len(parts) >= 4 and parts[3] == "api")


def _is_hardware(name: str) -> bool:
    return any(_matches(name, lib) for lib in HARDWARE_LIBS)


def _feature_adapter_ok(module: str, ref: ImportRef) -> bool:
    """機能の中のアダプタが、許されたハードのライブラリを関数の中で読み込むか。"""
    allowed = FEATURE_ADAPTER_LIBS.get(module, frozenset())
    return ref.in_function and any(_matches(ref.name, lib) for lib in allowed)


def _lazy_ok(module: str, ref: ImportRef) -> bool:
    """遅れて読み込むことを条件にした外の依存なら、許されたモジュールの関数の中だけで認める。"""
    for lib, allowed in LAZY_ONLY_LIBS.items():
        if _matches(ref.name, lib):
            return module in allowed and ref.in_function
    return True


def _adapter_allowed(module: str, name: str) -> bool:
    """許しの表にある基盤のアダプタを、許されたモジュールが読み込むか。"""
    return any(
        _matches(name, adapter) and module in modules
        for adapter, modules in ADAPTER_ALLOWLIST.items()
    )


def _is_forbidden(module: str, ref: ImportRef) -> bool:
    name = ref.name
    if _matches(name, ADAPTERS_PACKAGE):
        return not _adapter_allowed(module, name)
    if _matches(name, ENTRY_MODULE):
        return module not in ENTRY_ALLOWLIST
    if _is_other_feature(name, _own_feature(module)):
        return True
    if _is_hardware(name):
        return not _feature_adapter_ok(module, ref)
    return not _lazy_ok(module, ref)


def forbidden_imports(module: str, refs: list[ImportRef]) -> list[str]:
    return [ref.name for ref in refs if _is_forbidden(module, ref)]


def _imports(path: Path) -> list[ImportRef]:
    return scan_imports(
        path.read_text(encoding="utf-8"),
        _module_name(path),
        is_package=path.name == "__init__.py",
    )


FEATURE_FILES = sorted(FEATURES_DIR.rglob("*.py"))


def test_features_のモジュールが見つかり日課のパッケージを含む() -> None:
    names = {_module_name(p) for p in FEATURE_FILES}
    assert {"osekkai.features", "osekkai.features.routine"} <= names


@pytest.mark.parametrize("path", FEATURE_FILES, ids=_module_name)
def test_機能はアダプタとハードのライブラリとほかの機能を読み込まない(path: Path) -> None:
    assert forbidden_imports(_module_name(path), _imports(path)) == []


# --- 見張りの仕組みそのものの確かめ ---


def _check(source: str, module: str, *, is_package: bool = False) -> list[str]:
    return forbidden_imports(module, scan_imports(source, module, is_package=is_package))


def test_adapters_の読み込みを見つける() -> None:
    assert _check(
        "from osekkai.adapters.fake.clock import FakeClock\n", "osekkai.features.routine.x"
    )


def test_試しの入口でも_adapters_は断る() -> None:
    source = "def main():\n    import osekkai.adapters.pi\n"
    assert _check(source, "osekkai.features.routine.trial")


@pytest.mark.parametrize("lib", HARDWARE_LIBS)
def test_ハードのライブラリを見つける(lib: str) -> None:
    source = f"def f():\n    import {lib}\n"
    assert _check(source, "osekkai.features.routine.x") == [lib]


def test_ほかの機能のパッケージを見つける() -> None:
    assert _check("from osekkai.features.presence import x\n", "osekkai.features.routine.x")
    assert _check("import osekkai.features.presence.feature\n", "osekkai.features.routine.x")


def test_相対の読み込みでほかの機能を見つける() -> None:
    source = "from ..presence import feature\n"
    assert _check(source, "osekkai.features.routine.x") != []


def test_ほかの機能の_api_は認める() -> None:
    source = "from osekkai.features.routine.api import RoutineStatus\n"
    assert _check(source, "osekkai.features.presence.feature") == []


def test_自分のパッケージの中の読み込みは認める() -> None:
    source = "from .config import RoutineConfig\nfrom osekkai.features.routine import calendar\n"
    assert _check(source, "osekkai.features.routine.schedule") == []
    assert _check("from . import feature\n", "osekkai.features.routine", is_package=True) == []


def test_基盤の口は認める() -> None:
    source = "from osekkai.core.feature import FeatureSpec\nimport osekkai.core.storage\n"
    assert _check(source, "osekkai.features.routine.feature") == []


def test_試しの入口だけは_osekkai___main___を読み込める() -> None:
    source = "from osekkai.__main__ import build_devices\n"
    assert _check(source, "osekkai.features.routine.trial") == []
    assert _check(source, "osekkai.features.routine.__main__") == []
    assert _check(source, "osekkai.features.routine.feature") != []
    assert _check(source, "osekkai.features.presence.trial") != []


def test_jpholiday_は暦のモジュールの関数の中だけで認める() -> None:
    lazy = "def load():\n    import jpholiday\n"
    top = "import jpholiday\n"
    assert _check(lazy, "osekkai.features.routine.calendar") == []
    assert _check(top, "osekkai.features.routine.calendar") == ["jpholiday"]
    assert _check(lazy, "osekkai.features.routine.schedule") == ["jpholiday"]


def test_機能の中のアダプタだけは関数の中でハードのライブラリを読み込める() -> None:
    lazy = "def f():\n    import cv2\n    import numpy as np\n"
    assert _check(lazy, "osekkai.features.presence.adapters.opencv") == []
    # 一番上での読み込み・許されていないライブラリ・ほかのモジュールは断る
    assert _check("import cv2\n", "osekkai.features.presence.adapters.opencv") == ["cv2"]
    assert _check(lazy, "osekkai.features.presence.adapters.picamera") == ["cv2", "numpy"]
    assert _check(lazy, "osekkai.features.presence.watcher") == ["cv2", "numpy"]


def test_見つけてあいさつのコマンドは_osekkai___main___を読み込める() -> None:
    source = "from osekkai.__main__ import build_devices\n"
    assert _check(source, "osekkai.features.presence.cli") == []
    assert _check(source, "osekkai.features.presence.feature") != []


def test_だるまさんの道具は_osekkai___main___を読み込める() -> None:
    source = "from osekkai.__main__ import build_devices\n"
    assert _check(source, "osekkai.features.daruma.cli") == []
    assert _check(source, "osekkai.features.daruma.feature") != []
    assert _check(source, "osekkai.features.daruma.__main__") != []


CLAUDE_ADAPTER = "osekkai.features.conversation.adapters.claude"
CONVERSATION_FEATURE = "osekkai.features.conversation.feature"


def test_Claude_のアダプタだけは関数の中で_anthropic_を読み込める() -> None:
    lazy = "def f():\n    import anthropic\n"
    assert _check(lazy, CLAUDE_ADAPTER) == []
    assert _check("def f():\n    from anthropic import Anthropic\n", CLAUDE_ADAPTER) == []
    # 一番上での読み込み・おしゃべりのほかのモジュール・ほかの機能は断る
    assert _check("import anthropic\n", CLAUDE_ADAPTER) == ["anthropic"]
    for module in (
        CONVERSATION_FEATURE,
        "osekkai.features.conversation.adapters.scripted",
        "osekkai.features.conversation.checks",
        "osekkai.features.presence.adapters.opencv",
    ):
        assert _check(lazy, module) == ["anthropic"], module


def test_ラジコンのアダプタだけは関数の中でハードのライブラリを読み込める() -> None:
    sensors = "osekkai.features.remote.adapters.sensors"
    camera = "osekkai.features.remote.adapters.usb_camera"
    assert _check("def f():\n    import smbus2\n    import lgpio\n", sensors) == []
    assert _check("def f():\n    import cv2\n    import numpy\n", camera) == []
    # 一番上での読み込み・取り違え・ほかのモジュールは断る
    assert _check("import lgpio\n", sensors) == ["lgpio"]
    assert _check("def f():\n    import cv2\n", sensors) == ["cv2"]
    assert _check("def f():\n    import smbus2\n", camera) == ["smbus2"]
    assert _check("def f():\n    import cv2\n", "osekkai.features.remote.service") == ["cv2"]


def test_Claude_のアダプタでもほかのハードのライブラリは断る() -> None:
    assert _check("def f():\n    import cv2\n", CLAUDE_ADAPTER) == ["cv2"]


def test_おしゃべりの本体だけは端末の偽物の判定を読み込める() -> None:
    source = "from osekkai.adapters.fake.console import typed_input_active\n"
    assert _check(source, CONVERSATION_FEATURE) == []
    assert _check("def f():\n    " + source, CONVERSATION_FEATURE) == []
    # おしゃべりのほかのモジュールとほかの機能は断る
    for module in (
        "osekkai.features.conversation.flow",
        "osekkai.features.conversation.adapters.scripted",
        CLAUDE_ADAPTER,
        "osekkai.features.conversation",
        "osekkai.features.presence.feature",
        "osekkai.features.routine.feature",
    ):
        assert _check(source, module) != [], module


def test_おしゃべりの本体でも端末の偽物のほかの_adapters_は断る() -> None:
    for source in (
        "from osekkai.adapters.fake.clock import FakeClock\n",
        "from osekkai.adapters.fake import devices\n",
        "import osekkai.adapters.pi\n",
        "import osekkai.adapters\n",
    ):
        assert _check(source, CONVERSATION_FEATURE) != [], source
