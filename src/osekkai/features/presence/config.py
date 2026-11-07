"""見つけてあいさつの設定（`presence:` の節）の形と検証（1.8、3.10、8.1〜8.3）。

誤りの文言には、場所（キーのパス）と理由だけを入れる。設定の値は入れない。
時刻の書き方は日課と音声の入口の設定と同じ決まり。共通の補助は作らず、同じ規則をここに書く。
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import AfterValidator, BeforeValidator, Field, ValidationInfo, field_validator

from osekkai.core.config import FeatureConfig

if TYPE_CHECKING:
    from pathlib import Path

    from osekkai.core.paths import Paths

MODELS_DIR_NAME: Final = "models"
# 初期設定（deploy の provision）が置くファイルの名前と同じにする
DETECTION_MODEL_NAME: Final = "face_detection_yunet_2023mar.onnx"
RECOGNITION_MODEL_NAME: Final = "face_recognition_sface_2021dec.onnx"

_MINUTES_PER_DAY = 24 * 60
# YAML の 60 進の数の H:MM は、先頭が 1〜9 なので 60（1:00）より小さくならない
_MIN_BASE60 = 60

MSG_TIME_FORMAT = '時刻は "HH:MM" のように書いてください'
MSG_TIME_ZONE = "時刻にタイムゾーンは書けません"
MSG_TIME_SECONDS = "時刻に秒は書けません"
MSG_HOURS_EMPTY = "時間帯の始めと終わりが同じです（止めるときは enabled: false にしてください）"
MSG_OKAERI_SHORT = "okaeri_minutes は away_minutes 以上にしてください"


def _time_before(value: object) -> object:
    """YAML が 60 進の数として読んだ時刻（例: 20:30 → 1230）を、時と分に戻す。

    60 より小さい数（例: 7）は、時刻の書き方の誤りにする。
    """
    if isinstance(value, bool):
        raise ValueError(MSG_TIME_FORMAT)
    if isinstance(value, int):
        if _MIN_BASE60 <= value < _MINUTES_PER_DAY:
            return time(value // 60, value % 60)
        raise ValueError(MSG_TIME_FORMAT)
    if isinstance(value, float):
        raise ValueError(MSG_TIME_FORMAT)
    return value


def _time_after(value: time) -> time:
    if value.tzinfo is not None:
        raise ValueError(MSG_TIME_ZONE)
    if value.second or value.microsecond:
        raise ValueError(MSG_TIME_SECONDS)
    return value


_Time = Annotated[time, BeforeValidator(_time_before), AfterValidator(_time_after)]


class PresenceConfig(FeatureConfig):
    """見つけてあいさつの設定（`presence:` の節）。"""

    # あいさつしてよい時間帯（3.10）。始めを含み、終わりを含まない。
    # 始めが終わりより後なら、日をまたぐ帯
    greet_hours: tuple[_Time, _Time] = (time(7, 0), time(20, 30))
    away_minutes: int = Field(default=20, ge=1, le=600)  # あいさつする見えなかった時間（3.1）
    # おかえりにする見えなかった時間（3.4）。away_minutes より短いとやっほーにならないので誤り
    okaeri_minutes: int = Field(default=120, ge=1, le=1440)
    morning_end: _Time = time(11, 0)  # この時刻より前なら朝のあいさつ（3.3）
    match_threshold: float = Field(default=0.38, ge=0.0, le=1.0)  # 似ているかのしきい値（2.3）
    pan_reverse: bool = False  # 首の左右を反転する（1.8）
    tilt_reverse: bool = False  # 首の上下を反転する
    gaze_reverse: bool = False  # 待機画面の目の向きを反転する
    flip: bool = False  # 映像の上下（180 度）を反転する

    @field_validator("greet_hours")
    @classmethod
    def _check_hours(cls, value: tuple[time, time]) -> tuple[time, time]:
        start, end = value
        if start == end:
            raise ValueError(MSG_HOURS_EMPTY)
        return value

    @field_validator("okaeri_minutes")
    @classmethod
    def _check_okaeri(cls, value: int, info: ValidationInfo) -> int:
        away = info.data.get("away_minutes")  # away_minutes が誤りのときは入っていない
        if isinstance(away, int) and value < away:
            raise ValueError(MSG_OKAERI_SHORT)
        return value


def model_paths(paths: Paths) -> tuple[Path, Path]:
    """顔の検出と認識のモデルの置き場所（`<データの置き場所>/models/` の下）。

    機能（アダプタを作るとき）と self-check の両方が使う。
    """
    models = paths.data_dir / MODELS_DIR_NAME
    return models / DETECTION_MODEL_NAME, models / RECOGNITION_MODEL_NAME
