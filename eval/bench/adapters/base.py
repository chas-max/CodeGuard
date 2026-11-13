"""适配器共享工具：JSONL 读取、字段容错、ScoreRecord 构造。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..metrics import ratio, wilson
from ..models import CAPABILITIES, DatasetEntry, Observation, ScoreRecord, TaskSpec


def load_jsonl(path: Path | str) -> list[dict]:
    target = Path(path)
    rows: list[dict] = []
    for line_number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{target}:{line_number} 不是合法 JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"{target}:{line_number} 必须是 JSON 对象")
        rows.append(item)
    return rows


def load_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def as_list(value: Any) -> list:
    """兼容 JSON 字符串、逗号分隔字符串与 None。"""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return list(parsed)
        return [part.strip() for part in text.split(",") if part.strip()]
    return [value]


def first_present(raw: dict, *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return default


def entry_capabilities(
    entry: DatasetEntry,
    fallback: tuple[str, ...],
    *,
    supported: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """能力由 registry 声明与适配器支持能力共同决定；未声明时退回 fallback。"""
    declared = tuple(
        cap for cap in CAPABILITIES if float(entry.capabilities.get(cap, 0.0) or 0.0) > 0
    )
    if not declared:
        return fallback
    if supported is None:
        return declared
    matched = tuple(cap for cap in declared if cap in supported)
    return matched or fallback


def task_capabilities(task: TaskSpec | None, observation: Observation) -> tuple[str, ...]:
    if task is not None and task.capabilities:
        return tuple(task.capabilities)
    return tuple(observation.capabilities)


def rate_record(
    *, task: TaskSpec, capability: str, metric: str, successes: float, total: float,
    sample_size: int, with_interval: bool = True,
) -> ScoreRecord:
    interval = wilson(successes, total) if with_interval else None
    return ScoreRecord(
        task_id=task.task_id, dataset=task.dataset, capability=capability, metric=metric,
        numerator=float(successes), denominator=float(total),
        value=ratio(successes, total), sample_size=sample_size, interval=interval,
    )


def metric_record(
    *, task: TaskSpec, capability: str, metric: str, numerator: float,
    denominator: float, value: float, sample_size: int,
    interval: tuple[float, float] | None = None,
) -> ScoreRecord:
    return ScoreRecord(
        task_id=task.task_id, dataset=task.dataset, capability=capability, metric=metric,
        numerator=float(numerator), denominator=float(denominator),
        value=float(value), sample_size=sample_size, interval=interval,
    )


__all__ = [
    "load_jsonl", "load_json", "as_list", "first_present",
    "entry_capabilities", "task_capabilities",
    "rate_record", "metric_record",
]
