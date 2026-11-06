"""道具のパッケージの決まりのテスト（標準ライブラリだけ、`osekkai` を読み込まない）と入口。"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO / "deploy"
PACKAGE_DIR = DEPLOY_DIR / "rover_deploy"
ENTRY = DEPLOY_DIR / "rover-deploy"

TOOL_FILES = [*sorted(PACKAGE_DIR.rglob("*.py")), ENTRY]


def _imported_top_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_道具のファイルが見つかる() -> None:
    names = {p.name for p in TOOL_FILES}
    assert {"__init__.py", "common.py", "system.py", "devcli.py", "rover-deploy"} <= names


@pytest.mark.parametrize("path", TOOL_FILES, ids=lambda p: p.name)
def test_道具は_osekkai_を読み込まない(path: Path) -> None:
    assert "osekkai" not in _imported_top_names(path)


@pytest.mark.parametrize("path", TOOL_FILES, ids=lambda p: p.name)
def test_道具は標準ライブラリと自分だけを読み込む(path: Path) -> None:
    allowed = set(sys.stdlib_module_names) | {"rover_deploy", "__future__"}
    assert _imported_top_names(path) - allowed == set()


def test_見張りは_osekkai_の読み込みを見つける(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def f():\n    from osekkai.core import paths\n", encoding="utf-8")
    assert "osekkai" in _imported_top_names(bad)


def _run_isolated(code: str) -> subprocess.CompletedProcess[str]:
    # -I で、開発の環境の osekkai を読み込めないようにする
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, "-I", "-c", code, str(DEPLOY_DIR)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )


def test_道具を読み込んでも_osekkai_は読み込まれない() -> None:
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import rover_deploy, rover_deploy.common, rover_deploy.system, rover_deploy.devcli; "
        "print([m for m in sys.modules if m == 'osekkai' or m.startswith('osekkai.')])"
    )
    result = _run_isolated(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_入口の実行ファイルは使い方を出す() -> None:
    assert os.access(ENTRY, os.X_OK)
    assert ENTRY.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3\n")
    result = subprocess.run(
        [sys.executable, "-I", str(ENTRY), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "rover-deploy" in result.stdout


def test_入口はサブコマンドがなければ使い方の誤りで終える(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from rover_deploy import devcli

    assert devcli.main([]) == 2
    assert "rover-deploy" in capsys.readouterr().err


def test_入口は知らない引数を使い方の誤りにする() -> None:
    from rover_deploy import devcli

    with pytest.raises(SystemExit) as info:
        devcli.main(["--no-such-option"])
    assert info.value.code == 2
