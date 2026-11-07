"""音声の入口（voice）の読み込みの向きを、ソースを読んで静的に確かめる。"""

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
VOICE_DIR = SRC_DIR / "osekkai" / "voice"

FEATURES = "osekkai.features"
ADAPTERS = "osekkai.adapters"
# 打った文字の聞き取りだけが、端末の偽物の「最後に入れた行」の口を読む
TYPED_MODULE = "osekkai.voice.adapters.typed"
TYPED_ALLOWED = "osekkai.adapters.fake.console"
# 聞き取りのライブラリとサブプロセスは、実機の聞き取りのアダプタの中だけで使う
WHISPER_MODULE = "osekkai.voice.adapters.whisper"
LAZY_LIBS = ("faster_whisper", "numpy", "ctranslate2")
SUBPROCESS = "subprocess"
HARDWARE_LIBS = ("smbus2", "lgpio", "cv2", "picamera2", "anthropic")
# 基盤のロックと設定の読み込みは、開発用の確認のコマンドだけが使う
# （設定のモデルの FeatureConfig は誰でも読んでよい）
MAIN_MODULE = "osekkai.voice.__main__"
MAIN_ONLY = ("osekkai.core.instance_lock", "osekkai.core.config.ConfigLoader")
# 基盤の起動（Runtime）は入口のどこからも使わない
RUNTIME = "osekkai.core.runtime"


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
    if _matches(name, FEATURES):
        return True
    if _matches(name, ADAPTERS):
        return not (module == TYPED_MODULE and _matches(name, TYPED_ALLOWED))
    if any(_matches(name, lib) for lib in LAZY_LIBS):
        return not (module == WHISPER_MODULE and ref.in_function)
    if _matches(name, SUBPROCESS):
        return module != WHISPER_MODULE
    if any(_matches(name, lib) for lib in HARDWARE_LIBS) or _matches(name, RUNTIME):
        return True
    if any(_matches(name, m) for m in MAIN_ONLY):
        return module != MAIN_MODULE
    return False


def voice_violations(module: str, refs: list[ImportRef]) -> list[str]:
    return [ref.name for ref in refs if _violation(module, ref)]


VOICE_FILES = sorted(VOICE_DIR.rglob("*.py"))


def test_voice_のモジュールが見つかる() -> None:
    names = {_module_name(p) for p in VOICE_FILES}
    assert {"osekkai.voice", "osekkai.voice.adapters"} <= names


@pytest.mark.parametrize("path", VOICE_FILES, ids=lambda p: _module_name(p))
def test_voice_は決まった向きだけを読み込む(path: Path) -> None:
    assert voice_violations(_module_name(path), _imports(path)) == []


# --- 見張りの仕組みそのものの確かめ ---


def _check(source: str, module: str) -> list[str]:
    return voice_violations(module, scan_imports(source, module))


def test_features_の読み込みを見つける() -> None:
    assert _check("import osekkai.features.greeting\n", "osekkai.voice.flow") != []


def test_相対の読み込みで_features_を見つける() -> None:
    assert _check("from ...features import greeting\n", "osekkai.voice.adapters.typed") != []


def test_from_osekkai_import_features_を見つける() -> None:
    assert _check("from osekkai import features\n", "osekkai.voice.feature") != []


def test_関数の中の_features_も見つける() -> None:
    source = "def f():\n    from osekkai.features.greeting import X\n"
    assert _check(source, "osekkai.voice.feature") != []


def test_adapters_の読み込みを見つける() -> None:
    assert _check("from osekkai.adapters.pi import hat\n", "osekkai.voice.flow") != []


def test_typed_は端末の偽物を読んでよい() -> None:
    source = "from osekkai.adapters.fake.console import take_typed_line\n"
    assert _check(source, "osekkai.voice.adapters.typed") == []
    assert (
        _check("from osekkai.adapters.fake import console\n", "osekkai.voice.adapters.typed") == []
    )


