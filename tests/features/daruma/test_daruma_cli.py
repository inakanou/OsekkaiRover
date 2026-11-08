"""遊びを始める道具と、判定を確かめる道具のテスト（6.3、8.1〜8.4、9.2）。

一時の XDG のディレクトリで、模擬のデバイス（端末の出力を StringIO にする）と、
待たない時計で動く台本の偽のカメラを差し込む。
"""

from __future__ import annotations

import argparse
import io
import logging
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from osekkai.adapters.fake.clock import FakeClock
from osekkai.adapters.fake.console import build_console_devices
from osekkai.core.clock import Clock
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.paths import Paths
from osekkai.core.ports import Devices
from osekkai.core.registry import FeatureRegistry
from osekkai.features.daruma import DarumaFeature, cli, rules, texts
from osekkai.features.daruma.camera import ScriptedCamera
from osekkai.features.daruma.config import DarumaConfig
from osekkai.features.presence.api import CameraLoan, CameraNotAvailable

JUDGE_LINE = re.compile(r"^\s*(\d+\.\d) 秒  動きの量 (\d\.\d{3})  (うごいた|とまってる)$")
# ログに出てはいけない動きの量（ほかの数と重ならない値）
HIDDEN_AMOUNTS = (0.0123, 0.0456)


def _write_config(text: str) -> None:
    paths = Paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")


class _NoCamera:
    """いつも貸せない偽のカメラ。"""

    def __init__(self) -> None:
        self.asked = 0

    def borrow(self, owner: str, alive: Callable[[], bool]) -> CameraLoan:
        self.asked += 1
        raise CameraNotAvailable()


class Rig:
    """道具に差し込む、模擬のデバイスと台本の偽のカメラ。"""

    def __init__(self) -> None:
        self.console = io.StringIO()
        self.built = 0
        self.fake_flags: list[bool] = []
        self.values: tuple[float, ...] = (0.0,)
        self.no_camera: _NoCamera | None = None

    def build(self, args: argparse.Namespace, paths: Paths, registry: FeatureRegistry) -> Devices:
        self.built += 1
        self.fake_flags.append(bool(args.fake_hardware))
        return build_console_devices(time, out=self.console, inp=io.StringIO(""))

    def camera(self, clock: Clock) -> ScriptedCamera | _NoCamera:
        # 道具の時計は実際の時計なので、待たない時計の偽のカメラに替える
        if self.no_camera is not None:
            return self.no_camera
        return ScriptedCamera(FakeClock(), self.values)


