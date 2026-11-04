"""基盤の設定と、設定ファイルの読み込み・検証（3.3、8.5、11.1〜11.7）。

ログと報告には、誤りの場所（キーのパス）と理由だけを出し、設定の値は出さない。
機能の契約（`core.feature`）は読み込まない。機能の設定のモデルは「名前 → モデル」で受け取る。
"""

from __future__ import annotations

import logging
import types
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import time
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from osekkai.core.paths import Paths, ensure_private_dir

logger = logging.getLogger(__name__)

CORE_SECTION = "core"
EXAMPLE_RESOURCE = "example_config.yaml"


class VoiceSettings(BaseModel):
    """声の合成と再生の設定。"""

    open_jtalk: str = "open_jtalk"
    dictionary: Path = Path("/var/lib/mecab/dic/open-jtalk/naist-jdic")
    htsvoice: Path = Path("~/.local/share/osekkai/voices/tohoku-f01-happy.htsvoice")
    pitch: float = Field(default=3.0, ge=-24, le=24)
    speed: float = Field(default=1.05, ge=0.5, le=2.0)
    sink: str = ""  # 空なら既定の再生先


class Calibration(BaseModel):
    """実機に合わせる調整値（8.5）。"""

    steering: float = Field(default=0.0, ge=-20, le=20)
    pan: float = Field(default=0.0, ge=-20, le=20)
    tilt: float = Field(default=0.0, ge=-20, le=20)
    motor_direction: tuple[Literal[1, -1], Literal[1, -1]] = (1, 1)


class MotionSettings(BaseModel):
    """動きの設定。"""

    allow_drive_in_dance: bool = False
    pan_range: tuple[float, float] = (-80.0, 80.0)
    tilt_range: tuple[float, float] = (-30.0, 60.0)
    calibration: Calibration = Field(default_factory=Calibration)


class HardwareSettings(BaseModel):
    """ハードの設定。"""

    lcd_address: int = Field(default=0x27, ge=0x03, le=0x77)
    extra_button_pin: str | None = None  # 例: "D0"
    motor_driver: Literal["auto", "pwm_dir", "dual_pwm"] = "auto"
    speaker_enable_pin: Literal["auto"] | int = "auto"


class CoreConfig(BaseModel):
    """基盤の設定（`core` の節）。"""

    model_config = ConfigDict(extra="allow")  # 知らない項目は警告にする（11.6）

    startup_voice_hours: tuple[time, time] = (time(8, 0), time(20, 0))
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    motion: MotionSettings = Field(default_factory=MotionSettings)
    hardware: HardwareSettings = Field(default_factory=HardwareSettings)
    cache_max_mb: int = Field(default=200, ge=10, le=5000)


class FeatureConfig(BaseModel):
    """機能の設定の基本の型。機能はこれを継いで自分の項目を足す（11.7）。"""

    model_config = ConfigDict(extra="allow")  # 知らない項目は警告にする（11.6）

    enabled: bool = True


@dataclass(frozen=True)
class ConfigReport:
    """設定の検証の結果。

    - `fatal_errors`: YAML の誤り・トップレベルの形の誤り・`core` の誤り。
      このとき `core` は既定値。YAML として読めなかったときは `features` は空。
      `core` だけの誤りのときも、機能の節は検証して報告する（機能を開始しないのは登録の側）
    - `feature_errors`: 機能の節の誤り。その機能は `features` に入らない
    - `warnings`: 知らない項目のキーのパス
    """

    core: CoreConfig
    features: Mapping[str, FeatureConfig]
    fatal_errors: Sequence[str] = field(default_factory=list)
    feature_errors: Mapping[str, Sequence[str]] = field(default_factory=dict)
    warnings: Sequence[str] = field(default_factory=list)
    created_from_example: bool = False


def example_config_bytes() -> bytes:
    """パッケージに同梱した設定の見本。"""
    return resources.files("osekkai.core").joinpath(EXAMPLE_RESOURCE).read_bytes()


def _model_types(annotation: Any) -> list[type[BaseModel]]:
    """注釈の中の pydantic のモデル（`X | None` も見る）。"""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        return [m for arg in typing.get_args(annotation) for m in _model_types(arg)]
    return []


def _unknown_keys(model: type[BaseModel], data: object, prefix: str) -> list[str]:
    """モデルが知らない項目のキーのパスを、入れ子の中まで集める。"""
    if not isinstance(data, Mapping):
        return []
    found: list[str] = []
    fields = model.model_fields
    for key, value in data.items():
        path = f"{prefix}.{key}"
        info = fields.get(str(key))
        if info is None:
            found.append(path)
            continue
        nested = _model_types(info.annotation)
        if len(nested) == 1:
            found.extend(_unknown_keys(nested[0], value, path))
    return found


