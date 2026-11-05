"""外のコマンドを呼ぶ口と、systemctl・journalctl・git の薄い包みのテスト。"""

import sys
from pathlib import Path

import pytest
from fake_runner import FakeRunner

from rover_deploy import system
from rover_deploy.system import (
    CommandFailed,
    CommandNotFound,
    CommandTimedOut,
    Runner,
    RunResult,
    SubprocessRunner,
    UnexpectedOutput,
    UnitInfo,
)

SECRET = "ひみつのなまえ"
PY = sys.executable


# --- 本物の Runner ---


def test_本物の_Runner_は終了コードと出力を返す() -> None:
    run: Runner = SubprocessRunner()
    code = "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(3)"
    result = run([PY, "-c", code])
    assert result == RunResult(3, b"out", b"err")


def test_本物の_Runner_は標準入力を渡す() -> None:
    code = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])"
    result = SubprocessRunner()([PY, "-c", code], input=b"abc")
    assert result.stdout == b"cba"


def test_本物の_Runner_は入力がなければ標準入力を空にする() -> None:
    code = "import sys; sys.stdout.write(repr(sys.stdin.read()))"
    result = SubprocessRunner()([PY, "-c", code], timeout=10)
    assert result.text == "''"


def test_本物の_Runner_は場所と環境変数を渡す(tmp_path: Path) -> None:
    code = "import os; print(os.getcwd()); print(os.environ.get('ROVER_X', ''))"
    result = SubprocessRunner()([PY, "-c", code], cwd=tmp_path, env={"ROVER_X": "1"})
    cwd, value = result.text.splitlines()
    assert Path(cwd).resolve() == tmp_path.resolve()
    assert value == "1"


def test_本物の_Runner_は_check_で失敗を例外にし標準エラーを写さない() -> None:
    code = f"import sys; sys.stderr.write({SECRET!r}); sys.exit(4)"
    with pytest.raises(CommandFailed) as info:
        SubprocessRunner()([PY, "-c", code, SECRET], check=True)
    assert info.value.returncode == 4
    assert info.value.program == Path(PY).name
    assert SECRET not in str(info.value)
    assert SECRET not in repr(info.value)


def test_本物の_Runner_は時間切れを例外にする() -> None:
    with pytest.raises(CommandTimedOut) as info:
        SubprocessRunner()([PY, "-c", "import time; time.sleep(5)"], timeout=0.2)
    assert info.value.program == Path(PY).name


def test_本物の_Runner_はないコマンドを例外にする() -> None:
    with pytest.raises(CommandNotFound) as info:
        SubprocessRunner()(["/nonexistent/rover-deploy-no-such-command"])
    assert info.value.program == "rover-deploy-no-such-command"


def test_結果の表示に出力を出さない() -> None:
    result = RunResult(0, SECRET.encode(), SECRET.encode())
    assert SECRET.encode() not in repr(result).encode()
    assert SECRET not in repr(result)


def test_出力の文字は壊れていても読める() -> None:
    assert RunResult(0, b"a\xffb", b"").text == "a�b"


# --- 偽の Runner ---


def test_偽の_Runner_は呼び出しを記録し決めた結果を返す(fake_runner: FakeRunner) -> None:
    fake_runner.script(["git", "describe"], stdout="v1\n")
    run: Runner = fake_runner
    result = run(["git", "describe", "--tags"], input=b"x", timeout=3.0)
    assert result.text == "v1\n"
    assert fake_runner.argvs == [("git", "describe", "--tags")]
    call = fake_runner.calls[0]
    assert (call.input, call.timeout, call.check) == (b"x", 3.0, False)


def test_偽の_Runner_はいちばん長く一致する決まりを使う(fake_runner: FakeRunner) -> None:
    fake_runner.script(["git"], stdout="short")
    fake_runner.script(["git", "status"], stdout="long")
    assert fake_runner(["git", "status", "--porcelain"]).text == "long"
    assert fake_runner(["git", "log"]).text == "short"


