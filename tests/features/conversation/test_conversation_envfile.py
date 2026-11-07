"""鍵のファイルを読む口（8.1、8.3、9.2）。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from osekkai.core.paths import Paths
from osekkai.features.conversation.envfile import VARIABLE_NAME, SetupProblem, read_setup

fake_value = "abc123"  # 偽の値（10 文字未満）


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(env={"XDG_CONFIG_HOME": str(tmp_path / "config")}, home=tmp_path)


def _write(paths: Paths, text: str, mode: int = 0o600) -> Path:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    target = paths.config_dir / "env"
    target.write_text(text, encoding="utf-8")
    os.chmod(target, mode)
    return target


def test_private_file_returns_value(paths: Paths) -> None:
    # 場所は設定のディレクトリの env（リポジトリの外。8.3）
    _write(paths, f"{VARIABLE_NAME}={fake_value}\n")
    assert read_setup(paths) == fake_value


@pytest.mark.parametrize(
    "line",
    [
        f'{VARIABLE_NAME}="{fake_value}"',
        f"{VARIABLE_NAME}='{fake_value}'",
        f"  {VARIABLE_NAME} =  {fake_value}  ",
        f'{VARIABLE_NAME} = " {fake_value} "',
    ],
)
def test_strips_spaces_and_quotes(paths: Paths, line: str) -> None:
    _write(paths, line + "\n")
    assert read_setup(paths) == fake_value


def test_skips_comments_blank_and_other_names(paths: Paths) -> None:
    _write(
        paths,
        "\n".join(
            [
                "# メモ",
                f"# {VARIABLE_NAME}=old",
                "",
                "   ",
                "OTHER=zzz",
                "これは行ではない",
                f"{VARIABLE_NAME}={fake_value}",
            ]
        ),
    )
    assert read_setup(paths) == fake_value


def test_similar_name_is_not_the_line(paths: Paths) -> None:
    _write(paths, f"{VARIABLE_NAME}_OLD={fake_value}\n")
    assert read_setup(paths) is SetupProblem.NOT_WRITTEN


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o620, 0o602, 0o660], ids=oct)
def test_others_can_access_is_not_private(paths: Paths, mode: int) -> None:
    _write(paths, f"{VARIABLE_NAME}={fake_value}\n", mode)
    assert read_setup(paths) is SetupProblem.NOT_PRIVATE


def test_owner_only_read_is_private(paths: Paths) -> None:
    _write(paths, f"{VARIABLE_NAME}={fake_value}\n", 0o400)
    assert read_setup(paths) == fake_value


def test_other_owner_is_not_private(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(paths, f"{VARIABLE_NAME}={fake_value}\n")
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    assert read_setup(paths) is SetupProblem.NOT_PRIVATE


@pytest.mark.parametrize(
    "text",
    [
        "",
        "OTHER=zzz\n",
        f"{VARIABLE_NAME}=\n",
        f"{VARIABLE_NAME}=   \n",
        f'{VARIABLE_NAME}=""\n',
        f"# {VARIABLE_NAME}={fake_value}\n",
    ],
)
def test_missing_or_empty_line_is_not_written(paths: Paths, text: str) -> None:
    _write(paths, text)
    assert read_setup(paths) is SetupProblem.NOT_WRITTEN


def test_no_file(paths: Paths) -> None:
    assert read_setup(paths) is SetupProblem.NO_FILE


def test_no_config_dir(tmp_path: Path) -> None:
    paths = Paths(env={"XDG_CONFIG_HOME": str(tmp_path / "nowhere")}, home=tmp_path)
    assert read_setup(paths) is SetupProblem.NO_FILE


def test_directory_in_place_of_file_is_unreadable(paths: Paths) -> None:
    (paths.config_dir / "env").mkdir(parents=True)
    os.chmod(paths.config_dir / "env", 0o700)
    assert read_setup(paths) is SetupProblem.UNREADABLE


def test_not_utf8_is_unreadable(paths: Paths) -> None:
    target = _write(paths, "")
    target.write_bytes(b"\xff\xfe" + f"{VARIABLE_NAME}={fake_value}".encode())
    assert read_setup(paths) is SetupProblem.UNREADABLE


def test_read_error_is_unreadable(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(paths, f"{VARIABLE_NAME}={fake_value}\n")

    def broken(*args: object, **kwargs: object) -> int:
        raise PermissionError(13, f"denied {fake_value}")

    monkeypatch.setattr(os, "open", broken)
    assert read_setup(paths) is SetupProblem.UNREADABLE


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        (f"{VARIABLE_NAME}={fake_value}\n", 0o600),
        (f"{VARIABLE_NAME}={fake_value}\n", 0o644),
        (f"OTHER={fake_value}\n", 0o600),
    ],
)
def test_logs_never_show_value_or_line(
    paths: Paths, caplog: pytest.LogCaptureFixture, text: str, mode: int
) -> None:
    _write(paths, text, mode)
    with caplog.at_level(logging.DEBUG):
        read_setup(paths)
    assert fake_value not in caplog.text
    assert "OTHER" not in caplog.text


def test_read_error_message_does_not_escape(
    paths: Paths, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 読み出しの誤りは例外にせず種類で返す（例外の文に中身が乗って外に出ない）
    _write(paths, f"{VARIABLE_NAME}={fake_value}\n")

    def broken(*args: object, **kwargs: object) -> int:
        raise OSError(5, f"io {fake_value}")

    monkeypatch.setattr(os, "open", broken)
    with caplog.at_level(logging.DEBUG):
        assert read_setup(paths) is SetupProblem.UNREADABLE
    assert fake_value not in caplog.text


def test_last_named_line_wins(paths: Paths) -> None:
    _write(paths, f"{VARIABLE_NAME}=old\n{VARIABLE_NAME}={fake_value}\n")
    assert read_setup(paths) == fake_value


def test_named_pipe_is_unreadable_without_waiting(paths: Paths) -> None:
    paths.config_dir.mkdir(parents=True)
    os.mkfifo(paths.config_dir / "env", 0o600)
    assert read_setup(paths) is SetupProblem.UNREADABLE
