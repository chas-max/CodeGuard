"""质量门槛配置：最低合格线、退化阈值、最小样本量。

默认 `enforce: false`——门槛只写进报告（PASS/WARN/FAIL），不改变 CLI 退出码。
未来启用阻断时改 YAML 即可，不需要改评分代码。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import CAPABILITIES, CAPABILITY_WEIGHTS
from .paths import DEFAULT_THRESHOLDS

DEFAULT_FLOORS = {"F1": 70.0, "F2": 60.0, "F3": 60.0, "F4": 55.0, "F5": 80.0, "F6": 85.0}


@dataclass(frozen=True)
class Thresholds:
    enforce: bool = False
    floors: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_FLOORS))
    weights: dict[str, float] = field(default_factory=lambda: dict(CAPABILITY_WEIGHTS))
    regression_composite: float = 2.0
    regression_dimension: float = 5.0
    min_samples: int = 3
    recommended_samples: int = 10
    warn_margin: float = 2.0
    source: str = "defaults"

    def __post_init__(self) -> None:
        for capability, floor in self.floors.items():
            if capability not in CAPABILITIES:
                raise ValueError(f"门槛引用了未知能力 {capability!r}")
            if not 0 <= float(floor) <= 100:
                raise ValueError(f"{capability} 合格线必须在 0..100")
        for capability, weight in self.weights.items():
            if capability not in CAPABILITY_WEIGHTS:
                raise ValueError(f"综合权重引用了不可计权能力 {capability!r}")
            if float(weight) < 0:
                raise ValueError(f"{capability} 权重不能为负")
        total = sum(float(value) for value in self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"综合权重之和必须为 1（当前 {total}）")
        if self.min_samples < 1:
            raise ValueError("min_samples 必须 >= 1")
        if self.recommended_samples < self.min_samples:
            raise ValueError("recommended_samples 必须 >= min_samples")

    def floor(self, capability: str) -> float:
        return float(self.floors.get(capability, 0.0))

    def to_dict(self) -> dict:
        return {
            "enforce": self.enforce,
            "floors": {cap: self.floors.get(cap) for cap in CAPABILITIES},
            "weights": dict(self.weights),
            "regression": {
                "composite": self.regression_composite,
                "dimension": self.regression_dimension,
            },
            "min_samples": self.min_samples,
            "recommended_samples": self.recommended_samples,
            "warn_margin": self.warn_margin,
            "source": self.source,
        }


def load_thresholds(path: Path | str | None = None) -> Thresholds:
    target = Path(path) if path else DEFAULT_THRESHOLDS
    if not target.is_file():
        return Thresholds()
    try:
        payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"读取门槛配置失败 {target}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("门槛配置必须是 YAML 对象")
    floors = dict(DEFAULT_FLOORS)
    for capability, value in (payload.get("floors") or {}).items():
        floors[str(capability)] = float(value)
    weights = dict(CAPABILITY_WEIGHTS)
    for capability, value in (payload.get("weights") or {}).items():
        weights[str(capability)] = float(value)
    regression = payload.get("regression") or {}
    samples = payload.get("min_samples") or {}
    return Thresholds(
        enforce=bool(payload.get("enforce", False)),
        floors=floors,
        weights=weights,
        regression_composite=float(regression.get("composite", 2.0)),
        regression_dimension=float(regression.get("dimension", 5.0)),
        min_samples=int(samples.get("default", 3)),
        recommended_samples=int(samples.get("recommended", 10)),
        warn_margin=float(payload.get("warn_margin", 2.0)),
        source=target.as_posix(),
    )


__all__ = ["DEFAULT_FLOORS", "Thresholds", "load_thresholds"]
