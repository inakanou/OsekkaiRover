"""おしゃべり（conversation）の読み込みの向きを、ソースを読んで静的に確かめる。"""

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

SRC_DIR = Path(__file__).resolve().parents[3] / "src"
CONVERSATION_DIR = SRC_DIR / "osekkai" / "features" / "conversation"

CONVERSATION = "osekkai.features.conversation"
FEATURES = "osekkai.features"
ADAPTERS = "osekkai.adapters"
# 本体だけが、端末の偽物が動いているかの判定を読む（PC の偽の返事を選ぶときだけ）
FEATURE_MODULE = "osekkai.features.conversation.feature"
CONSOLE_ALLOWED = "osekkai.adapters.fake.console.typed_input_active"
# anthropic は Claude のアダプタの関数の中だけで読む
CLAUDE_MODULE = "osekkai.features.conversation.adapters.claude"
ANTHROPIC = "anthropic"
# ほかのハードのライブラリは、どこでも読まない
HARDWARE_LIBS = ("smbus2", "lgpio", "numpy", "cv2", "picamera2", "faster_whisper", "ctranslate2")
# 音声の入口は公開の口（api）だけを使う
VOICE = "osekkai.voice"
VOICE_API = "osekkai.voice.api"
# 基盤の組み立ての口は読まない
ENTRY_MODULE = "osekkai.__main__"


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
        # ほかの機能は api も含めて読まない
        return not _matches(name, CONVERSATION)
    if _matches(name, ADAPTERS):
        return not (module == FEATURE_MODULE and _matches(name, CONSOLE_ALLOWED))
    if _matches(name, ANTHROPIC):
        return not (module == CLAUDE_MODULE and ref.in_function)
    if any(_matches(name, lib) for lib in HARDWARE_LIBS):
        return True
    if _matches(name, VOICE):
        return not _matches(name, VOICE_API)
    return _matches(name, ENTRY_MODULE)


def conversation_violations(module: str, refs: list[ImportRef]) -> list[str]:
    return [ref.name for ref in refs if _violation(module, ref)]


CONVERSATION_FILES = sorted(CONVERSATION_DIR.rglob("*.py"))


def test_conversation_のモジュールが見つかる() -> None:
    names = {_module_name(p) for p in CONVERSATION_FILES}
    assert {CONVERSATION, f"{CONVERSATION}.adapters"} <= names


@pytest.mark.parametrize("path", CONVERSATION_FILES, ids=_module_name)
def test_conversation_は決まった向きだけを読み込む(path: Path) -> None:
    assert conversation_violations(_module_name(path), _imports(path)) == []


# --- 見張りの仕組みそのものの確かめ ---


def _check(source: str, module: str, *, is_package: bool = False) -> list[str]:
    return conversation_violations(module, scan_imports(source, module, is_package=is_package))


@pytest.mark.parametrize(
    "source",
    [
        "import osekkai.features.routine\n",
        "from osekkai.features.presence.feature import PresenceFeature\n",
        "from osekkai.features.routine.api import RoutineStatus\n",
        "from osekkai import features\n",
        "from .. import presence\n",
        "def f():\n    import osekkai.features.presence\n",
    ],
)
def test_ほかの機能の読み込みを見つける(source: str) -> None:
    assert _check(source, f"{CONVERSATION}.flow") != []


def test_自分のパッケージは読んでよい() -> None:
    source = (
        "from . import config\n"
        "from .ports import ChatModelPort\n"
        "from osekkai.features.conversation.texts import SYSTEM_PROMPT\n"
        "import osekkai.features.conversation.session\n"
    )
    assert _check(source, f"{CONVERSATION}.flow") == []
    assert _check("from ..ports import Turn\n", CLAUDE_MODULE) == []
    assert _check("from .feature import X\n", CONVERSATION, is_package=True) == []


@pytest.mark.parametrize(
    "source",
    [
        "from osekkai.adapters.pi import hat\n",
        "import osekkai.adapters.fake\n",
        "from osekkai import adapters\n",
        "from osekkai.adapters.fake.clock import FakeClock\n",
        "def f():\n    from osekkai.adapters.fake.devices import FakeDevices\n",
    ],
)
def test_基盤のアダプタの読み込みを見つける(source: str) -> None:
    for module in (FEATURE_MODULE, CLAUDE_MODULE, f"{CONVERSATION}.adapters.scripted"):
        assert _check(source, module) != [], module


