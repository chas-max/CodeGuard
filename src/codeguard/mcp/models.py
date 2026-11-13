"""MCP Client 数据模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class McpToolInfo(BaseModel):
    """从 MCP Server 发现的单个工具元信息。"""

    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class McpCallResult(BaseModel):
    """MCP `tools/call` 调用结果。"""

    ok: bool
    server: str
    tool: str
    content: Any = None
    error: str = ""
    duration_ms: int = 0


class McpServerStatus(BaseModel):
    """MCP Server 健康探测与能力矩阵视图。"""

    name: str
    enabled: bool
    transport: str
    ok: bool
    tools_count: int = 0
    tools: list[str] = Field(default_factory=list)
    error: str = ""
    capability: str = "read"
    allowed_roles: list[str] = Field(default_factory=list)
    latency_ms: int = 0
