"""機能の発見と、開始してよい機能の選び出し。"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points

from osekkai.core.config import ConfigReport, FeatureConfig
from osekkai.core.feature import Feature, FeatureSpec, validate_spec

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "osekkai.features"

REASON_LOAD = "読み込めない"
REASON_NOT_FEATURE = "機能の申告がない"
REASON_DUPLICATE = "名前が重なる"
REASON_CONFIG = "設定の誤り"
REASON_DISABLED = "無効"


@dataclass(frozen=True)
class FeaturePlan:
    """開始する機能と、その設定。"""

    feature_cls: type[Feature]
    config: FeatureConfig


class FeatureRegistry:
    """機能を発見し、開始してよいものを選ぶ。

    `discovered` が None ならエントリーポイントの群から読み込む（名前の順）。
    除いた機能は `skipped()` で (名前, 理由) として得られる。名前は、申告を
    確かめられたものは `spec.name`、それより前に除いたものはエントリーポイントの名前
    （直接渡したときはクラスの名前）。
    """

    def __init__(self, discovered: Sequence[type[Feature]] | None = None) -> None:
        self._discovery_skipped: list[tuple[str, str]] = []
        self._plan_skipped: list[tuple[str, str]] = []
        if discovered is None:
            candidates = self._load_entry_points()
        else:
            candidates = [(getattr(cls, "__name__", repr(cls)), cls) for cls in discovered]
        self._accepted = self._accept(candidates)

    def feature_classes(self) -> Sequence[type[Feature]]:
        return list(self._accepted)

    def skipped(self) -> Sequence[tuple[str, str]]:
        """発見で除いた機能と、最後の `plan()` で除いた機能。"""
        return [*self._discovery_skipped, *self._plan_skipped]

    def plan(self, report: ConfigReport) -> Sequence[FeaturePlan]:
        self._plan_skipped = []
        plans: list[FeaturePlan] = []
        for cls in self._accepted:
            spec = cls.spec
            if report.fatal_errors or spec.name in report.feature_errors:
                self._skip_planned(spec.name, REASON_CONFIG)
                continue
            config = report.features.get(spec.name)
            if config is None:
                config = spec.config_model()
            if not config.enabled:
                self._skip_planned(spec.name, REASON_DISABLED)
                continue
            plans.append(FeaturePlan(cls, config))
        return plans

    def _load_entry_points(self) -> list[tuple[str, object]]:
        found = sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda ep: (ep.name, ep.value))
        candidates: list[tuple[str, object]] = []
        for ep in found:
            try:
                candidates.append((ep.name, ep.load()))
            except Exception as exc:
                logger.warning("機能 %s を除く: %s（%s）", ep.name, REASON_LOAD, type(exc).__name__)
                self._discovery_skipped.append((ep.name, REASON_LOAD))
        return candidates

    def _accept(self, candidates: Iterable[tuple[str, object]]) -> list[type[Feature]]:
        accepted: list[type[Feature]] = []
        seen: set[str] = set()
        for label, obj in candidates:
            spec = getattr(obj, "spec", None) if isinstance(obj, type) else None
            if not isinstance(spec, FeatureSpec):
                self._skip_discovered(label, REASON_NOT_FEATURE)
                continue
            reasons = validate_spec(spec)
            if reasons:
                self._skip_discovered(label, "、".join(reasons))
                continue
            if spec.name in seen:
                self._skip_discovered(spec.name, REASON_DUPLICATE)
                continue
            seen.add(spec.name)
            accepted.append(obj)  # type: ignore[arg-type]  # spec を確かめたクラス
        return accepted

    def _skip_discovered(self, label: str, reason: str) -> None:
        logger.warning("機能 %s を除く: %s", label, reason)
        self._discovery_skipped.append((label, reason))

    def _skip_planned(self, name: str, reason: str) -> None:
        logger.info("機能 %s を開始しない: %s", name, reason)
        self._plan_skipped.append((name, reason))
