"""顔を覚える・消す・一覧のコマンドのテスト（5.1〜5.9、6.3、6.5）。

一時の XDG のディレクトリで動かす。覚えるときは模擬のデバイス（端末の出力を StringIO にする）と、
偽のカメラと偽の映像の処理を差し込み、集める間を短くする。
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
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
from osekkai.features.presence import cli, texts
from osekkai.features.presence.config import PresenceConfig
from osekkai.features.presence.faces import FaceBook, Person
from osekkai.features.presence.greeting import UNKNOWN, GreetingRecords
from osekkai.features.presence.ports import CameraPort, VisionPort
from osekkai.features.presence.tracking import FaceBox

HIDDEN_ID = "zzkidid"
OTHER_ID = "zzotherid"
HIDDEN_NAME = "ひみつのよびな"
OTHER_NAME = "べつのよびな"
SMALL = FaceBox(-0.5, 0.0, 0.1)
BIG = FaceBox(0.2, 0.0, 0.4)
WHEN = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)


def _store() -> StateStore:
    return StateStore(Paths().state_dir_for("presence"), FakeClock())


def _faces_file() -> Path:
    return Paths().state_dir_for("presence") / "faces.json"


def _write_config(text: str = "presence:\n  enabled: true\n") -> None:
    paths = Paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")


def _seed_people() -> None:
    book = FaceBook(
        _store(),
        [
            Person(HIDDEN_ID, HIDDEN_NAME, (embedding(100), embedding(101))),
            Person(OTHER_ID, OTHER_NAME, (embedding(200), embedding(201), embedding(202))),
        ],
    )
    book.save()
    records = GreetingRecords(
        _store(),
        last_seen={HIDDEN_ID: WHEN, OTHER_ID: WHEN, UNKNOWN: WHEN},
        morning={HIDDEN_ID: date(2026, 11, 7), OTHER_ID: date(2026, 11, 7)},
    )
    records.save()


def _greetings() -> dict[str, object]:
    path = Paths().state_dir_for("presence") / "greetings.json"
    data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return data


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """集める間を短くする（数で決まる流れはそのまま）。"""
    monkeypatch.setattr(cli, "LEARN_INTERVAL_S", 0.005)
    monkeypatch.setattr(cli, "LEARN_DURATION_S", 5.0)


class Rig:
    """覚えるときに差し込む、模擬のデバイスとカメラと映像の処理。"""

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

    def said(self) -> str:
        """話した文章をつなげたもの（声は文ごとに分けて出る）。"""
        prefix = "[声] "
        lines = self.console.getvalue().splitlines()
        return "".join(line[len(prefix) :] for line in lines if line.startswith(prefix))


@pytest.fixture
def rig(xdg_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, quick: None) -> Rig:
    _write_config()
    rig = Rig()
    monkeypatch.setattr(cli, "build_devices", rig.build)
    monkeypatch.setattr(cli, "choose_devices", rig.choose)
    return rig


def _script(rig: Rig, frames: range) -> None:
    """そのフレームでは顔が 2 つ見え、大きい方の顔に特徴がある台本にする。"""
    rig.vision = FakeVision(
        faces={i: (SMALL, BIG) for i in frames},
        embeddings={i: embedding(i) for i in frames},
    )


def _assert_no_hidden_values(text: str) -> None:
    for hidden in (HIDDEN_NAME, OTHER_NAME, HIDDEN_ID, OTHER_ID):
        assert hidden not in text


@pytest.fixture
def held_lock(xdg_dirs: dict[str, Path]) -> Iterator[InstanceLock]:
    lock = InstanceLock(Paths().lock_file)
    assert lock.acquire()
    yield lock
    lock.release()


# --- 覚える ---


def test_覚えると_40_個を集めて保存し_半分で_1_回だけ横を向くよう話す(
    rig: Rig, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    _script(rig, range(1000))
    caplog.set_level(logging.DEBUG)

    code = cli.main(["--learn-face", HIDDEN_ID, "--call-name", HIDDEN_NAME, "--fake-hardware"])

    assert code == 0
    said = rig.said()
    # 基盤の起動の知らせが先に出ることがあるので、覚える言葉の順だけを見る
    start = said.index(texts.fill(texts.LEARN_START, HIDDEN_NAME))  # 5.1
    assert said.count(texts.LEARN_TURN) == 1  # 5.3
    assert start < said.index(texts.LEARN_TURN)
    assert said.endswith(texts.fill(texts.LEARN_DONE, HIDDEN_NAME))  # 5.4
    assert texts.LEARN_FAILED not in said
    shown = rig.console.getvalue()
    assert "ｵﾎﾞｴﾃﾙﾖ 1/40" in shown and "ｵﾎﾞｴﾃﾙﾖ 40/40" in shown  # 5.2
    assert texts.LCD_LEARNED in shown
    # 半分（20 個）で横を向くよう話した
    lines = shown.splitlines()
    turn = lines.index(f"[声] {texts.LEARN_TURN}")
    assert any("ｵﾎﾞｴﾃﾙﾖ 20/40" in line for line in lines[:turn])
    assert not any("ｵﾎﾞｴﾃﾙﾖ 21/40" in line for line in lines[:turn])

    book = FaceBook.load(_store())
    (person,) = book.people()
    assert person.person_id == HIDDEN_ID
    assert person.call_name == HIDDEN_NAME
    assert len(person.samples) == texts.LEARN_SAMPLES
    # 大きい方の顔の特徴を集めた
    assert all(face == BIG for _, face in rig.vision.embed_calls)
    assert rig.camera.stop_count >= 1

    out, err = capsys.readouterr()
    assert out.strip()  # 覚えたことを 1 行出す
    _assert_no_hidden_values(out + err)
    _assert_no_hidden_values("\n".join(r.getMessage() for r in caplog.records))


def test_同じ名前の前の特徴は置き換え_ほかの人は残す(rig: Rig) -> None:
    _seed_people()
    _script(rig, range(1000))

    code = cli.main(
        ["--learn-face", HIDDEN_ID, "--call-name", "あたらしいよびな", "--fake-hardware"]
    )

    assert code == 0
    book = FaceBook.load(_store())
    people = {p.person_id: p for p in book.people()}
    assert set(people) == {HIDDEN_ID, OTHER_ID}
    assert people[HIDDEN_ID].call_name == "あたらしいよびな"
    assert len(people[HIDDEN_ID].samples) == texts.LEARN_SAMPLES
    assert len(people[OTHER_ID].samples) == 3


def test_10_個に届かなければ何も保存せず_うまくおぼえられなかったと話す(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "LEARN_DURATION_S", 0.3)
    _script(rig, range(9))  # 9 個だけ集まる

    code = cli.main(["--learn-face", HIDDEN_ID, "--call-name", HIDDEN_NAME, "--fake-hardware"])

    assert code == 1
    said = rig.said()
    assert said.endswith(texts.LEARN_FAILED)  # 5.5
    assert texts.LEARN_TURN not in said  # 半分に届かなかった
    assert "おぼえたよ" not in said
    assert not _faces_file().exists()
    out, err = capsys.readouterr()
    _assert_no_hidden_values(out + err)


def test_ちょうど_10_個なら保存する(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "LEARN_DURATION_S", 0.3)
    _script(rig, range(10))

    code = cli.main(["--learn-face", HIDDEN_ID, "--call-name", HIDDEN_NAME, "--fake-hardware"])

    assert code == 0
    (person,) = FaceBook.load(_store()).people()
    assert len(person.samples) == 10


def test_足りないときは前の特徴をそのまま残す(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_people()
    before = _faces_file().read_bytes()
    monkeypatch.setattr(cli, "LEARN_DURATION_S", 0.2)
    _script(rig, range(0))  # 顔が見えない

    code = cli.main(["--learn-face", HIDDEN_ID, "--call-name", HIDDEN_NAME, "--fake-hardware"])

    assert code == 1
    assert _faces_file().read_bytes() == before


def test_覚えた顔を読めなければ始めず_決まった文言を出して前のファイルを残す(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _faces_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 99, "people": {}}), encoding="utf-8")
    before = path.read_bytes()
    _script(rig, range(1000))

    code = cli.main(["--learn-face", HIDDEN_ID, "--call-name", HIDDEN_NAME, "--fake-hardware"])

    assert code == 1
    assert cli.UNREADABLE_MESSAGE in capsys.readouterr().err
    assert path.read_bytes() == before  # ほかの人の特徴を黙って消さない
    assert rig.built == 0  # デバイスを作らない


def test_カメラがなければ始めずに_1_で終える(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    rig.no_camera = True
    code = cli.main(["--learn-face", HIDDEN_ID, "--call-name", HIDDEN_NAME, "--fake-hardware"])
    assert code == 1
    assert cli.NO_CAMERA_MESSAGE in capsys.readouterr().err
    assert rig.built == 0


def test_撮れなくなったら保存せずに_1_で終える(rig: Rig) -> None:
    rig.camera = FakeCamera(unavailable=True)
    code = cli.main(["--learn-face", HIDDEN_ID, "--call-name", HIDDEN_NAME, "--fake-hardware"])
    assert code == 1
    assert not _faces_file().exists()


@pytest.mark.parametrize("person_id", ["Kid", "1kid", "kid-a", "a" * 33, ""])
def test_使えない名前なら_2_で終える(
    rig: Rig, person_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["--learn-face", person_id, "--call-name", HIDDEN_NAME, "--fake-hardware"])
    assert code == 2
    _, err = capsys.readouterr()
    assert HIDDEN_NAME not in err
    assert rig.built == 0


def test_呼び名がなければ_2_で終える(rig: Rig) -> None:
    assert cli.main(["--learn-face", HIDDEN_ID, "--call-name", "  ", "--fake-hardware"]) == 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["--learn-face", HIDDEN_ID, "--fake-hardware"])
    assert exc.value.code == 2


def test_無効になっていれば始めずに_1_で終える(rig: Rig) -> None:
    _write_config("presence:\n  enabled: false\n")
    code = cli.main(["--learn-face", HIDDEN_ID, "--call-name", HIDDEN_NAME, "--fake-hardware"])
    assert code == 1
    assert rig.built == 0


# --- ロック ---


@pytest.mark.parametrize(
    "argv",
    [
        ["--learn-face", HIDDEN_ID, "--call-name", HIDDEN_NAME, "--fake-hardware"],
        ["--forget-face", HIDDEN_ID],
        ["--forget-all"],
    ],
)
def test_ロボットが動いていれば_75_で終え_何も変えない(
    rig: Rig, held_lock: InstanceLock, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_people()
    before = _faces_file().read_bytes()
    assert cli.main(argv) == 75
    assert cli.LOCK_BUSY_MESSAGE in capsys.readouterr().err  # 5.8
    assert _faces_file().read_bytes() == before
    assert rig.built == 0


# --- 消す ---


def test_1_人を消すと特徴とあいさつの記録を消す(
    xdg_dirs: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_people()
    assert cli.main(["--forget-face", HIDDEN_ID]) == 0  # 5.6
    people = FaceBook.load(_store()).people()
    assert [p.person_id for p in people] == [OTHER_ID]
    data = _greetings()
    assert data["last_seen"] == {OTHER_ID: WHEN.isoformat(), UNKNOWN: WHEN.isoformat()}
    assert data["morning"] == {OTHER_ID: "2026-11-07"}
    out, err = capsys.readouterr()
    assert out.strip()
    _assert_no_hidden_values(out + err)


def test_覚えていない名前を消そうとすると_2_で終え_一覧を出さない(
    xdg_dirs: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_people()
    before = _faces_file().read_bytes()
    assert cli.main(["--forget-face", "zznobody"]) == 2
    out, err = capsys.readouterr()
    assert cli.UNKNOWN_PERSON_MESSAGE in err
    _assert_no_hidden_values(out + err)
    assert _faces_file().read_bytes() == before


def test_すべて消すと全員の特徴とあいさつの記録を消す(
    xdg_dirs: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_people()
    assert cli.main(["--forget-all"]) == 0  # 5.7
    book = FaceBook.load(_store())
    assert book.people() == ()
    assert book.readable
    data = _greetings()
    assert data["last_seen"] == {}
    assert data["morning"] == {}
    out, err = capsys.readouterr()
    assert out.strip()
    _assert_no_hidden_values(out + err)


def test_読めない帳面はすべて消すで直り_退避したファイルも消す(
    xdg_dirs: dict[str, Path],
) -> None:
    directory = Paths().state_dir_for("presence")
    directory.mkdir(parents=True, exist_ok=True)
    _faces_file().write_text("{こわれた", encoding="utf-8")
    assert not FaceBook.load(_store()).readable  # 退避される
    assert list(directory.glob("faces.corrupt-*.json"))

    assert cli.main(["--forget-all"]) == 0

    assert FaceBook.load(_store()).readable
    assert not list(directory.glob("faces.corrupt-*.json"))


def test_読めない帳面から_1_人は消さずに_1_で終える(
    xdg_dirs: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    path = _faces_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 99}), encoding="utf-8")
    before = path.read_bytes()
    assert cli.main(["--forget-face", HIDDEN_ID]) == 1
    assert cli.UNREADABLE_MESSAGE in capsys.readouterr().err
    assert path.read_bytes() == before


# --- 一覧 ---


def test_一覧は名前と見本の数だけを出し_呼び名を出さない(
    xdg_dirs: dict[str, Path], held_lock: InstanceLock, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_people()
    assert cli.main(["--list"]) == 0  # ロックはいらない（5.9）
    out, err = capsys.readouterr()
    lines = out.strip().splitlines()
    assert len(lines) == 2
    # 名前の順
    assert lines[0].split()[0] == HIDDEN_ID and "2" in lines[0]
    assert lines[1].split()[0] == OTHER_ID and "3" in lines[1]
    for hidden in (HIDDEN_NAME, OTHER_NAME):
        assert hidden not in out + err


def test_覚えた人がいなければそう出す(
    xdg_dirs: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--list"]) == 0
    assert cli.NO_PEOPLE_MESSAGE in capsys.readouterr().out


def test_一覧で帳面を読めなければ決まった文言で_1_で終える(
    xdg_dirs: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    path = _faces_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 99}), encoding="utf-8")
    assert cli.main(["--list"]) == 1
    assert cli.UNREADABLE_MESSAGE in capsys.readouterr().err


def test_どれか_1_つの指定がいる() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["--list", "--forget-all"])
    assert exc.value.code == 2


def test_覚える機能の申告は見つけてあいさつと同じで_self_check_の項目はない() -> None:
    from osekkai.features.presence.feature import PresenceFeature

    feature = cli.make_learn_feature(
        HIDDEN_ID, HIDDEN_NAME, cli.LearnOutcome(), camera=FakeCamera(), vision=FakeVision()
    )
    assert feature.spec.name == PresenceFeature.spec.name
    assert feature.spec.priority == PresenceFeature.spec.priority
    assert feature.spec.resources == PresenceFeature.spec.resources
    assert feature.spec.config_model is PresenceConfig
    assert feature.spec.checks == ()


def test_main_module_はコマンドの入口を呼ぶ() -> None:
    from osekkai.features.presence import __main__ as entry

    assert entry.main is cli.main
