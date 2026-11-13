"""Agent 工具契约：工具描述、参数模型、结果、限额与工具箱协议。

工具层是 L2 Agent 与 L4 执行之间的受控边界：Agent 只能看到注册进 ToolBox 的
工具，且每个工具都声明副作用等级（read / exec / write）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

Capability = Literal["none", "read", "exec", "write"]
ToolCapability = Literal["read", "exec", "write"]
ToolStatus = Literal["ok", "invalid_args", "denied", "failed", "infra_error"]

# 能力序关系：none < read < exec < write。生效能力取所有上限的最小值。
CAPABILITY_ORDER: dict[str, int] = {"none": 0, "read": 1, "exec": 2, "write": 3}


def capability_rank(capability: str) -> int:
    return CAPABILITY_ORDER.get(str(capability or "none"), 0)


def min_capability(*capabilities: str) -> Capability:
    """取最小能力；空列表按 none 处理（fail closed）。"""
    if not capabilities:
        return "none"
    winner = min(capabilities, key=capability_rank)
    return winner if winner in CAPABILITY_ORDER else "none"  # type: ignore[return-value]


def capability_allows(granted: str, required: str) -> bool:
    return capability_rank(granted) >= capability_rank(required)


@dataclass(frozen=True)
class ToolSpec:
    """单个工具的静态描述；args_model 用于参数校验与提示词 schema 生成。"""

    name: str
    description: str
    args_model: type[BaseModel]
    capability: ToolCapability = "read"
    timeout_sec: int = 30
    source: Literal["builtin", "mcp"] = "builtin"
    server: str = ""
    raw_schema: dict[str, Any] | None = None

    def to_openai_tool(self) -> dict[str, Any]:
        """转换为 OpenAI / LangChain `bind_tools` 兼容的函数声明。"""
        schema = dict(self.raw_schema) if self.raw_schema else self.args_model.model_json_schema()
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    status: ToolStatus = "ok"
    content: str = ""
    error: str = ""
    output_chars: int = 0
    duration_ms: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "content": self.content,
            "error": self.error,
            "output_chars": self.output_chars,
            "duration_ms": self.duration_ms,
            "meta": dict(self.meta),
        }


@dataclass(frozen=True)
class ToolLimits:
    """单次 Agent 工具循环的硬上限。"""

    max_steps: int = 6
    max_tool_calls: int = 6
    max_wall_sec: int = 120
    max_output_chars: int = 8000
    sandbox_timeout_sec: int = 120
    max_cost_cny: float = 0.0  # 0 = 不单独熔断，沿用 job 级预算
    protocol: str = "auto"  # auto / native / json


@runtime_checkable
class ToolBox(Protocol):
    role: str
    capability: str

    def specs(self) -> list[ToolSpec]: ...

    def dispatch(self, name: str, args: dict[str, Any]) -> ToolResult: ...

    @property
    def events(self) -> list[dict]: ...


__all__ = [
    "Capability", "ToolCapability", "ToolStatus", "CAPABILITY_ORDER",
    "capability_rank", "min_capability", "capability_allows",
    "ToolSpec", "ToolCall", "ToolResult", "ToolLimits", "ToolBox",
]
