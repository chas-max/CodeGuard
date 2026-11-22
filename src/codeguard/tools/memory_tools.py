"""RAG 工具：供 Coder Agent（及可选 Reviewer）检索本地程序化记忆库。"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .base import error_result, ok_result, truncate
from .spec import ToolResult


class SearchProceduralMemoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=1000)
    category: str = Field(default="", max_length=64)
    repo_only: bool = False
    verified_only: bool = True
    top_k: int = Field(default=3, ge=1, le=10)


def search_procedural_memory(
    store: Any,
    args: SearchProceduralMemoryArgs,
    *,
    current_repo: str = "",
    max_chars: int = 8000,
) -> ToolResult:
    """检索本地向量记忆库中的历史问题修复策略与反面教训（只读工具）。"""
    if store is None:
        return ok_result(
            "本地程序化记忆库未启用或暂无记录。",
            meta={"hits": 0, "memory_ids": []},
        )
    try:
        hits = store.search(
            args.query,
            repo=current_repo if args.repo_only else "",
            current_repo=current_repo,
            category=args.category,
            verified_only=args.verified_only,
            top_k=args.top_k,
        )
    except Exception as exc:  # noqa: BLE001
        return error_result("failed", f"程序化记忆检索失败: {exc}")

    if not hits:
        return ok_result(
            "未命中相关的历史程序化记忆（可按报错根因或模块关键字调整 query，或设 verified_only=false 查看反面教训）。",
            meta={"hits": 0, "memory_ids": []},
        )

    payload = json.dumps(hits, ensure_ascii=False, indent=2)
    content, truncated = truncate(payload, max_chars)
    return ok_result(
        content,
        meta={
            "hits": len(hits),
            "memory_ids": [item["memory_id"] for item in hits],
            "truncated": truncated,
        },
    )


__all__ = ["SearchProceduralMemoryArgs", "search_procedural_memory"]
