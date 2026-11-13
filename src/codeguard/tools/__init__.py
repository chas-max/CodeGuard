"""Agent 取控工具层：只读检索 + 沙箱执行 + 仅 fixer 可写。"""
from __future__ import annotations

from .registry import (
    AgentToolBox,
    build_toolbox,
    effective_capability,
    limits_from_policy,
    role_group,
    role_tools,
)
from .spec import (
    CAPABILITY_ORDER,
    ToolCall,
    ToolLimits,
    ToolResult,
    ToolSpec,
    capability_allows,
    min_capability,
)

__all__ = [
    "AgentToolBox", "build_toolbox", "effective_capability", "limits_from_policy",
    "role_group", "role_tools",
    "CAPABILITY_ORDER", "ToolCall", "ToolLimits", "ToolResult", "ToolSpec",
    "capability_allows", "min_capability",
]
