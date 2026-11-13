"""bench 相关路径常量：集中一处，避免 registry 与 adapters 循环导入。"""
from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
EVAL_ROOT = PACKAGE_ROOT.parent
PROJECT_ROOT = EVAL_ROOT.parent
BENCH_ROOT = EVAL_ROOT / "datasets" / "bench"
CORE_SUITE = EVAL_ROOT / "datasets" / "core" / "cases.core.jsonl"
REVIEWER_CASSETTE = EVAL_ROOT / "cassettes" / "reviewer_cases.json"
DEFAULT_REGISTRY = BENCH_ROOT / "registry.json"
DEFAULT_THRESHOLDS = BENCH_ROOT / "thresholds.yml"
DEFAULT_REPORTS = PROJECT_ROOT / "reports" / "bench"

__all__ = [
    "PACKAGE_ROOT", "EVAL_ROOT", "PROJECT_ROOT", "BENCH_ROOT", "CORE_SUITE",
    "REVIEWER_CASSETTE", "DEFAULT_REGISTRY", "DEFAULT_THRESHOLDS", "DEFAULT_REPORTS",
]