def test_相対の読み込みで基盤のアダプタを見つける() -> None:
    assert _check("from ...adapters.fake import clock\n", FEATURE_MODULE) != []
    assert _check("from ....adapters.fake import clock\n", CLAUDE_MODULE) != []


def test_本体だけは端末の偽物の判定を読んでよい() -> None:
    source = "from osekkai.adapters.fake.console import typed_input_active\n"
    assert _check(source, FEATURE_MODULE) == []
    assert _check("def f():\n    " + source, FEATURE_MODULE) == []


def test_本体でも端末の偽物のほかの口は断る() -> None:
    assert _check("from osekkai.adapters.fake.console import FakeConsole\n", FEATURE_MODULE) != []
    assert _check("from osekkai.adapters.fake import console\n", FEATURE_MODULE) != []
    assert _check("import osekkai.adapters.fake.console\n", FEATURE_MODULE) != []


@pytest.mark.parametrize(
    "module",
    [
        f"{CONVERSATION}.flow",
        f"{CONVERSATION}.checks",
        f"{CONVERSATION}.adapters.scripted",
        CLAUDE_MODULE,
        CONVERSATION,
    ],
)
def test_本体のほかは端末の偽物の判定を読まない(module: str) -> None:
    source = "from osekkai.adapters.fake.console import typed_input_active\n"
    assert _check(source, module) != []


def test_Claude_のアダプタでは関数の中で_anthropic_を読んでよい() -> None:
    assert _check("def make():\n    import anthropic\n", CLAUDE_MODULE) == []
    assert _check("def make():\n    from anthropic import Anthropic\n", CLAUDE_MODULE) == []


def test_Claude_のアダプタでも一番上での_anthropic_は断る() -> None:
    assert _check("import anthropic\n", CLAUDE_MODULE) != []
    assert _check("from anthropic import Anthropic\n", CLAUDE_MODULE) != []


@pytest.mark.parametrize(
    "module",
    [
        FEATURE_MODULE,
        f"{CONVERSATION}.checks",
        f"{CONVERSATION}.adapters.scripted",
        f"{CONVERSATION}.adapters",
        CONVERSATION,
    ],
)
def test_Claude_のアダプタのほかは関数の中でも_anthropic_を断る(module: str) -> None:
    assert _check("def f():\n    import anthropic\n", module) != []


@pytest.mark.parametrize("lib", HARDWARE_LIBS)
def test_ほかのハードのライブラリはどこでも断る(lib: str) -> None:
    source = f"def f():\n    import {lib}\n"
    for module in (CLAUDE_MODULE, FEATURE_MODULE, f"{CONVERSATION}.adapters.scripted"):
        assert _check(source, module) != [], module
    assert _check(f"from {lib} import x\n", CLAUDE_MODULE) != []


def test_音声の入口は_api_だけを読んでよい() -> None:
    source = "from osekkai.voice.api import Heard, listen_again, register_fallback\n"
    assert _check(source, FEATURE_MODULE) == []
    assert _check("from osekkai.voice import api\n", FEATURE_MODULE) == []
    assert _check("from osekkai.voice.feature import VoiceGatewayFeature\n", FEATURE_MODULE) != []
    assert _check("import osekkai.voice.adapters.typed\n", FEATURE_MODULE) != []


def test_基盤の組み立ての口は読まない() -> None:
    assert _check("from osekkai.__main__ import build_devices\n", FEATURE_MODULE) != []


def test_基盤の口は読んでよい() -> None:
    source = (
        "from osekkai.core.feature import FeatureContext, FeatureSpec\n"
        "from osekkai.core.config import FeatureConfig\n"
        "from osekkai.core import selfcheck, storage, paths, lcd_text\n"
        "from osekkai.core.types import Priority, Resource\n"
    )
    assert _check(source, FEATURE_MODULE) == []
