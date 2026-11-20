"""朝と夜のお話（talk）の読み込みの向きを、ソースを読んで静的に確かめる（12.1）。

お話のパッケージの下のモジュールは、あとから足したものも自動で確かめる。
"""

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

SRC_DIR = Path(__file__).resolve().parents[3] / "src"
TALK_DIR = SRC_DIR / "osekkai" / "features" / "talk"

TALK = "osekkai.features.talk"
TALK_ADAPTERS = "osekkai.features.talk.adapters"
FEATURES = "osekkai.features"
# ほかの機能は、日課とおしゃべりの公開の口（api）だけを読む
OTHER_FEATURE_APIS = ("osekkai.features.routine.api", "osekkai.features.conversation.api")
ADAPTERS = "osekkai.adapters"
# 本体だけが、端末の偽物が動いているかの判定を読む（PC で偽の天気と豆知識を選ぶときだけ）
FEATURE_MODULE = "osekkai.features.talk.feature"
CONSOLE_ALLOWED = "osekkai.adapters.fake.console.typed_input_active"
# 基盤の組み立ての口は、試しのコマンドだけが読む
ENTRY_MODULE = "osekkai.__main__"
ENTRY_MODULES = frozenset({"osekkai.features.talk.trial", "osekkai.features.talk.__main__"})
# 外への問い合わせの標準ライブラリは、お話の adapters の中だけで使う
NETWORK_LIBS = ("urllib", "http", "socket")
# 外の HTTP のライブラリ・ハードのライブラリ・anthropic は、どこでも読まない
NEVER_LIBS = (
    "requests",
    "httpx",
    "aiohttp",
    "anthropic",
    "smbus2",
    "lgpio",
    "numpy",
    "cv2",
    "picamera2",
    "faster_whisper",
    "ctranslate2",
)
# 音声の入口は使わない（話すのは基盤の口で行う）
VOICE = "osekkai.voice"


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
        # 自分のパッケージと、日課とおしゃべりの api だけを読む
        return not (_matches(name, TALK) or any(_matches(name, api) for api in OTHER_FEATURE_APIS))
    if _matches(name, ADAPTERS):
        return not (module == FEATURE_MODULE and _matches(name, CONSOLE_ALLOWED))
    if _matches(name, ENTRY_MODULE):
        return module not in ENTRY_MODULES
    if any(_matches(name, lib) for lib in NETWORK_LIBS):
        return not _matches(module, TALK_ADAPTERS)
    if any(_matches(name, lib) for lib in NEVER_LIBS):
        return True
    return _matches(name, VOICE)


def talk_violations(module: str, refs: list[ImportRef]) -> list[str]:
    return [ref.name for ref in refs if _violation(module, ref)]


TALK_FILES = sorted(TALK_DIR.rglob("*.py"))


def test_talk_のモジュールが見つかる() -> None:
    names = {_module_name(p) for p in TALK_FILES}
    assert {TALK, TALK_ADAPTERS} <= names


@pytest.mark.parametrize("path", TALK_FILES, ids=_module_name)
def test_talk_は決まった向きだけを読み込む(path: Path) -> None:
    assert talk_violations(_module_name(path), _imports(path)) == []


# --- 見張りの仕組みそのものの確かめ ---


def _check(source: str, module: str, *, is_package: bool = False) -> list[str]:
    return talk_violations(module, scan_imports(source, module, is_package=is_package))


def test_日課とおしゃべりの_api_は読んでよい() -> None:
    source = (
        "from osekkai.features.routine.api import RoutineStatus, TaskMoment, current_status\n"
        "from osekkai.features.conversation.api import ChatModelPort, Replied, KID_RULES\n"
        "from osekkai.features.routine import api\n"
        "import osekkai.features.conversation.api\n"
    )
    assert _check(source, f"{TALK}.prepare") == []
    assert _check(source, FEATURE_MODULE) == []


@pytest.mark.parametrize(
    "source",
    [
        "import osekkai.features.routine\n",
        "from osekkai.features.routine.feature import RoutineFeature\n",
        "from osekkai.features.routine.schedule import Scheduler\n",
        "from osekkai.features.conversation.adapters.claude import ClaudeChatModel\n",
        "from osekkai.features.conversation import texts\n",
        "from osekkai.features.presence.api import x\n",
        "from osekkai.features.daruma.feature import DarumaFeature\n",
        "from osekkai import features\n",
        "from .. import routine\n",
        "from ..conversation import feature\n",
        "def f():\n    import osekkai.features.remote\n",
    ],
)
def test_ほかの機能の読み込みを見つける(source: str) -> None:
    assert _check(source, f"{TALK}.flow") != []


