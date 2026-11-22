"""Node 2 (pr_reader) Agent：结构化理解 PR 并执行单调风险等级定级。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..models import (
    CodePartition,
    ImportanceLevel,
    PRBrief,
    max_importance,
    normalize_importance,
)
from ..prompts.pr_reader import pr_reader_prompt
from ..prompts.tools import tool_protocol_block
from ..tools.spec import ToolBox, ToolLimits
from .base import call_json_with_tools


class _PRReaderPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    problem_summary: str = ""
    change_intent: str = ""
    before_behavior: str = ""
    after_behavior: str = ""
    background_context: str = ""
    llm_suggested_level: str = "medium"
    escalation_reasons: list[str] = Field(default_factory=list)
    affected_modules: list[str] = Field(default_factory=list)
    risk_highlights: list[str] = Field(default_factory=list)


def read_pr_context(
    ctx: Any,
    *,
    pr_context: dict[str, Any],
    code_partition: CodePartition | dict[str, Any],
    diff_text: str,
    repo_map: dict[str, Any],
    guidance: str,
    deterministic_level: ImportanceLevel,
    deterministic_reasons: list[str],
    tools: ToolBox | None = None,
    limits: ToolLimits | None = None,
) -> tuple[PRBrief, list[dict], list[dict]]:
    """调用 pr_reader Agent 生成 PRBrief，并强制保证 final_level >= deterministic_level。"""
    limits = limits or ToolLimits()
    part_obj = (
        code_partition
        if isinstance(code_partition, CodePartition)
        else CodePartition.model_validate(code_partition or {})
    )
    repo_map_summary = {
        "total_files": len(repo_map.get("files") or []),
        "test_files_count": len(repo_map.get("test_files") or []),
        "manifests": (repo_map.get("manifests") or [])[:10],
        "languages": repo_map.get("languages") or {},
    }
    tools_block = tool_protocol_block(tools.specs(), limits) if tools else ""
    prompt = pr_reader_prompt(
        pr_context=pr_context,
        code_partition=part_obj.model_dump(mode="json"),
        diff_text=diff_text,
        repo_map_summary=repo_map_summary,
        guidance=guidance,
        deterministic_level=deterministic_level,
        deterministic_reasons=deterministic_reasons,
        tools_block=tools_block,
    )
    payload, costs, events = call_json_with_tools(
        ctx,
        "pr_reader",
        prompt,
        _PRReaderPayload,
        tools=tools,
        limits=limits,
        temperature=0.1,
    )
    suggested = normalize_importance(payload.llm_suggested_level, default=deterministic_level)
    final_level = max_importance(deterministic_level, suggested)
    reasons = list(deterministic_reasons)
    for item in payload.escalation_reasons:
        text = str(item).strip()
        if text and text not in reasons:
            reasons.append(text)

    brief = PRBrief(
        problem_summary=payload.problem_summary.strip() or str(pr_context.get("title") or ""),
        change_intent=payload.change_intent.strip() or "change",
        before_behavior=payload.before_behavior.strip(),
        after_behavior=payload.after_behavior.strip(),
        background_context=payload.background_context.strip(),
        code_partition=part_obj,
        deterministic_level=deterministic_level,
        llm_suggested_level=suggested,
        final_level=final_level,
        escalation_reasons=reasons,
        affected_modules=[str(m) for m in payload.affected_modules[:30]],
        risk_highlights=[str(r) for r in payload.risk_highlights[:20]],
    )
    return brief, costs, events


__all__ = ["read_pr_context"]
