"""追従の確認と動きの量の確認のコマンドのテスト（9.1〜9.3、6.3）。

一時の XDG のディレクトリで、模擬のデバイス（端末の出力を StringIO にする）と、
偽のカメラと偽の映像の処理を差し込む。確認の 1 秒の長さを短くして動かす。
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from presence_fakes import FakeCamera, FakeVision, embedding

from osekkai.adapters.fake.clock import FakeClock
from osekkai.adapters.fake.console import build_console_devices
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.paths import Paths
from osekkai.core.ports import Devices
from osekkai.core.registry import FeatureRegistry
from osekkai.core.storage import StateStore
from osekkai.features.presence import cli
from osekkai.features.presence.config import PresenceConfig
from osekkai.features.presence.faces import FaceBook, Person
from osekkai.features.presence.ports import CameraPort, VisionPort
from osekkai.features.presence.tracking import FaceBox

HIDDEN_ID = "zzkidid"
HIDDEN_NAME = "ひみつのよびな"
RIGHT = FaceBox(0.5, 0.0, 0.4)  # 右に寄った顔（首が右を向く）
SECOND = 0.05  # テストの確認の 1 秒の実際の長さ

WATCH_LINE = re.compile(r"^\s*\d+\.\d 秒  顔 (x=[+-]\d\.\d\d y=[+-]\d\.\d\d 大きさ=\d\.\d\d|なし)")
MOTION_LINE = re.compile(r"^\s*\d+ 秒  動きの量 (\d\.\d{3})$")


def _store() -> StateStore:
    return StateStore(Paths().state_dir_for("presence"), FakeClock())


def _faces_file() -> Path:
    return Paths().state_dir_for("presence") / "faces.json"


def _write_config(text: str = "presence:\n  enabled: true\n") -> None:
    paths = Paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")


def _seed_person() -> None:
    FaceBook(_store(), [Person(HIDDEN_ID, HIDDEN_NAME, (embedding(100),))]).save()


class Rig:
    """確認のときに差し込む、模擬のデバイスとカメラと映像の処理。"""

    def __init__(self) -> None:
        self.console = io.StringIO()
        self.camera = FakeCamera()
        self.vision = FakeVision()
        self.built = 0
        self.no_camera = False

    def build(self, args: argparse.Namespace, paths: Paths, registry: FeatureRegistry) -> Devices:
        assert args.fake_hardware
        self.built += 1
        return build_console_devices(time, out=self.console, inp=io.StringIO(""))

    def choose(self, config: PresenceConfig) -> tuple[CameraPort | None, VisionPort | None]:
        if self.no_camera:
            return None, None
        return self.camera, self.vision


@pytest.fixture
def rig(xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Rig:
    _write_config()
    rig = Rig()
    monkeypatch.setattr(cli, "build_devices", rig.build)
    monkeypatch.setattr(cli, "choose_devices", rig.choose)
    monkeypatch.setattr(cli, "CHECK_SECOND_S", SECOND)
    return rig


@pytest.fixture
def held_lock(xdg_dirs: dict[str, Path]) -> Iterator[InstanceLock]:
    lock = InstanceLock(Paths().lock_file)
    assert lock.acquire()
    yield lock
    lock.release()


def _assert_no_hidden_values(text: str) -> None:
    for hidden in (HIDDEN_NAME, HIDDEN_ID):
        assert hidden not in text


def _watch_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if WATCH_LINE.match(line)]


# --- 追従の確認（9.1） ---


def test_追従の確認は顔の位置と首と目と認識の結果を出し続け_名前と呼び名を出さない(
    rig: Rig, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    _seed_person()
    rig.vision = FakeVision(
        faces={i: (RIGHT,) for i in range(100000)},
        embeddings={i: embedding(100) for i in range(100000)},  # 覚えた人の顔
    )
    caplog.set_level(logging.DEBUG)

    code = cli.main(["--watch-test", "--seconds", "2", "--fake-hardware"])

    assert code == 0
    out, err = capsys.readouterr()
    lines = _watch_lines(out)
    # 0.2 秒ごとに 1 行、2 秒の間（遅れの分のゆとりを見る）
    assert 3 <= len(lines) <= 11
    last = lines[-1]
    assert "x=+0.50 y=+0.00 大きさ=0.40" in last
    pan = float(re.search(r"首 左右=([+-]\d+\.\d)", last).group(1))  # type: ignore[union-attr]
    assert pan > 0.0  # 右の顔の方へ首を向けた
    assert re.search(r"上下=[+-]\d+\.\d", last)
    gaze = float(re.search(r"目=([+-]\d\.\d\d)", last).group(1))  # type: ignore[union-attr]
    assert gaze > 0.0
    assert last.endswith(f"認識={cli.LABEL_KNOWN}")
    assert "[首] pan=" in rig.console.getvalue()  # 首を動かした
    assert rig.camera.stop_count >= 1
    _assert_no_hidden_values(out + err + rig.console.getvalue())
    _assert_no_hidden_values("\n".join(r.getMessage() for r in caplog.records))


def test_追従の確認で知らない人と顔なしを分けて出す(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_person()
    # 初めの 6 枚は知らない人の顔、その後は顔なし
    rig.vision = FakeVision(
        faces={i: (RIGHT,) for i in range(6)},
        embeddings={i: embedding(999) for i in range(6)},
    )

    code = cli.main(["--watch-test", "--seconds", "2", "--fake-hardware"])

    assert code == 0
    lines = _watch_lines(capsys.readouterr().out)
    assert any(line.endswith(f"認識={cli.LABEL_UNKNOWN}") for line in lines)
    assert not any(line.endswith(f"認識={cli.LABEL_KNOWN}") for line in lines)
    assert "  顔 なし  " in lines[-1]
    assert lines[-1].endswith(f"認識={cli.LABEL_NONE}")


def test_追従の確認は覚えた顔を読めなくても上書きせず_みんな知らない人として出す(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _faces_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 99, "people": {}}), encoding="utf-8")
    before = path.read_bytes()
    rig.vision = FakeVision(
        faces={i: (RIGHT,) for i in range(100000)},
        embeddings={i: embedding(100) for i in range(100000)},
    )

    code = cli.main(["--watch-test", "--seconds", "1", "--fake-hardware"])

    assert code == 0
    out, _ = capsys.readouterr()
    assert cli.UNREADABLE_WATCH_MESSAGE in out
    lines = _watch_lines(out)
    assert lines
    assert all(not line.endswith(f"認識={cli.LABEL_KNOWN}") for line in lines)
    assert path.read_bytes() == before


def test_追従の確認は決めた秒数で終わる(rig: Rig) -> None:
    rig.vision = FakeVision(faces={i: (RIGHT,) for i in range(100000)})
    started = time.monotonic()
    assert cli.main(["--watch-test", "--seconds", "4", "--fake-hardware"]) == 0
    elapsed = time.monotonic() - started
    assert 4 * SECOND <= elapsed < 4 * SECOND + 5.0


# --- 動きの量の確認（9.2） ---


@pytest.mark.parametrize(("seconds", "count"), [("4", 2), ("5", 3), ("1", 1)])
def test_動きの量の確認は_2_秒ごとに動きの量を出す(
    rig: Rig, capsys: pytest.CaptureFixture[str], seconds: str, count: int
) -> None:
    rig.vision = FakeVision(motions=(0.25,))

    code = cli.main(["--motion-test", "--seconds", seconds, "--fake-hardware"])

    assert code == 0
    out, _ = capsys.readouterr()
    values = [m.group(1) for m in map(MOTION_LINE.match, out.splitlines()) if m]
    assert values == ["0.250"] * count
    assert rig.vision.motion_calls  # 撮って比べた
    assert rig.camera.stop_count >= 1


def test_動きの量の確認の時刻は_2_秒ずつ進む(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    rig.vision = FakeVision(motions=(0.0,))
    assert cli.main(["--motion-test", "--seconds", "5", "--fake-hardware"]) == 0
    times = [
        int(line.split()[0]) for line in capsys.readouterr().out.splitlines() if "動きの量" in line
    ]
    assert times == [2, 4, 5]


def test_動きの量は_1_回だけの大きな値を数えない(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.vision = FakeVision(motions=(0.0, 0.9, 0.0, 0.3, 0.3))
    assert cli.main(["--motion-test", "--seconds", "2", "--fake-hardware"]) == 0
    out = capsys.readouterr().out
    assert [m.group(1) for m in map(MOTION_LINE.match, out.splitlines()) if m] == ["0.300"]


# --- 共通 ---


@pytest.mark.parametrize("flag", ["--watch-test", "--motion-test"])
def test_ロボットが動いていれば_75_で終え_止めるよう示す(
    rig: Rig, held_lock: InstanceLock, flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([flag, "--seconds", "1", "--fake-hardware"]) == 75  # 9.3
    _, err = capsys.readouterr()
    assert cli.LOCK_BUSY_MESSAGE in err
    assert rig.built == 0  # デバイスに触らない
    assert rig.camera.start_count == 0


@pytest.mark.parametrize("flag", ["--watch-test", "--motion-test"])
def test_カメラがなければ_1_で終える(
    rig: Rig, flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.no_camera = True
    assert cli.main([flag, "--seconds", "1", "--fake-hardware"]) == 1
    assert cli.NO_CAMERA_CHECK_MESSAGE in capsys.readouterr().err
    assert rig.built == 0


@pytest.mark.parametrize("flag", ["--watch-test", "--motion-test"])
def test_撮れなければ_1_で終える(rig: Rig, flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    rig.camera = FakeCamera(unavailable=True)
    assert cli.main([flag, "--seconds", "1", "--fake-hardware"]) == 1
    assert capsys.readouterr().err.strip()


@pytest.mark.parametrize("flag", ["--watch-test", "--motion-test"])
def test_無効になっていれば始めずに_1_で終える(rig: Rig, flag: str) -> None:
    _write_config("presence:\n  enabled: false\n")
    assert cli.main([flag, "--fake-hardware"]) == 1
    assert rig.built == 0


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5", "3601"])
@pytest.mark.parametrize("flag", ["--watch-test", "--motion-test"])
def test_秒数が誤りなら_2_で終える(flag: str, value: str) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([flag, "--seconds", value])
    assert exc.value.code == 2


def test_秒数は確認のときだけ使える() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--forget-all", "--seconds", "3"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["--watch-test", "--motion-test"])
    assert exc.value.code == 2


def test_秒数の既定は追従が_30_秒_動きの量が_20_秒() -> None:
    assert cli.parse_args(["--watch-test"]).seconds is None
    assert cli.WATCH_DEFAULT_SECONDS == 30
    assert cli.MOTION_DEFAULT_SECONDS == 20


def test_確認の機能の申告は見つけてあいさつと同じで_self_check_の項目はない() -> None:
    from osekkai.features.presence.feature import PresenceFeature

    for feature in (
        cli.make_watch_feature(
            3, cli.CheckOutcome(), camera=FakeCamera(), vision=FakeVision(), emit=print
        ),
        cli.make_motion_feature(
            3, cli.CheckOutcome(), camera=FakeCamera(), vision=FakeVision(), emit=print
        ),
    ):
        assert feature.spec.name == PresenceFeature.spec.name
        assert feature.spec.priority == PresenceFeature.spec.priority
        assert feature.spec.resources == PresenceFeature.spec.resources
        assert feature.spec.checks == ()
