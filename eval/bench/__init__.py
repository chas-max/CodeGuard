"""CodeGuard 评测平台：数据集登记、统一任务契约、两级功能评分与报告。

对外只暴露稳定类型与版本号；执行入口是 `python -m eval.bench`。
"""
from __future__ import annotations

from .models import (
    CAPABILITIES,
    CAPABILITY_NAMES,
    DEFAULT_ROLE,
    INTEGRATION,
    PRIMARY_REQUIRED,
    ROLES,
    DatasetEntry,
    Observation,
    RunMeta,
    ScoreRecord,
    TaskSpec,
)

SCHEMA_VERSION = 1

__all__ = [
    "SCHEMA_VERSION",
    "CAPABILITIES",
    "CAPABILITY_NAMES",
    "ROLES",
    "DEFAULT_ROLE",
    "PRIMARY_REQUIRED",
    "INTEGRATION",
    "DatasetEntry",
    "Observation",
    "RunMeta",
    "ScoreRecord",
    "TaskSpec",
]
