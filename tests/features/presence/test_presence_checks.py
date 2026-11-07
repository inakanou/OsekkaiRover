"""見つけてあいさつの self-check の項目（presence.camera・presence.models・presence.faces。
6.4、8.4）のテスト。

一時の保存先（XDG）と設定ファイルから基盤の ConfigLoader で報告を作り、
基盤の SelfCheckRunner で動かす。picamera2・cv2 は開発 PC と CI にないので、
読み込めるか・カメラの台数は checks の補助を偽物に替える。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osekkai.core.config import ConfigLoader, ConfigReport
from osekkai.core.paths import Paths
from osekkai.core.selfcheck import (
    REASON_SKIPPED_BUSY,
    CheckContext,
    CheckResult,
    CheckStatus,
    SelfCheckRunner,
    render_lines,
)
from osekkai.features.presence import checks as checks_module
from osekkai.features.presence.checks import (
    CAMERA_TIMEOUT_S,
    PRESENCE_CHECKS,
    REASON_CONFIG_UNUSABLE,
    REASON_DISABLED,
    REASON_FACES_UNREADABLE,
    REASON_NO_CAMERA,
    REASON_NO_LIBRARY,
    REASON_NO_MODEL,
)
from osekkai.features.presence.config import PresenceConfig, model_paths
from osekkai.features.presence.faces import EMBEDDING_SIZE

CAMERA = "presence.camera"
MODELS = "presence.models"
FACES = "presence.faces"

# 出力に出てはいけない、目立つ名前と呼び名
HIDDEN_ID = "zzhiddenid"
HIDDEN_NAME = "ひみつのよびな"


@pytest.fixture
def paths(xdg_dirs: dict[str, Path]) -> Paths:
    return Paths()


class Helpers:
    """checks の補助の偽物。呼ばれた回数も数える。"""

    def __init__(self, *, picamera: bool = True, opencv: bool = True, cameras: int = 1) -> None:
        self.picamera = picamera
        self.opencv = opencv
        self.cameras = cameras
        self.count_calls = 0

    def camera_count(self) -> int:
        self.count_calls += 1
        return self.cameras


@pytest.fixture
def helpers(monkeypatch: pytest.MonkeyPatch) -> Helpers:
    fake = Helpers()
    monkeypatch.setattr(checks_module, "picamera_available", lambda: fake.picamera)
    monkeypatch.setattr(checks_module, "opencv_available", lambda: fake.opencv)
    monkeypatch.setattr(checks_module, "camera_count", fake.camera_count)
    return fake


def _report(paths: Paths, text: str | None = "presence: {}\n") -> ConfigReport:
    if text is not None:
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        paths.config_file.write_text(text, encoding="utf-8")
    return ConfigLoader(paths, {"presence": PresenceConfig}).load(create_missing=False)


def _run(
    paths: Paths, text: str | None = "presence: {}\n", *, hardware: bool = True
) -> dict[str, CheckResult]:
    ctx = CheckContext(
        paths=paths, report=_report(paths, text), service=None, hardware_allowed=hardware
    )
    return dict(SelfCheckRunner().run(PRESENCE_CHECKS, ctx))


def _put_models(paths: Paths, which: tuple[int, ...] = (0, 1)) -> None:
    files = model_paths(paths)
    for index in which:
        files[index].parent.mkdir(parents=True, exist_ok=True)
        files[index].write_bytes(b"x")


def _faces_dir(paths: Paths) -> Path:
    directory = paths.state_dir_for("presence")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_faces(paths: Paths, content: str | bytes) -> Path:
    target = _faces_dir(paths) / "faces.json"
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    return target


def _valid_faces() -> str:
    sample = [0.0] * EMBEDDING_SIZE
    sample[0] = 1.0
    data = {
        "version": 1,
        "people": {HIDDEN_ID: {"call_name": HIDDEN_NAME, "samples": [sample]}},
    }
    return json.dumps(data, ensure_ascii=False)


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())}


# --- 申告 ---


def test_項目の名前と種類() -> None:
    by_name = {check.name: check for check in PRESENCE_CHECKS}
    assert list(by_name) == [CAMERA, MODELS, FACES]
    assert by_name[CAMERA].hardware is True
    assert by_name[CAMERA].timeout_s == CAMERA_TIMEOUT_S
    assert 0 < CAMERA_TIMEOUT_S <= 10.0
    assert by_name[MODELS].hardware is False
    assert by_name[FACES].hardware is False
    assert all(check.owner == "presence" for check in PRESENCE_CHECKS)


def test_理由は決まった文言() -> None:
    assert REASON_NO_CAMERA == "カメラが見つからない"
    assert REASON_NO_MODEL == "顔のモデルがない"
    assert REASON_FACES_UNREADABLE == "覚えた顔を読めない"
    assert REASON_NO_LIBRARY == "カメラのライブラリがない"
    assert REASON_DISABLED == "無効"
    assert REASON_CONFIG_UNUSABLE == "見つけてあいさつの設定を使えない"


# --- 開発 PC の既定 ---


def test_開発_PC_の既定では_カメラとモデルが飛ばしたになる(paths: Paths) -> None:
    # 補助を偽物にしない（picamera2・cv2 は開発 PC と CI にない）
    results = _run(paths)
    assert results[CAMERA] == CheckResult(CheckStatus.SKIPPED, REASON_NO_LIBRARY)
    assert results[MODELS] == CheckResult(CheckStatus.SKIPPED, REASON_NO_LIBRARY)
    assert results[FACES] == CheckResult(CheckStatus.OK)


# --- カメラ ---


def test_カメラが_1_台以上あればよい(paths: Paths, helpers: Helpers) -> None:
    helpers.cameras = 2
    results = _run(paths)
    assert results[CAMERA] == CheckResult(CheckStatus.OK)
    assert helpers.count_calls == 1


def test_カメラがなければだめ(paths: Paths, helpers: Helpers) -> None:
    helpers.cameras = 0
    results = _run(paths)
    assert results[CAMERA] == CheckResult(CheckStatus.FAILED, REASON_NO_CAMERA)


def test_ハードを触れなければカメラは基盤が飛ばす(paths: Paths, helpers: Helpers) -> None:
    _put_models(paths)
    results = _run(paths, hardware=False)
    assert results[CAMERA] == CheckResult(CheckStatus.SKIPPED, REASON_SKIPPED_BUSY)
    assert results[MODELS] == CheckResult(CheckStatus.OK)
    assert helpers.count_calls == 0


# --- モデル ---


def test_モデルが_2_つあればよい(paths: Paths, helpers: Helpers) -> None:
    _put_models(paths)
    results = _run(paths)
    assert results[MODELS] == CheckResult(CheckStatus.OK)


@pytest.mark.parametrize("present", [(), (0,), (1,)])
def test_モデルが_1_つでも欠けるとだめ(
    paths: Paths, helpers: Helpers, present: tuple[int, ...]
) -> None:
    _put_models(paths, present)
    results = _run(paths)
    assert results[MODELS] == CheckResult(CheckStatus.FAILED, REASON_NO_MODEL)


def test_モデルの名前のディレクトリはモデルにしない(paths: Paths, helpers: Helpers) -> None:
    for file in model_paths(paths):
        file.mkdir(parents=True)
    results = _run(paths)
    assert results[MODELS] == CheckResult(CheckStatus.FAILED, REASON_NO_MODEL)


# --- ライブラリ ---


@pytest.mark.parametrize(("picamera", "opencv"), [(False, True), (True, False), (False, False)])
def test_ライブラリがなければカメラとモデルを飛ばし_覚えた顔は確かめる(
    paths: Paths, helpers: Helpers, picamera: bool, opencv: bool
) -> None:
    helpers.picamera = picamera
    helpers.opencv = opencv
    _write_faces(paths, "{")
    results = _run(paths)
    assert results[CAMERA] == CheckResult(CheckStatus.SKIPPED, REASON_NO_LIBRARY)
    assert results[MODELS] == CheckResult(CheckStatus.SKIPPED, REASON_NO_LIBRARY)
    assert results[FACES] == CheckResult(CheckStatus.OK, REASON_FACES_UNREADABLE)
    assert helpers.count_calls == 0


# --- 覚えた顔 ---


def test_覚えた顔がなければよい(paths: Paths, helpers: Helpers) -> None:
    results = _run(paths)
    assert results[FACES] == CheckResult(CheckStatus.OK)
    # 確かめるだけで、保存先を作らない
    assert not paths.state_dir_for("presence").exists()


def test_読める覚えた顔はよい(paths: Paths, helpers: Helpers) -> None:
    _write_faces(paths, _valid_faces())
    # 退避されたものが残っていても、読める faces.json があれば理由を添えない
    (_faces_dir(paths) / "faces.corrupt-20260101T000000.json").write_text("{", encoding="utf-8")
    results = _run(paths)
    assert results[FACES] == CheckResult(CheckStatus.OK)


def test_誰もいない覚えた顔はよい(paths: Paths, helpers: Helpers) -> None:
    _write_faces(paths, '{"version": 1, "people": {}}')
    results = _run(paths)
    assert results[FACES] == CheckResult(CheckStatus.OK)


BAD_FACES: list[str | bytes] = [
    "{",
    b"\xff\xfe\x00",
    "[]",
    "null",
    '{"version": 2, "people": {}}',
    '{"version": true, "people": {}}',
    '{"people": {}}',
    '{"version": 1}',
    '{"version": 1, "people": []}',
    '{"version": 1, "people": {"a": 1}}',
    '{"version": 1, "people": {"a": {"call_name": "x"}}}',
    '{"version": 1, "people": {"a": {"call_name": "x", "samples": [[1.0, 2.0]]}}}',
    '{"version": 1, "people": {"a": {"call_name": "x", "samples": []}}}',
    '{"version": 1, "people": {"BAD ID": {"call_name": "x", "samples": [[1.0]]}}}',
]


@pytest.mark.parametrize("content", BAD_FACES)
def test_読めない覚えた顔はよいに理由を添え_ファイルは動かさない(
    paths: Paths, helpers: Helpers, content: str | bytes
) -> None:
    _write_faces(paths, content)
    directory = _faces_dir(paths)
    before = _snapshot(directory)
    results = _run(paths)
    assert results[FACES] == CheckResult(CheckStatus.OK, REASON_FACES_UNREADABLE)
    assert _snapshot(directory) == before


def test_faces_json_が開けなければよいに理由を添える(paths: Paths, helpers: Helpers) -> None:
    (_faces_dir(paths) / "faces.json").mkdir()
    results = _run(paths)
    assert results[FACES] == CheckResult(CheckStatus.OK, REASON_FACES_UNREADABLE)


def test_退避されたものだけが残っていればよいに理由を添え_動かさない(
    paths: Paths, helpers: Helpers
) -> None:
    directory = _faces_dir(paths)
    (directory / "faces.corrupt-20260101T000000.json").write_text("{", encoding="utf-8")
    before = _snapshot(directory)
    results = _run(paths)
    assert results[FACES] == CheckResult(CheckStatus.OK, REASON_FACES_UNREADABLE)
    assert _snapshot(directory) == before


def test_ほかの退避されたものは見ない(paths: Paths, helpers: Helpers) -> None:
    (_faces_dir(paths) / "records.corrupt-20260101T000000.json").write_text("{", encoding="utf-8")
    results = _run(paths)
    assert results[FACES] == CheckResult(CheckStatus.OK)


def test_出力に名前_呼び名_パスを出さない(paths: Paths, helpers: Helpers) -> None:
    helpers.cameras = 0
    _write_faces(paths, _valid_faces().replace('"version": 1', '"version": 9'))
    ctx = CheckContext(paths=paths, report=_report(paths), service=None, hardware_allowed=True)
    text = "\n".join(render_lines(SelfCheckRunner().run(PRESENCE_CHECKS, ctx)))
    for hidden in (
        HIDDEN_ID,
        HIDDEN_NAME,
        str(paths.state_dir),
        str(paths.data_dir),
        "faces.json",
        ".onnx",
    ):
        assert hidden not in text


# --- 飛ばした ---


def test_無効ならすべて飛ばす(paths: Paths, helpers: Helpers) -> None:
    _write_faces(paths, "{")
    results = _run(paths, "presence:\n  enabled: false\n")
    for name in (CAMERA, MODELS, FACES):
        assert results[name] == CheckResult(CheckStatus.SKIPPED, REASON_DISABLED)
    assert helpers.count_calls == 0


@pytest.mark.parametrize(
    "text",
    [
        None,  # 設定ファイルがない
        "presence:\n  away_minutes: 0\n",  # 節に誤りがある
        "presence: [\n",  # YAML として読めない
    ],
)
def test_設定を使えなければすべて飛ばす(paths: Paths, helpers: Helpers, text: str | None) -> None:
    results = _run(paths, text)
    for name in (CAMERA, MODELS, FACES):
        assert results[name] == CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)
    assert helpers.count_calls == 0


def test_節がなければ既定の設定で確かめる(paths: Paths, helpers: Helpers) -> None:
    _put_models(paths)
    results = _run(paths, "core: {}\n")
    assert results[CAMERA] == CheckResult(CheckStatus.OK)
    assert results[MODELS] == CheckResult(CheckStatus.OK)
    assert results[FACES] == CheckResult(CheckStatus.OK)