@pytest.fixture
def rig(xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Rig:
    _write_config("daruma:\n  enabled: true\n  watch_seconds: 1.0\n  nobody_rounds: 1\n")
    rig = Rig()
    monkeypatch.setattr(cli, "build_devices", rig.build)
    monkeypatch.setattr(cli, "make_fake_camera", rig.camera)
    monkeypatch.setattr(cli, "PRESENCE_WAIT_S", 2.0)
    # 遊びの待ちを短くする（流れは texts と rules の値を呼ぶたびに読む）
    monkeypatch.setattr(texts, "INTRO_WAIT_S", 0.0)
    monkeypatch.setattr(texts, "MOVED_WAIT_S", 0.0)
    monkeypatch.setattr(rules, "PAUSE_MAX_S", 0.0)
    return rig


@pytest.fixture
def held_lock(xdg_dirs: dict[str, Path]) -> Iterator[InstanceLock]:
    lock = InstanceLock(Paths().lock_file)
    assert lock.acquire()
    yield lock
    lock.release()


def _judge_lines(out: str) -> list[tuple[str, str, str]]:
    found = [JUDGE_LINE.match(line) for line in out.splitlines()]
    return [(m.group(1), m.group(2), m.group(3)) for m in found if m]


def _log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def _spoke(console: str, speech: str) -> bool:
    """模擬の声が、その言葉の初めの一文を話したか（声は文ごとに合成される）。"""
    first = re.split(r"(?<=[。！？])", speech)[0]
    return f"[声] {first}" in console


def _assert_no_amounts(text: str) -> None:
    for amount in HIDDEN_AMOUNTS:
        for shown in (str(amount), f"{amount:.3f}", f"{amount:.2f}"):
            assert shown not in text


# --- 遊びを始める道具（8.1、8.4） ---


def test_偽のハードで遊びを_1_回終えて_0_で戻る(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["--play", "--fake-hardware"])

    assert code == 0  # 8.1
    out, _ = capsys.readouterr()
    assert "nobody" in out  # 動かない台本なので、だれもいないで終わる
    assert rig.fake_flags == [True]
    console = rig.console.getvalue()
    assert _spoke(console, texts.SAY_INTRO)  # 話しかけたときと同じ遊び
    assert "[首]" in console


def test_偽のカメラの台本が動いたなら_動いたの流れになる(rig: Rig) -> None:
    # 押下が来なくても、台本の量がしきい値を超えれば 動いた になる（8.4）
    rig.values = (0.5, 0.0)
    _write_config("daruma:\n  watch_seconds: 1.0\n  nobody_rounds: 1\n  max_rounds: 3\n")
    assert cli.main(["--play", "--fake-hardware"]) == 0
    assert _spoke(rig.console.getvalue(), texts.SAY_MOVED)


def test_遊びの道具は言葉を登録しない(rig: Rig) -> None:
    from osekkai.voice import api as voice_api

    voice_api._reset_for_tests()
    assert cli.main(["--play", "--fake-hardware"]) == 0
    assert voice_api.registry().handler_for(DarumaFeature.spec.name) is None


def test_カメラがなければ遊べずに_1_で終える(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    rig.no_camera = _NoCamera()
    assert cli.main(["--play", "--fake-hardware"]) == 1
    out, _ = capsys.readouterr()
    assert "no_camera" in out
    assert rig.no_camera.asked == 1
    assert _spoke(rig.console.getvalue(), texts.SAY_NO_CAMERA)  # 1.4


def test_実機の形でも見つけてあいさつにカメラがなければ_1_で終える(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    # PC の見つけてあいさつはカメラを持たない（エントリーポイントから選んだ本物で動く）
    _write_config("daruma:\n  watch_seconds: 1.0\npresence:\n  enabled: true\n")
    assert cli.main(["--play"]) == 1
    out, _ = capsys.readouterr()
    assert "no_camera" in out
    assert rig.fake_flags == [False]


# --- 判定を確かめる道具（8.2、8.4） ---


@pytest.mark.parametrize(
    ("threshold", "labels"),
    [
        ("0.01", ["とまってる", "うごいた", "とまってる", "うごいた"]),
        ("0.003", ["うごいた", "うごいた", "うごいた", "うごいた"]),
        ("0.05", ["とまってる", "とまってる", "とまってる", "とまってる"]),
    ],
)
def test_判定の道具は見る長さごとの量と_しきい値での判定を出す(
    rig: Rig, capsys: pytest.CaptureFixture[str], threshold: str, labels: list[str]
) -> None:
    rig.values = (0.004, 0.03)
    _write_config(f"daruma:\n  watch_seconds: 1.0\n  threshold: {threshold}\n")

    code = cli.main(["--judge-test", "--seconds", "4", "--fake-hardware"])

    assert code == 0
    lines = _judge_lines(capsys.readouterr().out)
    assert [label for _, _, label in lines] == labels  # 8.2
    assert [amount for _, amount, _ in lines] == ["0.004", "0.030", "0.004", "0.030"]
    assert [at for at, _, _ in lines] == ["1.0", "2.0", "3.0", "4.0"]


def test_判定の道具は見る長さの設定で区切る(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config("daruma:\n  watch_seconds: 2.5\n")
    assert cli.main(["--judge-test", "--seconds", "6", "--fake-hardware"]) == 0
    lines = _judge_lines(capsys.readouterr().out)
    assert [at for at, _, _ in lines] == ["2.5", "5.0"]


def test_判定の道具は首を正面に向ける(rig: Rig) -> None:
    assert cli.main(["--judge-test", "--seconds", "1", "--fake-hardware"]) == 0
    pan, tilt = texts.POSE_FACING
    assert f"[首] pan={pan:.1f} tilt={tilt:.1f}" in rig.console.getvalue()


def test_判定の道具はカメラがなければ_1_で終える(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.no_camera = _NoCamera()
    assert cli.main(["--judge-test", "--seconds", "2", "--fake-hardware"]) == 1
    out, err = capsys.readouterr()
    assert not _judge_lines(out)
    assert cli.JUDGE_MESSAGES[cli.JudgeResult.CAMERA] in err


def test_動きの量は画面にだけ出し_ログには出さない(
    rig: Rig, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig.values = HIDDEN_AMOUNTS
    # 動いたの回を 1 回で終える（テストを速くする）
    _write_config("daruma:\n  enabled: true\n  watch_seconds: 1.0\n  max_rounds: 1\n")

    assert cli.main(["--judge-test", "--seconds", "2", "--fake-hardware"]) == 0
    assert cli.main(["--play", "--fake-hardware"]) == 0

    out, err = capsys.readouterr()
    assert "0.012" in out and "0.046" in out  # 画面には出す（8.2）
    _assert_no_amounts(_log_text(caplog))  # 6.3
    _assert_no_amounts(err)


# --- ロック・設定・指定 ---


@pytest.mark.parametrize("argv", [["--play"], ["--judge-test", "--seconds", "1"]])
@pytest.mark.parametrize("fake", [True, False])
def test_ロボットが動いていれば_75_で終え_止めるよう示す(
    rig: Rig,
    held_lock: InstanceLock,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    fake: bool,
) -> None:
    assert cli.main([*argv, *(["--fake-hardware"] if fake else [])]) == 75  # 8.3
    _, err = capsys.readouterr()
    assert cli.LOCK_BUSY_MESSAGE in err
    assert rig.built == 0  # デバイスに触らない


@pytest.mark.parametrize("argv", [["--play"], ["--judge-test", "--seconds", "1"]])
def test_無効になっていれば始めずに_1_で終える(rig: Rig, argv: list[str]) -> None:
    _write_config("daruma:\n  enabled: false\n")
    assert cli.main([*argv, "--fake-hardware"]) == 1
    assert rig.built == 0


def test_設定の誤りは場所と理由だけを出して_1_で終える(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config("daruma:\n  threshold: 1.987654\n")
    assert cli.main(["--play", "--fake-hardware"]) == 1
    _, err = capsys.readouterr()
    assert "threshold" in err
    assert "1.987654" not in err
    assert rig.built == 0


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--play", "--judge-test"],
        ["--play", "--seconds", "3"],
        ["--judge-test", "--seconds", "0"],
        ["--judge-test", "--seconds", "601"],
        ["--judge-test", "--seconds", "1.5"],
        ["--judge-test", "--seconds", "abc"],
    ],
)
def test_指定の誤りは_2_で終える(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def test_判定の道具の秒数の既定は_30_秒() -> None:
    assert cli.parse_args(["--judge-test"]).seconds is None
    assert cli.JUDGE_DEFAULT_SECONDS == 30
    assert cli.parse_args(["--judge-test", "--seconds", "600"]).seconds == 600


def test_道具の機能の申告はだるまさんと同じで_self_check_の項目はない() -> None:
    for feature in (
        cli.make_play_feature(cli.PlayOutcome(), cli.make_fake_camera_borrow),
        cli.make_judge_feature(3, cli.JudgeOutcome(), cli.make_fake_camera_borrow, print),
    ):
        spec = feature.spec
        assert spec.name == DarumaFeature.spec.name
        assert spec.priority == DarumaFeature.spec.priority
        assert spec.resources == DarumaFeature.spec.resources
        assert spec.config_model is DarumaConfig
        assert spec.checks == ()


def test_main_module_は道具の入口を呼ぶ() -> None:
    from osekkai.features.daruma import __main__ as entry

    assert entry.main is cli.main