def test_偽の_Runner_は順に返し最後の結果を返し続ける(fake_runner: FakeRunner) -> None:
    fake_runner.script(["x"], stdout="1")
    fake_runner.script(["x"], stdout="2")
    assert [fake_runner(["x"]).text for _ in range(3)] == ["1", "2", "2"]


def test_偽の_Runner_は決めていないコマンドを断る(fake_runner: FakeRunner) -> None:
    with pytest.raises(AssertionError):
        fake_runner(["rm", "-rf", "/"])


def test_偽の_Runner_は_check_で失敗を例外にする(fake_runner: FakeRunner) -> None:
    fake_runner.script(["false"], returncode=1)
    assert fake_runner(["false"]).returncode == 1
    with pytest.raises(CommandFailed):
        fake_runner(["false"], check=True)


def test_偽の_Runner_は決めた例外を投げる(fake_runner: FakeRunner) -> None:
    fake_runner.raise_on(["ssh"], CommandTimedOut("ssh"))
    with pytest.raises(CommandTimedOut):
        fake_runner(["ssh", "rover"])


# --- systemctl ---

SYSTEMCTL = ("systemctl", "--user")


@pytest.mark.parametrize(
    ("state", "returncode", "expected"),
    [
        ("active", 0, True),
        ("activating", 3, True),
        ("deactivating", 3, True),
        ("reloading", 0, True),
        ("inactive", 3, False),
        ("failed", 3, False),
    ],
)
def test_動いているかを読む(
    fake_runner: FakeRunner, state: str, returncode: int, expected: bool
) -> None:
    fake_runner.script([*SYSTEMCTL, "is-active"], returncode=returncode, stdout=f"{state}\n")
    assert system.is_active(fake_runner, "osekkai.service") is expected
    assert fake_runner.argvs == [(*SYSTEMCTL, "is-active", "osekkai.service")]


def test_動いているかが分からなければ例外にする(fake_runner: FakeRunner) -> None:
    # ユーザーのバスにつながらないとき、標準出力は空になる
    fake_runner.script([*SYSTEMCTL, "is-active"], returncode=1, stderr=SECRET)
    with pytest.raises(UnexpectedOutput) as info:
        system.is_active(fake_runner, "osekkai.service")
    assert SECRET not in str(info.value)


def test_ユニットの状態を読む(fake_runner: FakeRunner) -> None:
    fake_runner.script(
        [*SYSTEMCTL, "show"], stdout="MainPID=1234\nNRestarts=2\nActiveState=active\n"
    )
    info = system.show_unit(fake_runner, "osekkai.service")
    assert info == UnitInfo(main_pid=1234, n_restarts=2, active_state="active")
    assert fake_runner.argvs == [
        (
            *SYSTEMCTL,
            "show",
            "--property=MainPID,NRestarts,ActiveState",
            "osekkai.service",
        )
    ]
    assert fake_runner.calls[0].check is True


def test_止まっているユニットの_MainPID_は_None(fake_runner: FakeRunner) -> None:
    fake_runner.script(
        [*SYSTEMCTL, "show"], stdout="ActiveState=inactive\nMainPID=0\nNRestarts=0\n"
    )
    info = system.show_unit(fake_runner, "osekkai.service")
    assert info == UnitInfo(main_pid=None, n_restarts=0, active_state="inactive")


@pytest.mark.parametrize(
    "stdout",
    ["", "MainPID=1\nNRestarts=0\n", "MainPID=x\nNRestarts=0\nActiveState=active\n"],
)
def test_ユニットの状態の形が違えば例外にする(fake_runner: FakeRunner, stdout: str) -> None:
    fake_runner.script([*SYSTEMCTL, "show"], stdout=stdout)
    with pytest.raises(UnexpectedOutput):
        system.show_unit(fake_runner, "osekkai.service")


