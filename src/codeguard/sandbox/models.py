"""沙箱执行规格与结果。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..models import SandboxStatus


@dataclass(frozen=True)
class SandboxSpec:
    job_id: str
    workspace: Path
    image: str
    commands: list[str]
    timeout_sec: int = 300
    network: Literal["none", "prepare"] = "none"
    env: dict[str, str] = field(default_factory=dict)
    memory: str = "1g"
    cpus: float = 1.0
    pids_limit: int = 256

    def __post_init__(self):
        if not self.commands:
            raise ValueError("SandboxSpec.commands 不能为空")
        if self.network not in ("none", "prepare"):
            raise ValueError("network 只允许 none/prepare")
        if self.timeout_sec <= 0:
            raise ValueError("timeout_sec 必须为正")
        if self.network == "none" and self.env.get("CG_ALLOW_NETWORK") == "1":
            raise ValueError("测试阶段禁止网络")


@dataclass
class SandboxResult:
    status: SandboxStatus
    exit_code: int = 0
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    duration_sec: float = 0.0
    tree_hash: str = ""
    container_id: str = ""
    infra_error: str = ""


__all__ = ["SandboxSpec", "SandboxResult"]
