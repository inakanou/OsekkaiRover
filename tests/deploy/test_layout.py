"""実機の置き場所のテスト（10.2）。"""

import stat
from pathlib import Path

import pytest

from rover_deploy.layout import (
    CORE_UNIT_NAME,
    SAMPLE_UNIT_NAME,
    Layout,
    ensure_private_dir,
)

SHA = "0123456789abcdef0123456789abcdef01234567"


def make(home: Path, env: dict[str, str] | None = None, uid: int = 1000) -> Layout:
    return Layout(env={} if env is None else env, home=home, uid=uid)


def test_新しい基盤の置き場所は_osekkai_rover_にまとまる(tmp_path: Path) -> None:
    layout = make(tmp_path)
    root = tmp_path / "osekkai-rover"
    assert layout.root == root
    assert layout.repo_git == root / "repo.git"
    assert layout.releases_dir == root / "releases"
    assert layout.current_link == root / "current"
    assert layout.tool_dir == root / "tool"


def test_版のディレクトリは_sha_の先頭_12_桁(tmp_path: Path) -> None:
    layout = make(tmp_path)
    assert layout.release_dir(SHA) == tmp_path / "osekkai-rover/releases/0123456789ab"


@pytest.mark.parametrize("bad", ["", "abc", "../" + SHA[:37], SHA.upper(), SHA + "0"])
def test_版のディレクトリは_40_桁の_sha_だけを受け取る(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        make(tmp_path).release_dir(bad)


def test_ユニットの置き場所(tmp_path: Path) -> None:
    layout = make(tmp_path)
    unit_dir = tmp_path / ".config/systemd/user"
    assert layout.user_unit_dir == unit_dir
    assert layout.core_unit_file == unit_dir / "osekkai.service"
    assert layout.sample_unit_file == unit_dir / "osekkai-rover.service"
    assert CORE_UNIT_NAME == "osekkai.service"
    assert SAMPLE_UNIT_NAME == "osekkai-rover.service"


def test_道具の状態のディレクトリ(tmp_path: Path) -> None:
    layout = make(tmp_path)
    state = tmp_path / ".local/state/osekkai-deploy"
    assert layout.state_dir == state
    assert layout.state_file == state / "state.json"
    assert layout.lock_file == state / "deploy.lock"
    assert layout.log_dir == state / "log"
    assert layout.backups_dir == state / "backups"


def test_基盤の_XDG_の置き場所(tmp_path: Path) -> None:
    layout = make(tmp_path)
    assert layout.core_config_dir == tmp_path / ".config/osekkai"
    assert layout.core_state_dir == tmp_path / ".local/state/osekkai"


def test_XDG_の変数があればそれを使う(tmp_path: Path) -> None:
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_STATE_HOME": str(tmp_path / "st"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
    }
    layout = make(tmp_path / "home", env)
    assert layout.user_unit_dir == tmp_path / "cfg/systemd/user"
    assert layout.core_config_dir == tmp_path / "cfg/osekkai"
    assert layout.state_dir == tmp_path / "st/osekkai-deploy"
    assert layout.core_state_dir == tmp_path / "st/osekkai"
    assert layout.runtime_dir == tmp_path / "run/osekkai"


def test_相対パスの_XDG_の変数は無視する(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": "rel/state", "XDG_RUNTIME_DIR": "rel/run"}
    layout = make(tmp_path, env, uid=1234)
    assert layout.state_dir == tmp_path / ".local/state/osekkai-deploy"
    assert layout.runtime_dir == Path("/tmp/osekkai-1234")


def test_実行時のディレクトリがないときは基盤と同じ決まり(tmp_path: Path) -> None:
    # rover-core の Paths.runtime_dir と同じ: XDG_RUNTIME_DIR が空なら /tmp/osekkai-<uid>
    assert make(tmp_path, {"XDG_RUNTIME_DIR": ""}, uid=1234).runtime_dir == Path(
        "/tmp/osekkai-1234"
    )
    assert make(tmp_path, {}, uid=42).runtime_dir == Path("/tmp/osekkai-42")
    layout = make(tmp_path, {"XDG_RUNTIME_DIR": "/run/user/1000"})
    assert layout.runtime_dir == Path("/run/user/1000/osekkai")


def test_サンプルの場所(tmp_path: Path) -> None:
    layout = make(tmp_path)
    assert layout.sample_dir == tmp_path / "works/OsekkaiRover"
    assert layout.sample_venv == tmp_path / ".venvs/osekkai"
    assert not layout.sample_exists()
    (tmp_path / "works/OsekkaiRover/.git").mkdir(parents=True)
    assert layout.sample_exists()


def test_作ったあとで環境が変わっても置き場所は変わらない(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path / "st")}
    layout = make(tmp_path, env)
    env["XDG_STATE_HOME"] = str(tmp_path / "other")
    assert layout.state_dir == tmp_path / "st/osekkai-deploy"


def test_作るだけではディレクトリを作らない(tmp_path: Path) -> None:
    make(tmp_path).state_dir  # noqa: B018
    assert list(tmp_path.iterdir()) == []


def test_自分だけのディレクトリを_0700_で作る(tmp_path: Path) -> None:
    target = tmp_path / "a/b/c"
    assert ensure_private_dir(target) == target
    for p in (tmp_path / "a", tmp_path / "a/b", target):
        assert stat.S_IMODE(p.stat().st_mode) == 0o700
    # もとからあるものも 0700 にそろえる（道具だけが使う場所なので）
    target.chmod(0o755)
    ensure_private_dir(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
