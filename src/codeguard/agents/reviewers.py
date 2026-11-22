"""Node 3 四个并行视角 Reviewer Agent 执行层。"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..engine.synthesis import finding_fingerprint, normalize_finding_dict
from ..models import PerspectiveReviewOutput, ReviewerMemory, ReviewerRole
from ..prompts.reviewers import reviewer_perspective_prompt
from ..prompts.tools import tool_protocol_block
from ..tools.spec import ToolBox, ToolLimits
from .base import call_json_with_tools

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class _PerspectivePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str = ""
    needs_modification: bool = False
    summary: str = ""
    findings: list[dict[str, Any]] = Field(default_factory=list)
    memory_notes: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


def _diff_touched_lines(diff_text: str) -> dict[str, set[int]]:
    """提取 diff 中实际发生增删的行号窗口，用于增量复审时过滤未变动旧问题。"""
    touched: dict[str, set[int]] = {}
    cur_path = ""
    new_line = 0
    for raw in (diff_text or "").splitlines():
        if raw.startswith("+++ "):
            name = raw[4:].strip()
            cur_path = name[2:] if name.startswith("b/") else name
            touched.setdefault(cur_path, set())
            continue
        if raw.startswith("diff --git "):
            cur_path = ""
            continue
        m = _HUNK_RE.match(raw)
        if m:
            new_line = int(m.group(1))
            continue
        if not cur_path:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            touched[cur_path].update(range(max(1, new_line - 2), new_line + 3))
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            touched[cur_path].update(range(max(1, new_line - 2), new_line + 3))
        elif raw.startswith(" "):
            new_line += 1
    return touched


def _is_suppressed_by_memory(
    item: dict[str, Any],
    *,
    memory: ReviewerMemory,
    round_mode: str,
    touched_lines: dict[str, set[int]],
) -> bool:
    fp = finding_fingerprint(item)
    if fp in memory.dismissed_fingerprints:
        return True
    if round_mode == "incremental":
        path = str(item.get("path") or "")
        line = int(item.get("line") or 0)
        lines_in_file = touched_lines.get(path)
        # 增量复审中，若目标文件/行号未在本轮 fix_patch 改动范围内且记忆已提过，视为重复噪声
        if fp in memory.raised_fingerprints or fp in memory.resolved_fingerprints:
            if not lines_in_file or (line > 0 and line not in lines_in_file):
                return True
    return False


def run_perspective_review(
    ctx: Any,
    *,
    role: ReviewerRole,
    pr_brief: dict[str, Any],
    diff_text: str,
    repo_context: str,
    guidance: str,
    deterministic_findings: list[dict[str, Any]],
    memory: ReviewerMemory,
    round_no: int,
    round_mode: str,
    tools: ToolBox | None = None,
    limits: ToolLimits | None = None,
    tier: str = "T1",
) -> tuple[PerspectiveReviewOutput, list[dict], list[dict]]:
    """运行单个专业视角的 Reviewer Agent，并结合短期记忆过滤重复问题。"""
    limits = limits or ToolLimits()
    tools_block = tool_protocol_block(tools.specs(), limits) if tools else ""
    prompt = reviewer_perspective_prompt(
        role=role,
        pr_brief=pr_brief,
        diff_text=diff_text,
        repo_context=repo_context,
        guidance=guidance,
        deterministic_findings=deterministic_findings,
        memory=memory,
        round_no=round_no,
        round_mode=round_mode,
        tools_block=tools_block,
    )
    payload, costs, events = call_json_with_tools(
        ctx,
        role,
        prompt,
        _PerspectivePayload,
        tools=tools,
        limits=limits,
        temperature=0.1,
        tier=tier,
    )

    touched = _diff_touched_lines(diff_text) if round_mode == "incremental" else {}
    filtered_findings: list[dict[str, Any]] = []
    for raw in payload.findings:
        if not isinstance(raw, dict):
            continue
        norm = normalize_finding_dict(raw, role=role)
        if _is_suppressed_by_memory(
            norm,
            memory=memory,
            round_mode=round_mode,
            touched_lines=touched,
        ):
            continue
        filtered_findings.append(norm)

    needs_mod = bool(payload.needs_modification) and bool(filtered_findings)
    if any(item.get("blocking") for item in filtered_findings):
        needs_mod = True

    output = PerspectiveReviewOutput(
        role=role,
        needs_modification=needs_mod,
        summary=payload.summary.strip(),
        findings=filtered_findings,
        memory_notes=[str(n).strip() for n in payload.memory_notes if str(n).strip()][:10],
        confidence=payload.confidence,
    )
    return output, costs, events


__all__ = ["run_perspective_review"]
