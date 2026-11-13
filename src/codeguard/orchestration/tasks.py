"""协调器任务模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class TaskStatus(str, Enum):
    ok = "ok"
    failed = "failed"
    timeout = "timeout"
    cancelled = "cancelled"


@dataclass(frozen=True)
class Task:
    id: str
    stage: str
    fn: Callable[[], Any]
    depends_on: tuple[str, ...] = ()
    timeout_sec: int = 120
    retries: int = 0
    agent_role: str | None = None
    writes_workspace: bool = False
    idempotency_key: str | None = None


@dataclass
class TaskResult:
    task_id: str
    status: TaskStatus
    value: Any = None
    error: str | None = None
    duration_ms: int = 0
    cost_cny: float = 0.0
    attempts: int = 0
    metadata: dict = field(default_factory=dict)


__all__ = ["Task", "TaskResult", "TaskStatus"]