def _error_messages(exc: ValidationError, prefix: str) -> list[str]:
    """pydantic の誤りを「場所: 理由」にする。入力の値は入れない。"""
    messages: list[str] = []
    for err in exc.errors(include_url=False, include_context=False):
        loc = ".".join(str(part) for part in (prefix, *err["loc"]))
        reason = err["msg"]
        value = _short_input(err)
        if value is not None and value in reason:
            reason = err["type"]  # 理由に値が入っていたら種類だけにする
        messages.append(f"{loc}: {reason}")
    return messages


def _short_input(err: Mapping[str, Any]) -> str | None:
    """誤りの入力が短い値なら、その文字列（理由に入っていないか確かめる用）。"""
    value = err.get("input")
    if isinstance(value, str | int | float) and str(value):
        return str(value)
    return None


def _yaml_error_message(exc: yaml.YAMLError) -> str:
    """YAML の誤りの場所と理由。元の行の抜き出しは値を含むので使わない。"""
    if isinstance(exc, yaml.MarkedYAMLError):
        mark = exc.problem_mark or exc.context_mark
        reason = exc.problem or exc.context or "書き方の誤り"
        if mark is not None:
            return f"YAML（{mark.line + 1} 行 {mark.column + 1} 列）: {reason}"
        return f"YAML: {reason}"
    return f"YAML: {type(exc).__name__}"


class ConfigLoader:
    """設定ファイルを読み、基盤と機能の設定を検証して報告にまとめる。"""

    def __init__(self, paths: Paths, feature_models: Mapping[str, type[FeatureConfig]]) -> None:
        self._paths = paths
        self._feature_models = dict(feature_models)

    def load(self, override: Path | None = None) -> ConfigReport:
        created = False
        if override is not None:
            path = override
        else:
            path = self._paths.config_file
            if not path.exists():
                self._create_from_example(path)
                created = True

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return self._fatal(f"設定ファイルを読めません（{path}）: {type(exc).__name__}", created)
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            return self._fatal(_yaml_error_message(exc), created)
        if data is None:
            data = {}
        if not isinstance(data, Mapping):
            return self._fatal("トップレベルはキーと値の対応表にしてください", created)

        fatal_errors: list[str] = []
        warnings: list[str] = []
        feature_errors: dict[str, list[str]] = {}
        features: dict[str, FeatureConfig] = {}

        for key in data:
            if key != CORE_SECTION and key not in self._feature_models:
                warnings.append(f"{key}: 知らない項目です")

        core_data = data.get(CORE_SECTION)
        warnings.extend(
            f"{p}: 知らない項目です" for p in _unknown_keys(CoreConfig, core_data, CORE_SECTION)
        )
        try:
            core = CoreConfig.model_validate({} if core_data is None else core_data)
        except ValidationError as exc:
            fatal_errors.extend(_error_messages(exc, CORE_SECTION))
            core = CoreConfig()

        for name, model in self._feature_models.items():
            section = data.get(name)
            warnings.extend(f"{p}: 知らない項目です" for p in _unknown_keys(model, section, name))
            try:
                features[name] = model.model_validate({} if section is None else section)
            except ValidationError as exc:
                feature_errors[name] = _error_messages(exc, name)

        report = ConfigReport(
            core=core,
            features=features,
            fatal_errors=fatal_errors,
            feature_errors=feature_errors,
            warnings=warnings,
            created_from_example=created,
        )
        _log(report)
        return report

    def _create_from_example(self, path: Path) -> None:
        ensure_private_dir(path.parent)
        path.write_bytes(example_config_bytes())
        logger.info("設定ファイルがないので見本から作りました: %s", path)

    def _fatal(self, message: str, created: bool) -> ConfigReport:
        report = ConfigReport(
            core=CoreConfig(), features={}, fatal_errors=[message], created_from_example=created
        )
        _log(report)
        return report


def _log(report: ConfigReport) -> None:
    for message in report.fatal_errors:
        logger.error("設定の誤り（基盤）: %s", message)
    for name, messages in report.feature_errors.items():
        for message in messages:
            logger.error("設定の誤り（機能 %s）: %s", name, message)
    for message in report.warnings:
        logger.warning("設定の警告: %s", message)
