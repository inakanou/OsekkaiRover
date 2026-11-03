"""基盤（core）の読み込みの向きを、ソースを読んで静的に確かめる。"""

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
CORE_DIR = SRC_DIR / "osekkai" / "core"

FORBIDDEN_PACKAGES = ("osekkai.adapters", "osekkai.features")
HARDWARE_LIBS = ("smbus2", "lgpio", "numpy")

# 関数の中で遅れて読み込むことを条件に、アダプタを読み込んでよいモジュール（7.6 の安全停止）
LAZY_ADAPTER_ALLOWLIST = frozenset({"osekkai.core.safe_stop"})


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
    # `from x import y` は x.y というモジュールかもしれないので、両方を候補にする
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


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


def forbidden_imports(module: str, refs: list[ImportRef]) -> list[str]:
    bad: list[str] = []
    for ref in refs:
        for package in FORBIDDEN_PACKAGES:
            if not _matches(ref.name, package):
                continue
            lazy_ok = (
                module in LAZY_ADAPTER_ALLOWLIST
                and package == "osekkai.adapters"
                and ref.in_function
            )
            if not lazy_ok:
                bad.append(ref.name)
        bad.extend(ref.name for lib in HARDWARE_LIBS if _matches(ref.name, lib))
    return bad


CORE_FILES = sorted(CORE_DIR.rglob("*.py"))


def test_core_のモジュールが見つかる() -> None:
    names = {_module_name(p) for p in CORE_FILES}
    assert {"osekkai.core", "osekkai.core.types", "osekkai.core.ports"} <= names


@pytest.mark.parametrize("path", CORE_FILES, ids=_module_name)
def test_core_はアダプタと機能とハードのライブラリを読み込まない(path: Path) -> None:
    assert forbidden_imports(_module_name(path), _imports(path)) == []


def test_ports_は設定を型の確認のときだけ参照する() -> None:
    refs = _imports(CORE_DIR / "ports.py")
    config_refs = [r for r in refs if _matches(r.name, "osekkai.core.config")]
    assert config_refs, "ports は VoiceSettings を型として参照するはず"
    assert all(r.in_type_checking for r in config_refs)


# --- 見張りの仕組みそのものの確かめ ---


def test_一番上での_adapters_の読み込みを見つける() -> None:
    refs = scan_imports("from osekkai.adapters.pi import hat\n", "osekkai.core.x")
    assert forbidden_imports("osekkai.core.x", refs) != []


def test_相対の読み込みで_features_を見つける() -> None:
    refs = scan_imports("from ..features import greeting\n", "osekkai.core.x")
    assert "osekkai.features" in forbidden_imports("osekkai.core.x", refs)


def test_ハードのライブラリを見つける() -> None:
    refs = scan_imports("def f():\n    import numpy as np\n", "osekkai.core.x")
    assert forbidden_imports("osekkai.core.x", refs) == ["numpy"]


def test_許可の一覧でも一番上での読み込みは断る() -> None:
    refs = scan_imports("import osekkai.adapters.pi\n", "osekkai.core.safe_stop")
    assert forbidden_imports("osekkai.core.safe_stop", refs) != []


def test_許可の一覧では関数の中の遅れた読み込みを認める() -> None:
    source = "def main():\n    from osekkai.adapters.pi import hat\n"
    refs = scan_imports(source, "osekkai.core.safe_stop")
    assert forbidden_imports("osekkai.core.safe_stop", refs) == []


def test_許可の一覧にないモジュールは関数の中でも断る() -> None:
    source = "def main():\n    from osekkai.adapters.pi import hat\n"
    refs = scan_imports(source, "osekkai.core.runtime")
    assert forbidden_imports("osekkai.core.runtime", refs) != []


def test_許可の一覧でも_features_は関数の中でも断る() -> None:
    source = "def main():\n    import osekkai.features.greeting\n"
    refs = scan_imports(source, "osekkai.core.safe_stop")
    assert forbidden_imports("osekkai.core.safe_stop", refs) != []


def test_TYPE_CHECKING_の中の読み込みを見分ける() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from osekkai.core.config import VoiceSettings\n"
        "else:\n"
        "    import osekkai.core.paths\n"
    )
    refs = {r.name: r.in_type_checking for r in scan_imports(source, "osekkai.core.ports")}
    assert refs["osekkai.core.config"] is True
    assert refs["osekkai.core.paths"] is False
