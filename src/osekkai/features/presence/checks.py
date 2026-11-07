"""見つけてあいさつの self-check の項目（6.4、8.4）。

- `presence.camera`: カメラが 1 台以上あるか（ハードの項目）
- `presence.models`: 顔の検出と認識のモデルの 2 つのファイルがあるか
- `presence.faces`: 覚えた顔（`faces.json`）が読めるか。読めなくてもだめにしない
  （版を戻しても直らない）。よいに理由を添える

picamera2 か cv2 を読み込めない PC と CI では、カメラとモデルを飛ばす。
開発 PC と CI の self-check はこれで赤くならない。覚えた顔はそれでも確かめる。
設定で無効なとき・設定を使えないとき（ファイルがない・誤りがある）は、すべて飛ばす。
誤りは基盤の `core.config` が知らせる。

覚えた顔は自分で読むだけにする。`StateStore.load` は壊れたファイルを退避するので使わない。
理由はすべて固定の文言にする。パス・設定の値・名前・呼び名を入れない。
"""

from __future__ import annotations

import json
from typing import Final

from osekkai.core.selfcheck import Check, CheckContext, CheckResult, CheckStatus
from osekkai.features.presence.adapters.opencv import opencv_available
from osekkai.features.presence.adapters.picamera import camera_count, picamera_available
from osekkai.features.presence.config import PresenceConfig, model_paths
from osekkai.features.presence.faces import STORE_KEY, _parse, _Unreadable

__all__ = [
    "CAMERA_TIMEOUT_S",
    "PRESENCE_CHECKS",
    "REASON_CONFIG_UNUSABLE",
    "REASON_DISABLED",
    "REASON_FACES_UNREADABLE",
    "REASON_NO_CAMERA",
    "REASON_NO_LIBRARY",
    "REASON_NO_MODEL",
]

FEATURE_NAME: Final = "presence"

# 固定の理由の文言
REASON_NO_CAMERA: Final = "カメラが見つからない"
REASON_NO_MODEL: Final = "顔のモデルがない"
REASON_FACES_UNREADABLE: Final = "覚えた顔を読めない"
REASON_NO_LIBRARY: Final = "カメラのライブラリがない"
REASON_DISABLED: Final = "無効"
REASON_CONFIG_UNUSABLE: Final = "見つけてあいさつの設定を使えない"

# カメラを調べるのは libcamera を開くので少し時間がかかる
CAMERA_TIMEOUT_S: Final = 10.0
FILES_TIMEOUT_S: Final = 5.0

OK: Final = CheckResult(CheckStatus.OK)


def _config_skip(ctx: CheckContext) -> CheckResult | None:
    """設定を使えない・無効なら飛ばした結果、確かめるなら None を返す。"""
    config = ctx.report.features.get(FEATURE_NAME)
    if not isinstance(config, PresenceConfig):
        return CheckResult(CheckStatus.SKIPPED, REASON_CONFIG_UNUSABLE)
    if not config.enabled:
        return CheckResult(CheckStatus.SKIPPED, REASON_DISABLED)
    return None


def _camera_skip(ctx: CheckContext) -> CheckResult | None:
    """カメラとモデルの項目を飛ばすなら、その結果を返す。"""
    skipped = _config_skip(ctx)
    if skipped is not None:
        return skipped
    if not (picamera_available() and opencv_available()):
        return CheckResult(CheckStatus.SKIPPED, REASON_NO_LIBRARY)
    return None


def _check_camera(ctx: CheckContext) -> CheckResult:
    skipped = _camera_skip(ctx)
    if skipped is not None:
        return skipped
    if camera_count() >= 1:
        return OK
    return CheckResult(CheckStatus.FAILED, REASON_NO_CAMERA)


def _check_models(ctx: CheckContext) -> CheckResult:
    skipped = _camera_skip(ctx)
    if skipped is not None:
        return skipped
    if all(path.is_file() for path in model_paths(ctx.paths)):
        return OK
    return CheckResult(CheckStatus.FAILED, REASON_NO_MODEL)


def _faces_readable(raw: bytes) -> bool:
    """`faces.json` の中身が、帳面が読める形か（FaceBook.load と同じ決まり）。"""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return False
    if not isinstance(data, dict):
        return False
    try:
        _parse(data)
    except _Unreadable:
        return False
    return True


def _check_faces(ctx: CheckContext) -> CheckResult:
    skipped = _config_skip(ctx)
    if skipped is not None:
        return skipped
    directory = ctx.paths.state_dir_for(FEATURE_NAME)
    try:
        raw = (directory / f"{STORE_KEY}.json").read_bytes()
    except FileNotFoundError:
        # 退避された壊れたものだけが残っていれば、覚えた顔は使えていない
        if any(directory.glob(f"{STORE_KEY}.corrupt-*.json")):
            return CheckResult(CheckStatus.OK, REASON_FACES_UNREADABLE)
        return OK
    except OSError:
        return CheckResult(CheckStatus.OK, REASON_FACES_UNREADABLE)
    if _faces_readable(raw):
        return OK
    return CheckResult(CheckStatus.OK, REASON_FACES_UNREADABLE)


PRESENCE_CHECKS: Final[tuple[Check, ...]] = (
    # カメラはハードの項目。ハードを触れないとき（サービスが動いている・--no-hardware）は
    # サービスの報告にカメラの名前がないので、基盤が飛ばす
    Check(f"{FEATURE_NAME}.camera", _check_camera, timeout_s=CAMERA_TIMEOUT_S, hardware=True),
    Check(f"{FEATURE_NAME}.models", _check_models, timeout_s=FILES_TIMEOUT_S),
    Check(f"{FEATURE_NAME}.faces", _check_faces, timeout_s=FILES_TIMEOUT_S),
)
