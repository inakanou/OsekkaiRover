"""センサーを確かめる道具のテスト（14.1〜14.3）。

一時の XDG のディレクトリで動かす。時計は待たない偽の時計に、センサーは台本のセンサーに
替える。ロボットが動いているときは、センサーを開かないことを確かめる。
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from remote_fakes import FakeClock, ScriptedSensors

from osekkai.core.clock import Clock
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.paths import Paths
from osekkai.features.remote import cli
from osekkai.features.remote.adapters.fake import ClearSensors
from osekkai.features.remote.ports import SensorPort, SensorReading

LINE = re.compile(r"^距離 (.+?)  床 (\S+ \S+ \S+)  (\S+)  (.+)$")
# ログに出てはいけない距離（ほかの数と重ならない値）
HIDDEN_DISTANCE = 47.3


def _write_config(text: str) -> None:
    paths = Paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")


class Rig:
    """道具に差し込む、偽の時計と台本のセンサー。"""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.sensors = ScriptedSensors()
        self.opened: list[str] = []
        self.hardware = False

    def make_clock(self) -> Clock:
        return self.clock

    def fake(self) -> SensorPort:
        self.opened.append("fake")
        return self.sensors

    def hat(self, clock: Clock) -> SensorPort:
        self.opened.append("hat")
        return self.sensors

    def available(self) -> bool:
        return self.hardware


@pytest.fixture
def rig(xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Rig:
    _write_config("remote:\n  stop_cm: 20\n  cliff_reference: 500\n")
    rig = Rig()
    monkeypatch.setattr(cli, "make_clock", rig.make_clock)
    monkeypatch.setattr(cli, "make_fake_sensors", rig.fake)
    monkeypatch.setattr(cli, "make_hat_sensors", rig.hat)
    monkeypatch.setattr(cli, "sensors_available", rig.available)
    return rig


@pytest.fixture
def held_lock(xdg_dirs: dict[str, Path]) -> Iterator[InstanceLock]:
    lock = InstanceLock(Paths().lock_file)
    assert lock.acquire()
    yield lock
    lock.release()


def _lines(out: str) -> list[tuple[str, str, str, str]]:
    found = [LINE.match(line) for line in out.splitlines()]
    return [(m.group(1), m.group(2), m.group(3), m.group(4)) for m in found if m]


def _reading(
    distance: float | None, floor: tuple[int | None, int | None, int | None], far: bool = False
) -> SensorReading:
    return SensorReading(distance_cm=distance, floor=floor, far=far)


# --- 0.5 秒ごとの 1 行（14.1） ---


def test_決めた秒数の間_0_5_秒ごとに_1_行を出して_0_で終える(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    start = rig.clock.monotonic()
    assert cli.main(["--sensor-test", "--seconds", "3", "--fake-hardware"]) == 0
    out, _ = capsys.readouterr()
    assert len(_lines(out)) == 6
    assert rig.clock.monotonic() - start == pytest.approx(3.0)
    assert out.strip().splitlines()[-1] == cli.DONE_MESSAGE
    assert rig.sensors.closed


def test_秒数の既定は_30_秒() -> None:
    assert cli.parse_args(["--sensor-test"]).seconds == 30
    assert cli.parse_args(["--sensor-test", "--seconds", "600"]).seconds == 600
    assert cli.parse_args(["--sensor-test", "--seconds", "1"]).seconds == 1


def test_距離と床の値と段差と判定を出す(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    rig.sensors = ScriptedSensors(
        [
            _reading(23.4, (812, 640, 555)),
            _reading(12.0, (812, 640, 700)),
            _reading(50.0, (812, 300, 700)),
            _reading(None, (812, 640, 700)),
            _reading(math.inf, (812, 640, 700), far=True),
            _reading(50.0, (812, None, 700)),
        ]
    )
    assert cli.main(["--sensor-test", "--seconds", "3", "--fake-hardware"]) == 0
    out, _ = capsys.readouterr()
    assert _lines(out) == [
        ("23.4 cm", "812 640 555", "段差なし", "ふつう"),
        ("12.0 cm", "812 640 700", "段差なし", "まえに なにか"),
        ("50.0 cm", "812 300 700", "段差あり", "だんさ"),
        ("読めない", "812 640 700", "段差なし", "読めない(危険)"),
        ("遠い", "812 640 700", "段差なし", "ふつう"),
        ("50.0 cm", "812 読めない 700", "読めない", "読めない(危険)"),
    ]
    assert "距離 23.4 cm  床 812 640 555  段差なし  ふつう" in out.splitlines()


def test_段差の判定が無効なら判定しないと出し_床の値で危険にしない(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config("remote:\n  cliff_stop: false\n")
    rig.sensors = ScriptedSensors([_reading(50.0, (0, None, 0))])
    assert cli.main(["--sensor-test", "--seconds", "1", "--fake-hardware"]) == 0
    out, _ = capsys.readouterr()
    assert _lines(out) == [("50.0 cm", "0 読めない 0", "判定しない", "ふつう")] * 2


def test_止まる距離は設定の値で判定する(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config("remote:\n  stop_cm: 40\n")
    rig.sensors = ScriptedSensors([_reading(30.0, (900, 900, 900))])
    assert cli.main(["--sensor-test", "--seconds", "1", "--fake-hardware"]) == 0
    out, _ = capsys.readouterr()
    assert {line[3] for line in _lines(out)} == {"まえに なにか"}


def test_読みで例外が出たら読めないと出して続ける(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.sensors = ScriptedSensors([OSError("x"), _reading(60.0, (900, 900, 900))])
    assert cli.main(["--sensor-test", "--seconds", "1", "--fake-hardware"]) == 0
    out, _ = capsys.readouterr()
    lines = _lines(out)
    assert lines[0] == ("読めない", "読めない 読めない 読めない", "読めない", "読めない(危険)")
    assert lines[1][3] == "ふつう"


def test_偽のハードでは_ClearSensors_を使う(
    xdg_dirs: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert isinstance(cli.make_fake_sensors(), ClearSensors)
    _write_config("remote:\n  enabled: false\n")
    assert cli.main(["--sensor-test", "--seconds", "1", "--fake-hardware"]) == 0
    out, _ = capsys.readouterr()
    assert _lines(out) == [("100.0 cm", "1000 1000 1000", "段差なし", "ふつう")] * 2


def test_ラジコンが無効でも確かめられる(rig: Rig) -> None:
    _write_config("remote:\n  enabled: false\n")
    assert cli.main(["--sensor-test", "--seconds", "1", "--fake-hardware"]) == 0
    assert rig.sensors.reads == 2


def test_実機の形では_HAT_のセンサーを開く(rig: Rig) -> None:
    rig.hardware = True
    assert cli.main(["--sensor-test", "--seconds", "1"]) == 0
    assert rig.opened == ["hat"]
    assert rig.sensors.closed


def test_センサーのライブラリがなければ_1_で終える(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.hardware = False
    assert cli.main(["--sensor-test", "--seconds", "1"]) == 1
    _, err = capsys.readouterr()
    assert cli.NO_SENSORS_MESSAGE in err
    assert rig.opened == []


class _InterruptingClock(FakeClock):
    """2 回目の待ちで Ctrl+C が押される時計。"""

    def __init__(self) -> None:
        super().__init__()
        self.waits = 0

    def sleep(self, seconds: float) -> None:
        self.waits += 1
        if self.waits == 2:
            raise KeyboardInterrupt
        super().sleep(seconds)


def test_Ctrl_C_で途中で止めて_0_で終え_センサーを閉じる(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.clock = _InterruptingClock()
    assert cli.main(["--sensor-test", "--seconds", "10", "--fake-hardware"]) == 0
    out, _ = capsys.readouterr()
    assert len(_lines(out)) == 2
    assert cli.STOPPED_MESSAGE in out
    assert cli.DONE_MESSAGE not in out
    assert rig.sensors.closed
    # ロックを放している
    lock = InstanceLock(Paths().lock_file)
    assert lock.acquire()
    lock.release()


def test_終えたらロックを放す(rig: Rig) -> None:
    assert cli.main(["--sensor-test", "--seconds", "1", "--fake-hardware"]) == 0
    lock = InstanceLock(Paths().lock_file)
    assert lock.acquire()
    lock.release()


def test_距離と設定の値はログに出さない(
    rig: Rig, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config("remote:\n  stop_cm: 33.7\n  cliff_reference: 777\n")
    rig.sensors = ScriptedSensors([OSError("x"), _reading(HIDDEN_DISTANCE, (901, 902, 903))])
    with caplog.at_level(logging.DEBUG, logger="osekkai"):
        assert cli.main(["--sensor-test", "--seconds", "1", "--fake-hardware"]) == 0
    out, _ = capsys.readouterr()
    assert str(HIDDEN_DISTANCE) in out  # 画面には出す
    logs = "\n".join(record.getMessage() for record in caplog.records)
    for hidden in (str(HIDDEN_DISTANCE), "901", "902", "903", "33.7", "777"):
        assert hidden not in logs


# --- ロボットが動いているとき（14.2）・タイヤ（14.3） ---


@pytest.mark.parametrize("fake", [True, False])
def test_ロボットが動いていれば_75_で終え_センサーに触らない(
    rig: Rig, held_lock: InstanceLock, capsys: pytest.CaptureFixture[str], fake: bool
) -> None:
    rig.hardware = True
    argv = ["--sensor-test", "--seconds", "1", *(["--fake-hardware"] if fake else [])]
    assert cli.main(argv) == 75
    _, err = capsys.readouterr()
    assert "ロボットが動いています。止めてから実行してください" in err
    assert rig.opened == []
    assert rig.sensors.reads == 0


def test_道具はモーターとサーボと基盤を読み込まない() -> None:
    source = Path(cli.__file__).read_text(encoding="utf-8")
    for name in ("Runtime", "build_devices", "Resource", "Lease", "osekkai.adapters"):
        assert name not in source, name


# --- 設定と指定の誤り ---


def test_設定の誤りは場所と理由だけを出して_1_で終える(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config("remote:\n  stop_cm: 987.654\n")
    assert cli.main(["--sensor-test", "--seconds", "1", "--fake-hardware"]) == 1
    _, err = capsys.readouterr()
    assert "stop_cm" in err
    assert "987.654" not in err
    assert rig.opened == []


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--seconds", "3"],
        ["--sensor-test", "--seconds", "0"],
        ["--sensor-test", "--seconds", "601"],
        ["--sensor-test", "--seconds", "1.5"],
        ["--sensor-test", "--seconds", "abc"],
        ["--sensor-test", "--unknown"],
    ],
)
def test_指定の誤りは_2_で終える(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def test_main_module_は道具の入口を呼ぶ() -> None:
    from osekkai.features.remote import __main__ as entry

    assert entry.main is cli.main
