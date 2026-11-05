"""開発 PC と実機の両方で使う型と、JSON の読み書き（1.10、7.3）。

実機の道具は標準出力に JSON を 1 つ書き、開発 PC がそれを読む。人に向けた文言は開発 PC が作る。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path
from typing import Literal, get_args


class ExitCode(IntEnum):
    """`rover-deploy` の終了コード。2 は argparse の使い方の誤りに使う。"""

    OK = 0  # 成功
    ABORTED = 10  # 中止。実機は変えていない
    ROLLED_BACK = 11  # 入れた版の確認が失敗し、前の版に戻した
    HALTED = 12  # 前の版の確認も失敗し、サービスを止めた（人の手が要る）
    UNKNOWN = 13  # 接続が切れて結果が分からない


class Running(Enum):
    """実機で動いているサービスの種類。"""

    NONE = "none"  # どちらも止まっている
    CORE = "core"  # 新しい基盤（osekkai.service）
    SAMPLE = "sample"  # サンプル（osekkai-rover.service）
    BOTH = "both"  # 両方


TargetKind = Literal["sample", "release"]
RefKind = Literal["tag", "branch"]
Outcome = Literal["succeeded", "aborted", "rolled_back", "halted"]

SHORT_SHA_LEN = 7
SAMPLE_LABEL = "sample"
_SHA_RE = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Target:
    """入れる版。サンプルなら名前と sha はどれも `None`。"""

    kind: TargetKind
    ref: str | None  # タグかブランチの名前
    ref_kind: RefKind | None
    sha: str | None  # 40 桁

    def __post_init__(self) -> None:
        if self.kind == "sample":
            if (self.ref, self.ref_kind, self.sha) != (None, None, None):
                raise ValueError("サンプルには版の名前を付けません")
            return
        if self.kind != "release":
            raise ValueError("版の種類が違います")
        if not isinstance(self.ref, str) or not self.ref:
            raise ValueError("版の名前がありません")
        if self.ref_kind not in get_args(RefKind):
            raise ValueError("タグかブランチかが分かりません")
        if not isinstance(self.sha, str) or not _SHA_RE.fullmatch(self.sha):
            raise ValueError("コミットの識別子は 40 桁の小文字の 16 進数にします")


def version_label(target: Target) -> str:
    """版の表示（7.3）。タグなら "v0.2.0"、ブランチなら "feat/x@abc1234"、サンプルなら "sample"。"""
    if target.kind == "sample":
        return SAMPLE_LABEL
    assert target.ref is not None and target.sha is not None
    if target.ref_kind == "tag":
        return target.ref
    return f"{target.ref}@{target.sha[:SHORT_SHA_LEN]}"


@dataclass(frozen=True)
class DeployResult:
    """1 回の入れ替えの結果。"""

    deploy_id: str  # "20261104T210000Z" の形
    outcome: Outcome
    running: Target | None  # 終わった後に動いている版
    failed_checks: tuple[str, ...]  # 失敗した確認の項目の名前と、固定の理由


# --- JSON の形 ---


def _optional_str(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} の形が違います")
    return value


def target_to_dict(target: Target) -> dict[str, object]:
    return {
        "kind": target.kind,
        "ref": target.ref,
        "ref_kind": target.ref_kind,
        "sha": target.sha,
    }


def target_from_dict(data: Mapping[str, object]) -> Target:
    """JSON の辞書から版を作る。形が違えば `ValueError`。"""
    kind = data.get("kind")
    if kind not in get_args(TargetKind):
        raise ValueError("kind の形が違います")
    ref_kind = _optional_str(data, "ref_kind")
    if ref_kind is not None and ref_kind not in get_args(RefKind):
        raise ValueError("ref_kind の形が違います")
    return Target(
        kind=kind,  # type: ignore[arg-type]
        ref=_optional_str(data, "ref"),
        ref_kind=ref_kind,  # type: ignore[arg-type]
        sha=_optional_str(data, "sha"),
    )


def result_to_dict(result: DeployResult) -> dict[str, object]:
    return {
        "deploy_id": result.deploy_id,
        "outcome": result.outcome,
        "running": None if result.running is None else target_to_dict(result.running),
        "failed_checks": list(result.failed_checks),
    }


def result_from_dict(data: Mapping[str, object]) -> DeployResult:
    """JSON の辞書から入れ替えの結果を作る。形が違えば `ValueError`。"""
    deploy_id = data.get("deploy_id")
    if not isinstance(deploy_id, str):
        raise ValueError("deploy_id の形が違います")
    outcome = data.get("outcome")
    if outcome not in get_args(Outcome):
        raise ValueError("outcome の形が違います")
    running_raw = data.get("running")
    if running_raw is not None and not isinstance(running_raw, dict):
        raise ValueError("running の形が違います")
    checks = data.get("failed_checks")
    if not isinstance(checks, list) or not all(isinstance(c, str) for c in checks):
        raise ValueError("failed_checks の形が違います")
    return DeployResult(
        deploy_id=deploy_id,
        outcome=outcome,  # type: ignore[arg-type]
        running=None if running_raw is None else target_from_dict(running_raw),
        failed_checks=tuple(checks),
    )


def dump_json(data: Mapping[str, object]) -> str:
    """1 行の JSON にする（実機の道具の標準出力と、記録の 1 行に使う）。"""
    return json.dumps(dict(data), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_json_object(text: str) -> dict[str, object]:
    """JSON の辞書を読む。辞書でなければ `ValueError`。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError("JSON として読めません") from e
    if not isinstance(data, dict):
        raise ValueError("JSON の形が違います")
    return data


def read_json_file(path: Path) -> dict[str, object] | None:
    """JSON のファイルを読む。なければ `None`。壊れていれば `ValueError`。"""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("JSON のファイルの文字が読めません") from e
    return load_json_object(text)


def write_json_file(path: Path, data: Mapping[str, object]) -> None:
    """JSON のファイルを、本人だけが読み書きできる権限（0600）で原子的に書く。

    書けなかったときは前の内容が残る。ディレクトリは作らない。
    """
    text = json.dumps(dict(data), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
