"""開発 PC の利用者の設定を読むテスト（3.3、10.1、10.2）。"""

import shutil
import subprocess
from pathlib import Path

import pytest
from fake_runner import FakeRunner

from rover_deploy.common import ExitCode
from rover_deploy.devconfig import (
    CONFIG_ENV,
    ConfigError,
    DevConfig,
    https_from_origin,
    load_dev_config,
)
from rover_deploy.system import CommandNotFound, CommandTimedOut, SubprocessRunner

ORIGIN_SSH = "git@github.com:owner/repo.git"
URL = "https://github.com/owner/repo.git"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def write_default_config(home: Path, text: str) -> Path:
    path = home / ".config" / "osekkai-deploy" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def origin_argv(repo: Path) -> list[str]:
    return ["git", "-C", str(repo), "remote", "get-url", "origin"]


def script_outside_git(run: FakeRunner) -> None:
    """バックアップの置き場所は git の作業コピーの外（rev-parse が 128 で終わる）。"""
    run.script(["git", "-C"], returncode=128)


def script_origin(run: FakeRunner, repo: Path, url: str = ORIGIN_SSH) -> None:
    run.script(origin_argv(repo), stdout=url + "\n")


def rev_parse_calls(run: FakeRunner) -> list[tuple[str, ...]]:
    return [argv for argv in run.argvs if argv[3:] == ("rev-parse", "--is-inside-work-tree")]


# --- 既定値 ---


def test_defaults_from_default_location(home: Path, repo: Path, fake_runner: FakeRunner) -> None:
    write_default_config(home, 'host = "rover"\n')
    script_outside_git(fake_runner)
    script_origin(fake_runner, repo)

    config = load_dev_config({}, repo, fake_runner, home=home)

    assert config == DevConfig(
        host="rover",
        backup_dir=(home / ".local" / "share" / "osekkai-deploy" / "backups").resolve(),
        repo_url=URL,
    )


