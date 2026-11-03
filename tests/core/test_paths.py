"""XDG のパス（12.1、12.5）。"""

import stat
from pathlib import Path

import pytest

from osekkai.core.paths import Paths, ensure_private_dir


def test_環境変数のディレクトリの下にosekkaiの置き場所を作る(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    assert paths.config_dir == xdg_dirs["XDG_CONFIG_HOME"] / "osekkai"
    assert paths.config_file == xdg_dirs["XDG_CONFIG_HOME"] / "osekkai" / "config.yaml"
    assert paths.state_dir == xdg_dirs["XDG_STATE_HOME"] / "osekkai"
    assert paths.state_dir_for("core") == xdg_dirs["XDG_STATE_HOME"] / "osekkai" / "core"
    assert paths.cache_dir == xdg_dirs["XDG_CACHE_HOME"] / "osekkai"
    assert paths.cache_dir_for("core") == xdg_dirs["XDG_CACHE_HOME"] / "osekkai" / "core"
    assert paths.data_dir == xdg_dirs["XDG_DATA_HOME"] / "osekkai"
    assert paths.runtime_dir == xdg_dirs["XDG_RUNTIME_DIR"] / "osekkai"
    assert paths.lock_file == xdg_dirs["XDG_RUNTIME_DIR"] / "osekkai" / "rover.lock"


@pytest.mark.parametrize("value", [None, "", "relative/dir"])
def test_未設定や相対パスなら既定値を使う(tmp_path: Path, value: str | None) -> None:
    names = ["XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"]
    env = {} if value is None else dict.fromkeys(names, value)
    env_runtime = {} if value is None else {"XDG_RUNTIME_DIR": value}
    paths = Paths(env={**env, **env_runtime}, home=tmp_path, uid=1234)
    assert paths.config_dir == tmp_path / ".config" / "osekkai"
    assert paths.state_dir == tmp_path / ".local" / "state" / "osekkai"
    assert paths.cache_dir == tmp_path / ".cache" / "osekkai"
    assert paths.data_dir == tmp_path / ".local" / "share" / "osekkai"
    assert paths.runtime_dir == Path("/tmp/osekkai-1234")
    assert paths.lock_file == Path("/tmp/osekkai-1234/rover.lock")


def test_作っただけではディレクトリを作らない(xdg_dirs: dict[str, Path]) -> None:
    paths = Paths()
    assert not paths.state_dir.exists()
    assert not paths.cache_dir.exists()


@pytest.mark.parametrize("name", ["", "..", "a/b", "A", "../x", "a.b"])
def test_機能の名前でパスの外に出られない(xdg_dirs: dict[str, Path], name: str) -> None:
    paths = Paths()
    with pytest.raises(ValueError):
        paths.state_dir_for(name)
    with pytest.raises(ValueError):
        paths.cache_dir_for(name)


def test_置き場所は本人だけが読み書きできる権限で作る(tmp_path: Path) -> None:
    target = tmp_path / "osekkai" / "core"
    ensure_private_dir(target)
    for path in (tmp_path / "osekkai", target):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
