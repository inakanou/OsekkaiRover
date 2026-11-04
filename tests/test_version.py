import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

from osekkai import version


def _fake_run(stdout: str = "", returncode: int = 0, exc: Exception | None = None) -> Any:
    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(
            args=args, returncode=returncode, stdout=stdout, stderr=""
        )

    return run


def test_git_describe_が使えればその結果を返す(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="v0.1.0-3-gabc1234\n"))
    monkeypatch.setattr(metadata, "version", lambda name: "9.9.9")

    assert version.get_version() == "v0.1.0-3-gabc1234"


def test_git_describe_が失敗したらパッケージの版を返す(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_run(returncode=128))
    monkeypatch.setattr(metadata, "version", lambda name: "0.1.0")

    assert version.get_version() == "0.1.0"


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("git"),
        subprocess.TimeoutExpired(cmd="git", timeout=2),
        PermissionError("git"),
    ],
)
def test_git_が使えなければパッケージの版を返す(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_run(exc=exc))
    monkeypatch.setattr(metadata, "version", lambda name: "0.1.0")

    assert version.get_version() == "0.1.0"


def test_git_describe_の出力が空ならパッケージの版を返す(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="  \n"))
    monkeypatch.setattr(metadata, "version", lambda name: "0.1.0")

    assert version.get_version() == "0.1.0"


def test_パッケージの版も無ければ_unknown_を返す(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(subprocess, "run", _fake_run(returncode=128))
    monkeypatch.setattr(metadata, "version", missing)

    assert version.get_version() == "unknown"


def test_git_はパッケージのディレクトリで実行する(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="abc1234\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    assert version.get_version() == "abc1234"
    assert calls[0]["cmd"][:2] == ["git", "describe"]
    assert calls[0]["cwd"] == Path(version.__file__).resolve().parent
    assert calls[0]["timeout"] > 0