def test_env_var_selects_config_file(
    tmp_path: Path, home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    write_default_config(home, 'host = "other"\n')
    chosen = tmp_path / "chosen.toml"
    chosen.write_text('host = "rover"\n', encoding="utf-8")
    script_outside_git(fake_runner)
    script_origin(fake_runner, repo)

    config = load_dev_config({CONFIG_ENV: str(chosen)}, repo, fake_runner, home=home)

    assert config.host == "rover"


def test_empty_env_var_falls_back_to_default(
    home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    write_default_config(home, 'host = "rover"\n')
    script_outside_git(fake_runner)
    script_origin(fake_runner, repo)

    config = load_dev_config({CONFIG_ENV: ""}, repo, fake_runner, home=home)

    assert config.host == "rover"


def test_backup_dir_expands_tilde(home: Path, repo: Path, fake_runner: FakeRunner) -> None:
    write_default_config(home, 'host = "rover"\nbackup_dir = "~/rover-backups"\n')
    script_outside_git(fake_runner)
    script_origin(fake_runner, repo)

    config = load_dev_config({}, repo, fake_runner, home=home)

    assert config.backup_dir == (home / "rover-backups").resolve()
    # まだない置き場所は、いちばん近いある親で git の作業コピーかを確かめる
    assert rev_parse_calls(fake_runner) == [
        ("git", "-C", str(home.resolve()), "rev-parse", "--is-inside-work-tree")
    ]


def test_explicit_repo_url_skips_origin(home: Path, repo: Path, fake_runner: FakeRunner) -> None:
    write_default_config(home, f'host = "rover"\nrepo_url = "{URL}"\n')
    script_outside_git(fake_runner)

    config = load_dev_config({}, repo, fake_runner, home=home)

    assert config.repo_url == URL
    assert tuple(origin_argv(repo)) not in fake_runner.argvs


# --- 接続先がないときの案内 ---


def assert_guidance(error: ConfigError) -> None:
    message = str(error)
    assert CONFIG_ENV in message
    assert "~/.config/osekkai-deploy/config.toml" in message
    assert 'host = "rover"' in message
    assert error.exit_code == ExitCode.ABORTED


def test_missing_file_shows_guidance(home: Path, repo: Path, fake_runner: FakeRunner) -> None:
    with pytest.raises(ConfigError) as caught:
        load_dev_config({}, repo, fake_runner, home=home)

    assert_guidance(caught.value)
    assert fake_runner.calls == []


def test_missing_env_file_shows_guidance_without_path(
    tmp_path: Path, home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    missing = tmp_path / "ひみつの場所" / "config.toml"

    with pytest.raises(ConfigError) as caught:
        load_dev_config({CONFIG_ENV: str(missing)}, repo, fake_runner, home=home)

    assert_guidance(caught.value)
    assert "ひみつの場所" not in str(caught.value)


@pytest.mark.parametrize("text", ["", 'backup_dir = "~/b"\n', 'host = ""\n'])
def test_missing_host_shows_guidance(
    text: str, home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    write_default_config(home, text)

    with pytest.raises(ConfigError) as caught:
        load_dev_config({}, repo, fake_runner, home=home)

    assert_guidance(caught.value)
    assert fake_runner.calls == []


@pytest.mark.parametrize(
    "value",
    [
        '"-oProxyCommand=evil"',
        '"ro ver"',
        '"rover;reboot"',
        '"rover$(x)"',
        '"rover\\nx"',
        "42",
        '["rover"]',
    ],
)
def test_bad_host_is_refused_without_echo(
    value: str, home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    write_default_config(home, f"host = {value}\n")

    with pytest.raises(ConfigError) as caught:
        load_dev_config({}, repo, fake_runner, home=home)

    message = str(caught.value)
    assert "evil" not in message and "reboot" not in message
    assert caught.value.exit_code == ExitCode.ABORTED
    assert fake_runner.calls == []


@pytest.mark.parametrize("host", ["rover", "pi@rover.local", "rover-2", "my_rover.lan"])
def test_good_hosts_are_accepted(
    host: str, home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    write_default_config(home, f'host = "{host}"\n')
    script_outside_git(fake_runner)
    script_origin(fake_runner, repo)

    assert load_dev_config({}, repo, fake_runner, home=home).host == host


def test_broken_toml_is_refused_without_content(
    home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    write_default_config(home, 'host = "ひみつ\n')

    with pytest.raises(ConfigError) as caught:
        load_dev_config({}, repo, fake_runner, home=home)

    assert "ひみつ" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.exit_code == ExitCode.ABORTED


def test_config_path_that_is_a_directory_is_refused(
    tmp_path: Path, home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    with pytest.raises(ConfigError):
        load_dev_config({CONFIG_ENV: str(tmp_path)}, repo, fake_runner, home=home)


# --- バックアップの置き場所（作業コピーの中を拒む） ---


@pytest.mark.parametrize("value", ['"relative/backups"', '"~other/backups"', "7", '""'])
def test_bad_backup_dir_is_refused(
    value: str, home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    write_default_config(home, f'host = "rover"\nbackup_dir = {value}\n')

    with pytest.raises(ConfigError):
        load_dev_config({}, repo, fake_runner, home=home)


def test_backup_dir_inside_repo_root_is_refused(
    home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    write_default_config(home, f'host = "rover"\nbackup_dir = "{repo / "backups"}"\n')

    with pytest.raises(ConfigError) as caught:
        load_dev_config({}, repo, fake_runner, home=home)

    assert "作業コピー" in str(caught.value)
    assert str(repo) not in str(caught.value)


def test_backup_dir_equal_to_repo_root_is_refused(
    home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    write_default_config(home, f'host = "rover"\nbackup_dir = "{repo}"\n')

    with pytest.raises(ConfigError):
        load_dev_config({}, repo, fake_runner, home=home)


def test_backup_dir_through_symlink_into_repo_is_refused(
    tmp_path: Path, home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    link = tmp_path / "link"
    link.symlink_to(repo)
    write_default_config(home, f'host = "rover"\nbackup_dir = "{link / "backups"}"\n')

    with pytest.raises(ConfigError):
        load_dev_config({}, repo, fake_runner, home=home)


@pytest.mark.parametrize("answer", ["true\n", "false\n"])
def test_backup_dir_inside_other_git_work_tree_is_refused(
    answer: str, tmp_path: Path, home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    other = tmp_path / "other"
    (other / "deep").mkdir(parents=True)
    write_default_config(home, f'host = "rover"\nbackup_dir = "{other / "deep" / "b" / "c"}"\n')
    # "false" は .git の中か bare の写しの中。どちらも拒む
    fake_runner.script(["git", "-C"], stdout=answer)

    with pytest.raises(ConfigError) as caught:
        load_dev_config({}, repo, fake_runner, home=home)

    assert "作業コピー" in str(caught.value)
    assert rev_parse_calls(fake_runner) == [
        ("git", "-C", str((other / "deep").resolve()), "rev-parse", "--is-inside-work-tree")
    ]


@pytest.mark.parametrize("error", [CommandNotFound("git"), CommandTimedOut("git")])
def test_backup_dir_check_failure_is_refused(
    error: Exception, home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    write_default_config(home, 'host = "rover"\n')
    fake_runner.raise_on(["git", "-C"], error)

    with pytest.raises(ConfigError) as caught:
        load_dev_config({}, repo, fake_runner, home=home)

    assert caught.value.__cause__ is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git がない")
def test_backup_dir_inside_real_git_work_tree_is_refused(
    tmp_path: Path, home: Path, repo: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    write_default_config(home, f'host = "rover"\nbackup_dir = "{other / "backups"}"\n')

    with pytest.raises(ConfigError) as caught:
        load_dev_config({CONFIG_ENV: ""}, repo, SubprocessRunner(), home=home)

    assert "作業コピー" in str(caught.value)


# --- リポジトリの URL ---


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("git@github.com:owner/repo.git", "https://github.com/owner/repo.git"),
        ("git@github.com:owner/repo", "https://github.com/owner/repo"),
        ("ssh://git@github.com/owner/repo.git", "https://github.com/owner/repo.git"),
        ("ssh://git@github.com:22/owner/repo.git", "https://github.com/owner/repo.git"),
        ("https://github.com/owner/repo.git", "https://github.com/owner/repo.git"),
        ("https://user:token@github.com/owner/repo.git", "https://github.com/owner/repo.git"),
    ],
)
def test_https_from_origin(origin: str, expected: str) -> None:
    assert https_from_origin(origin) == expected


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "/local/path/repo.git",
        "file:///local/repo.git",
        "http://github.com/owner/repo.git",
        "git://github.com/owner/repo.git",
        "git@github.com:",
        "ssh://git@github.com/",
        "https://github.com/owner repo.git",
    ],
)
def test_unknown_origin_needs_repo_url(origin: str) -> None:
    with pytest.raises(ConfigError) as caught:
        https_from_origin(origin)

    assert "repo_url" in str(caught.value)


def test_origin_is_converted_when_loading(home: Path, repo: Path, fake_runner: FakeRunner) -> None:
    write_default_config(home, 'host = "rover"\n')
    script_outside_git(fake_runner)
    script_origin(fake_runner, repo, "ssh://git@github.com/owner/repo.git")

    assert load_dev_config({}, repo, fake_runner, home=home).repo_url == URL


def test_origin_failure_asks_for_repo_url(home: Path, repo: Path, fake_runner: FakeRunner) -> None:
    write_default_config(home, 'host = "rover"\n')
    script_outside_git(fake_runner)
    fake_runner.script(origin_argv(repo), returncode=2, stderr="ひみつ")

    with pytest.raises(ConfigError) as caught:
        load_dev_config({}, repo, fake_runner, home=home)

    assert "repo_url" in str(caught.value)
    assert "ひみつ" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "value",
    [
        '"http://github.com/owner/repo.git"',
        '"git@github.com:owner/repo.git"',
        '"https://user:token@github.com/owner/repo.git"',
        '"https://"',
        "1",
    ],
)
def test_bad_repo_url_is_refused_without_echo(
    value: str, home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    write_default_config(home, f'host = "rover"\nrepo_url = {value}\n')
    script_outside_git(fake_runner)

    with pytest.raises(ConfigError) as caught:
        load_dev_config({}, repo, fake_runner, home=home)

    assert "token" not in str(caught.value)


# --- 見直しで足したもの ---


@pytest.mark.parametrize(
    "origin",
    [
        "git@evil?x#:owner/repo",
        "git@[::1]:o/r",
        "git@github.com:owner/repo?x=1#f",
        "git@github.com:owner/re\x01po",
        "ssh://git@github.com/owner/repo?x=1",
        "https://github.com/owner/repo#f",
    ],
)
def test_origin_result_must_pass_repo_url_rule(origin: str) -> None:
    with pytest.raises(ConfigError) as caught:
        https_from_origin(origin)

    assert "repo_url" in str(caught.value)


@pytest.mark.parametrize(
    "origin", ["github-work:owner/repo.git", "ssh://git@github-work/owner/repo.git"]
)
def test_ssh_alias_origin_needs_repo_url(origin: str) -> None:
    with pytest.raises(ConfigError) as caught:
        https_from_origin(origin)

    assert "repo_url" in str(caught.value)


@pytest.mark.parametrize("key", ["host", "repo_url"])
@pytest.mark.parametrize("char", ["\\u0000", "\\u001f", "\\u007f"])
def test_control_characters_are_refused(
    key: str, char: str, home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    host = f"rover{char}" if key == "host" else "rover"
    url = f"https://github.com/owner/repo{char}.git" if key == "repo_url" else URL
    write_default_config(home, f'host = "{host}"\nrepo_url = "{url}"\n')
    script_outside_git(fake_runner)

    with pytest.raises(ConfigError):
        load_dev_config({}, repo, fake_runner, home=home)


@pytest.mark.parametrize("host", ["pi@-oProxyCommand", "pi@", "pi@.rover", "a@b@-x"])
def test_host_part_after_at_must_start_with_letter_or_digit(
    host: str, home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    write_default_config(home, f'host = "{host}"\n')

    with pytest.raises(ConfigError):
        load_dev_config({}, repo, fake_runner, home=home)


def test_default_backup_dir_inside_work_tree_explains_how_to_fix(
    home: Path, repo: Path, fake_runner: FakeRunner
) -> None:
    write_default_config(home, 'host = "rover"\n')
    fake_runner.script(["git", "-C"], stdout="true\n")

    with pytest.raises(ConfigError) as caught:
        load_dev_config({}, repo, fake_runner, home=home)

    message = str(caught.value)
    assert "作業コピー" in message
    assert "書かないときの既定" in message
    assert "backup_dir を書いて" in message


@pytest.mark.parametrize("error", [PermissionError(13, "x"), RuntimeError("loop")])
def test_backup_dir_resolve_errors_become_config_error(
    error: Exception,
    home: Path,
    repo: Path,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_default_config(home, 'host = "rover"\nbackup_dir = "~/b"\n')

    def broken(self: Path, strict: bool = False) -> Path:
        raise error

    monkeypatch.setattr(Path, "resolve", broken)

    with pytest.raises(ConfigError) as caught:
        load_dev_config({}, repo, fake_runner, home=home)

    assert caught.value.__cause__ is None


def test_backup_dir_exists_errors_become_config_error(
    home: Path, repo: Path, fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_default_config(home, 'host = "rover"\nbackup_dir = "/no/such/place"\n')

    def broken(self: Path, **kwargs: object) -> bool:
        raise PermissionError(13, "x")

    monkeypatch.setattr(Path, "exists", broken)

    with pytest.raises(ConfigError):
        load_dev_config({}, repo, fake_runner, home=home)