def test_typed_でも端末の偽物のほかの_adapters_は断る() -> None:
    source = "from osekkai.adapters.fake import devices\n"
    assert _check(source, "osekkai.voice.adapters.typed") != []
    assert _check("import osekkai.adapters.pi\n", "osekkai.voice.adapters.typed") != []


def test_typed_のほかは端末の偽物を読まない() -> None:
    source = "from osekkai.adapters.fake.console import take_typed_line\n"
    assert _check(source, "osekkai.voice.flow") != []


@pytest.mark.parametrize("lib", ["faster_whisper", "numpy", "ctranslate2"])
def test_whisper_では関数の中だけで聞き取りのライブラリを読んでよい(lib: str) -> None:
    source = f"def load():\n    import {lib}\n"
    assert _check(source, "osekkai.voice.adapters.whisper") == []


@pytest.mark.parametrize("lib", ["faster_whisper", "numpy", "ctranslate2"])
def test_whisper_でも一番上での聞き取りのライブラリは断る(lib: str) -> None:
    assert _check(f"import {lib}\n", "osekkai.voice.adapters.whisper") != []
    source = f"from {lib} import x\n"
    assert _check(source, "osekkai.voice.adapters.whisper") != []


@pytest.mark.parametrize("lib", ["faster_whisper", "numpy", "ctranslate2", "subprocess"])
def test_whisper_のほかは聞き取りのライブラリとサブプロセスを断る(lib: str) -> None:
    source = f"def f():\n    import {lib}\n"
    assert _check(source, "osekkai.voice.feature") != []
    assert _check(source, "osekkai.voice.adapters.typed") != []


def test_whisper_は一番上でサブプロセスを読んでよい() -> None:
    assert _check("import subprocess\n", "osekkai.voice.adapters.whisper") == []


@pytest.mark.parametrize("lib", ["smbus2", "lgpio", "cv2", "picamera2", "anthropic"])
def test_ハードのライブラリはどこでも断る(lib: str) -> None:
    source = f"def f():\n    import {lib}.sub\n"
    assert _check(source, "osekkai.voice.adapters.whisper") != []
    assert _check(source, "osekkai.voice.__main__") != []


def test_ロックは確認のコマンドだけが読む() -> None:
    source = "from osekkai.core import instance_lock\n"
    assert _check(source, "osekkai.voice.__main__") == []
    assert _check(source, "osekkai.voice.feature") != []
    assert _check("import osekkai.core.instance_lock\n", "osekkai.voice") != []


def test_設定の読み込みは確認のコマンドだけが使い_設定のモデルは誰でも読む() -> None:
    loader = "from osekkai.core.config import ConfigLoader\n"
    assert _check(loader, "osekkai.voice.__main__") == []
    assert _check(loader, "osekkai.voice.feature") != []
    assert _check(loader, "osekkai.voice.checks") != []
    assert _check("from osekkai.core.config import FeatureConfig\n", "osekkai.voice.config") == []


def test_基盤の起動はどこからも使わない() -> None:
    source = "from osekkai.core.runtime import Runtime\n"
    assert _check(source, "osekkai.voice.__main__") != []
    assert _check(source, "osekkai.voice.feature") != []


def test_core_の公開の口は読んでよい() -> None:
    source = (
        "from osekkai.core.feature import Feature\n"
        "from osekkai.core.clock import Clock\n"
        "from . import texts\n"
        "from .recognizer import Recognizer\n"
    )
    assert _check(source, "osekkai.voice.feature") == []


def test_相対の読み込みを解決する() -> None:
    refs = scan_imports("from ..recognizer import Recognizer\n", "osekkai.voice.adapters.whisper")
    assert [r.name for r in refs] == ["osekkai.voice.recognizer.Recognizer"]
    pkg_refs = scan_imports("from . import whisper\n", "osekkai.voice.adapters", is_package=True)
    assert [r.name for r in pkg_refs] == ["osekkai.voice.adapters.whisper"]
