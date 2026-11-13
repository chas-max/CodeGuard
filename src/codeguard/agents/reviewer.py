"""唯一审查 Agent：对当前候选代码做最终整体审查。"""
from __future__ import annotations

import re

from ..models import Finding, FindingSource, ReviewDecision, Severity
from ..prompts.reviewer import reviewer_prompt
from ..prompts.tools import tool_protocol_block
from ..tools.spec import ToolBox, ToolLimits
from .base import call_json_with_tools

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_SEVERITY_RANK = {"critical": 3, "major": 2, "minor": 1, "info": 0}


def diff_added_lines(diff_text: str) -> dict[str, set[int]]:
    """解析 unified diff 的新增行号，用于判断 finding 是否由本次 PR 引入。"""
    result: dict[str, set[int]] = {}
    path = ""
    new_line = 0
    for line in (diff_text or "").splitlines():
        if line.startswith("+++ "):
            raw = line[4:].strip()
            path = raw[2:] if raw.startswith("b/") else raw
            result.setdefault(path, set())
            continue
        if line.startswith("diff --git"):
            path = ""
            continue
        match = _HUNK_RE.match(line)
        if match:
            new_line = int(match.group(1))
            continue
        if not path:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            result[path].add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif line.startswith(" "):
            new_line += 1
    return result


def _introduced(path: str, line: int, added: dict[str, set[int]], changed_paths: set[str]) -> bool:
    if path and line and path in added:
        return line in added[path]
    return bool(path) and path in changed_paths


def _cap_nits(findings: list[Finding], max_nits: int) -> list[Finding]:
    blocking = [f for f in findings if f.blocking]
    advisory = [f for f in findings if not f.blocking]
    advisory.sort(key=lambda f: _SEVERITY_RANK.get(f.severity.value, 0), reverse=True)
    return blocking + advisory[: max(0, max_nits)]


def review(
    ctx,
    *,
    claim: dict,
    hybrid_report: dict,
    validation_report: dict | None,
    diff_text: str,
    fix_note: str = "",
    profile: str = "change",
    risk_tier: dict | None = None,
    intent: dict | None = None,
    guidance: str = "",
    round_mode: str = "full",
    max_nits: int = 5,
    open_findings: list[dict] | None = None,
    changed_paths: list[str] | None = None,
    tools: ToolBox | None = None,
    limits: ToolLimits | None = None,
) -> tuple[ReviewDecision, list[dict], list[dict]]:
    limits = limits or ToolLimits()
    tools_block = tool_protocol_block(tools.specs(), limits) if tools else ""
    prompt = reviewer_prompt(
        claim=claim,
        hybrid_report=hybrid_report,
        validation_report=validation_report,
        diff_text=diff_text,
        fix_note=fix_note,
        profile=profile,
        risk_tier=risk_tier,
        intent=intent,
        guidance=guidance,
        round_mode=round_mode,
        max_nits=max_nits,
        open_findings=open_findings,
        tools_block=tools_block,
    )
    decision, costs, tool_events = call_json_with_tools(
        ctx, "reviewer", prompt, ReviewDecision,
        tools=tools, limits=limits, temperature=0.1,
    )

    changed = {
        str(item.get("path", "")) for item in (open_findings or []) if item.get("path")
    } | {path for path in (changed_paths or []) if path}
    added = diff_added_lines(diff_text)
    normalized: list[Finding] = []
    for finding in decision.findings:
        severity = finding.severity
        blocking = bool(finding.blocking) and severity in (Severity.critical, Severity.major)
        demoted = ""
        if finding.category.value == "style" and blocking:
            blocking, demoted = False, "style"
        elif finding.blocking and not blocking:
            demoted = "severity"
        normalized.append(finding.model_copy(update={
            "source": FindingSource.semantic,
            "blocking": blocking,
            "evidence": finding.evidence or finding.message or "reviewer finding",
            "introduced_by_pr": finding.introduced_by_pr or _introduced(
                finding.path, finding.line, added, changed,
            ),
            "demoted_by": finding.demoted_by or demoted,
        }))
    normalized = _cap_nits(normalized, max_nits)
    decision = decision.model_copy(update={"findings": normalized})
    return decision, costs, tool_events


__all__ = ["review", "diff_added_lines"]