@pytest.mark.parametrize(
    ("func", "verb"),
    [
        (system.start_unit, "start"),
        (system.stop_unit, "stop"),
        (system.enable_unit, "enable"),
        (system.disable_unit, "disable"),
    ],
)
def test_ユニットを操作する(fake_runner: FakeRunner, func: object, verb: str) -> None:
    fake_runner.script([*SYSTEMCTL, verb])
    func(fake_runner, "osekkai.service")  # type: ignore[operator]
    assert fake_runner.argvs == [(*SYSTEMCTL, verb, "osekkai.service")]
    call = fake_runner.calls[0]
    assert call.check is True
    assert call.timeout is not None


def test_ユニットの操作の失敗は例外にする(fake_runner: FakeRunner) -> None:
    fake_runner.script([*SYSTEMCTL, "start"], returncode=1, stderr=SECRET)
    with pytest.raises(CommandFailed) as info:
        system.start_unit(fake_runner, "osekkai.service")
    assert info.value.program == "systemctl"
    assert SECRET not in str(info.value)


def test_ユニットを読み直す(fake_runner: FakeRunner) -> None:
    fake_runner.script([*SYSTEMCTL, "daemon-reload"])
    system.daemon_reload(fake_runner)
    assert fake_runner.argvs == [(*SYSTEMCTL, "daemon-reload")]
    assert fake_runner.calls[0].check is True


# --- journalctl ---


def test_時刻以降の_journal_を読む(fake_runner: FakeRunner) -> None:
    fake_runner.script(["journalctl"], stdout="起動します\n版: v0.2.0\n")
    lines = system.journal_since(fake_runner, "osekkai.service", "2026-11-04 21:00:00")
    assert lines == ["起動します", "版: v0.2.0"]
    assert fake_runner.argvs == [
        (
            "journalctl",
            "--user",
            "_SYSTEMD_USER_UNIT=osekkai.service",
            "--since",
            "2026-11-04 21:00:00",
            "-o",
            "cat",
            "--no-pager",
            "-q",
        )
    ]
    assert fake_runner.calls[0].check is True


def test_journal_が空なら空の一覧(fake_runner: FakeRunner) -> None:
    fake_runner.script(["journalctl"], stdout="")
    assert system.journal_since(fake_runner, "osekkai.service", "@0") == []


# --- git ---


def test_git_describe_を読む(fake_runner: FakeRunner, tmp_path: Path) -> None:
    fake_runner.script(["git"], stdout="v0.2.0-3-gabc1234-dirty\n")
    assert system.git_describe(fake_runner, tmp_path) == "v0.2.0-3-gabc1234-dirty"
    assert fake_runner.argvs == [
        ("git", "-C", str(tmp_path), "describe", "--tags", "--always", "--dirty")
    ]
    assert fake_runner.calls[0].check is True


def test_git_describe_が空なら例外にする(fake_runner: FakeRunner, tmp_path: Path) -> None:
    fake_runner.script(["git"], stdout="\n")
    with pytest.raises(UnexpectedOutput):
        system.git_describe(fake_runner, tmp_path)


def test_手の変更の件数を数える(fake_runner: FakeRunner, tmp_path: Path) -> None:
    fake_runner.script(["git"], stdout=" M src/a.py\n?? notes.txt\n\n")
    assert system.git_dirty_count(fake_runner, tmp_path) == 2
    assert fake_runner.argvs == [("git", "-C", str(tmp_path), "status", "--porcelain")]


def test_手の変更がなければ_0(fake_runner: FakeRunner, tmp_path: Path) -> None:
    fake_runner.script(["git"], stdout="")
    assert system.git_dirty_count(fake_runner, tmp_path) == 0


def test_git_の失敗は例外にする(fake_runner: FakeRunner, tmp_path: Path) -> None:
    fake_runner.script(["git"], returncode=128, stderr=SECRET)
    with pytest.raises(CommandFailed) as info:
        system.git_dirty_count(fake_runner, tmp_path)
    assert SECRET not in str(info.value)
