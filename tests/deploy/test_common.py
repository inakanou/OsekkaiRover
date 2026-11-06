"""道具の共通の型と JSON の読み書きのテスト（1.10、7.3）。"""

import json
import stat
from pathlib import Path

import pytest

from rover_deploy.common import (
    DeployResult,
    ExitCode,
    Running,
    Target,
    dump_json,
    load_json_object,
    read_json_file,
    result_from_dict,
    result_to_dict,
    target_from_dict,
    target_to_dict,
    version_label,
    write_json_file,
)

SHA = "0123456789abcdef0123456789abcdef01234567"

TAG = Target(kind="release", ref="v0.2.0", ref_kind="tag", sha=SHA)
BRANCH = Target(kind="release", ref="feat/x", ref_kind="branch", sha=SHA)
SAMPLE = Target(kind="sample", ref=None, ref_kind=None, sha=None)


# --- 終了コード（1.10） ---


def test_終了コードは結果ごとに違う() -> None:
    codes = {c.name: int(c) for c in ExitCode}
    assert codes == {"OK": 0, "ABORTED": 10, "ROLLED_BACK": 11, "HALTED": 12, "UNKNOWN": 13}
    assert len({int(c) for c in ExitCode}) == len(ExitCode)
    assert 2 not in {int(c) for c in ExitCode}  # 2 は argparse の使い方の誤り


# --- 版の表示（7.3） ---


def test_タグの版はタグの名前で表示する() -> None:
    assert version_label(TAG) == "v0.2.0"


def test_ブランチの版はブランチの名前と短い識別子で表示する() -> None:
    assert version_label(BRANCH) == "feat/x@0123456"


def test_サンプルは_sample_と表示する() -> None:
    assert version_label(SAMPLE) == "sample"


# --- 版の対象の形 ---


@pytest.mark.parametrize(
    ("ref", "ref_kind", "sha"),
    [
        (None, "tag", SHA),
        ("", "tag", SHA),
        ("v1", None, SHA),
        ("v1", "tag", None),
        ("v1", "tag", "abc1234"),
        ("v1", "tag", SHA.upper()),
        ("v1", "other", SHA),
    ],
)
def test_版の対象の形が違えば断る(ref: str | None, ref_kind: str | None, sha: str | None) -> None:
    with pytest.raises(ValueError):
        Target(kind="release", ref=ref, ref_kind=ref_kind, sha=sha)  # type: ignore[arg-type]


def test_サンプルに版の名前を付けたら断る() -> None:
    with pytest.raises(ValueError):
        Target(kind="sample", ref="v1", ref_kind="tag", sha=SHA)


def test_種類が違えば断る() -> None:
    with pytest.raises(ValueError):
        Target(kind="other", ref=None, ref_kind=None, sha=None)  # type: ignore[arg-type]


# --- 動いているサービスの種類 ---


def test_動いているサービスの種類() -> None:
    assert {r.value for r in Running} == {"none", "core", "sample", "both"}
    assert Running("core") is Running.CORE


# --- JSON の形 ---


@pytest.mark.parametrize("target", [TAG, BRANCH, SAMPLE])
def test_版の対象は_JSON_を通しても同じ(target: Target) -> None:
    text = dump_json(target_to_dict(target))
    assert target_from_dict(load_json_object(text)) == target


def test_版の対象の_JSON_の形() -> None:
    assert target_to_dict(BRANCH) == {
        "kind": "release",
        "ref": "feat/x",
        "ref_kind": "branch",
        "sha": SHA,
    }


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"kind": "release", "ref": "v1", "ref_kind": "tag"},
        {"kind": "release", "ref": 1, "ref_kind": "tag", "sha": SHA},
        {"kind": "release", "ref": "v1", "ref_kind": "tag", "sha": "x"},
        {"kind": "nope", "ref": None, "ref_kind": None, "sha": None},
    ],
)
def test_版の対象の_JSON_の形が違えば断る(data: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        target_from_dict(data)


@pytest.mark.parametrize(
    "result",
    [
        DeployResult("20261104T210000Z", "succeeded", TAG, ()),
        DeployResult(
            "20261104T210000Z", "rolled_back", SAMPLE, ("self-check:lcd", "no_version_log")
        ),
        DeployResult("20261104T210000Z", "halted", None, ("restart",)),
        DeployResult("20261104T210000Z", "aborted", BRANCH, ()),
    ],
)
def test_入れ替えの結果は_JSON_を通しても同じ(result: DeployResult) -> None:
    text = dump_json(result_to_dict(result))
    assert result_from_dict(load_json_object(text)) == result


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"deploy_id": "x", "outcome": "nope", "running": None, "failed_checks": []},
        {"deploy_id": "x", "outcome": "succeeded", "running": None, "failed_checks": [1]},
        {"deploy_id": 1, "outcome": "succeeded", "running": None, "failed_checks": []},
        {"deploy_id": "x", "outcome": "succeeded", "running": {}, "failed_checks": []},
        {"deploy_id": "x", "outcome": "succeeded", "running": None, "failed_checks": "a"},
    ],
)
def test_入れ替えの結果の_JSON_の形が違えば断る(data: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        result_from_dict(data)


def test_dump_json_は_1_行で書く() -> None:
    text = dump_json({"b": "版", "a": [1, 2]})
    assert "\n" not in text
    assert json.loads(text) == {"b": "版", "a": [1, 2]}
    assert "版" in text  # 日本語をそのまま書く


@pytest.mark.parametrize("text", ["[]", "1", "not json", ""])
def test_load_json_object_は辞書でなければ断る(text: str) -> None:
    with pytest.raises(ValueError):
        load_json_object(text)


# --- JSON のファイル ---


def test_JSON_のファイルを書いて読む(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    write_json_file(path, {"a": 1, "b": "版"})
    assert read_json_file(path) == {"a": 1, "b": "版"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_JSON_のファイルの書き込みは前の内容を置き換え一時ファイルを残さない(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    write_json_file(path, {"a": 1})
    write_json_file(path, {"a": 2})
    assert read_json_file(path) == {"a": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_書けない値なら前の内容を残す(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    write_json_file(path, {"a": 1})
    with pytest.raises(TypeError):
        write_json_file(path, {"a": object()})
    assert read_json_file(path) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_ないファイルは_None(tmp_path: Path) -> None:
    assert read_json_file(tmp_path / "none.json") is None


@pytest.mark.parametrize("raw", [b"[]", b"{", b"\xff\xfe"])
def test_壊れたファイルは断る(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "bad.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        read_json_file(path)