def test_自分のパッケージは読んでよい() -> None:
    source = (
        "from . import config\n"
        "from .model import Prepared\n"
        "from osekkai.features.talk.texts import OPENING\n"
        "import osekkai.features.talk.plan\n"
    )
    assert _check(source, f"{TALK}.flow") == []
    assert _check("from ..model import WeatherResult\n", f"{TALK_ADAPTERS}.open_meteo") == []
    assert _check("from .feature import X\n", TALK, is_package=True) == []


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
    for module in (FEATURE_MODULE, f"{TALK}.trial", f"{TALK_ADAPTERS}.canned"):
        assert _check(source, module) != [], module


def test_相対の読み込みで基盤のアダプタを見つける() -> None:
    assert _check("from ...adapters.fake import clock\n", FEATURE_MODULE) != []
    assert _check("from ....adapters.fake import clock\n", f"{TALK_ADAPTERS}.canned") != []


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
    [f"{TALK}.flow", f"{TALK}.trial", f"{TALK}.checks", f"{TALK_ADAPTERS}.canned", TALK],
)
def test_本体のほかは端末の偽物の判定を読まない(module: str) -> None:
    source = "from osekkai.adapters.fake.console import typed_input_active\n"
    assert _check(source, module) != []


def test_試しのコマンドだけは基盤の組み立ての口を読んでよい() -> None:
    source = "from osekkai.__main__ import build_devices\n"
    assert _check(source, f"{TALK}.trial") == []
    assert _check(source, f"{TALK}.__main__") == []
    assert _check("def main():\n    " + source, f"{TALK}.trial") == []


@pytest.mark.parametrize("module", [FEATURE_MODULE, f"{TALK}.checks", f"{TALK_ADAPTERS}", TALK])
def test_試しのコマンドのほかは基盤の組み立ての口を読まない(module: str) -> None:
    assert _check("from osekkai.__main__ import build_devices\n", module) != []
    assert _check("import osekkai.__main__\n", module) != []


@pytest.mark.parametrize(
    "source",
    [
        "import urllib.request\n",
        "from urllib.request import Request, urlopen\n",
        "from urllib.parse import urlencode\n",
        "import urllib\n",
        "import http.client\n",
        "from http.client import HTTPResponse\n",
        "import socket\n",
        "def f():\n    import urllib.request\n",
    ],
)
def test_外への問い合わせは_adapters_の中だけで使う(source: str) -> None:
    for module in (f"{TALK_ADAPTERS}.open_meteo", f"{TALK_ADAPTERS}.jma_alerts", TALK_ADAPTERS):
        assert _check(source, module) == [], module
    for module in (
        FEATURE_MODULE,
        f"{TALK}.prepare",
        f"{TALK}.checks",
        f"{TALK}.trial",
        f"{TALK}.weather_talk",
        TALK,
    ):
        assert _check(source, module) != [], module


def test_json_はどこでも読んでよい() -> None:
    assert _check("import json\n", f"{TALK_ADAPTERS}.open_meteo") == []
    assert _check("import json\n", f"{TALK}.record") == []


@pytest.mark.parametrize("lib", NEVER_LIBS)
def test_外の_HTTP_とハードのライブラリと_anthropic_はどこでも断る(lib: str) -> None:
    source = f"def f():\n    import {lib}\n"
    for module in (FEATURE_MODULE, f"{TALK_ADAPTERS}.open_meteo", f"{TALK}.checks"):
        assert _check(source, module) != [], module
    assert _check(f"from {lib} import x\n", f"{TALK_ADAPTERS}.jma_alerts") != []


def test_音声の入口は読まない() -> None:
    assert _check("from osekkai.voice.api import Heard\n", FEATURE_MODULE) != []
    assert _check("import osekkai.voice\n", f"{TALK}.flow") != []


def test_基盤の口は読んでよい() -> None:
    source = (
        "from osekkai.core.feature import FeatureContext, FeatureSpec\n"
        "from osekkai.core.config import FeatureConfig\n"
        "from osekkai.core import selfcheck, storage, paths, lcd_text, clock\n"
        "from osekkai.core.types import Priority, Resource, Face\n"
        "from osekkai.core.arbiter import Lease\n"
    )
    assert _check(source, FEATURE_MODULE) == []
