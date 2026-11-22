"""Node 7 (summary) Agent 与程序化记忆提炼函数。"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..models import (
    IMPORTANCE_RANK,
    ImportanceLevel,
    ProceduralMemoryRecord,
    ReviewSummary,
    max_importance,
    normalize_importance,
)
from ..prompts.summary import summary_prompt
from .base import call_json


class _SummaryPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    headline: str = ""
    changes_made: list[str] = Field(default_factory=list)
    verification_evidence: list[str] = Field(default_factory=list)
    residual_risks: list[str] = Field(default_factory=list)
    markdown_body: str = ""


def _deterministic_summary(
    *,
    pr_brief: dict[str, Any],
    findings: list[dict[str, Any]],
    fix_history: list[dict[str, Any]],
    validation_report: dict[str, Any] | None,
    inherited_level: ImportanceLevel,
    effective_level: ImportanceLevel,
    escalation_reason: str,
) -> ReviewSummary:
    open_blockers = [
        f for f in findings
        if f.get("lifecycle", "open") == "open" and f.get("blocking")
    ]
    resolved = [f for f in findings if f.get("lifecycle") == "resolved"]
    changes = [
        f"第 {item.get('round')} 轮修复 {', '.join(item.get('files') or [])}: "
        f"{str(item.get('explanation') or '')[:140]}"
        for item in fix_history
        if item.get("files")
    ]
    evidence: list[str] = []
    if validation_report:
        evidence.append(
            f"Docker Verify Gate: status={validation_report.get('status')}, "
            f"regression_passed={validation_report.get('regression_passed')}"
        )
    if resolved:
        evidence.append(f"已解决历史阻塞/问题项 {len(resolved)} 条")
    residual = [
        f"[{f.get('severity')}] {f.get('path') or 'global'}:{f.get('line', 0)} {f.get('message', '')}"
        for f in open_blockers[:10]
    ]
    if escalation_reason:
        residual.insert(0, escalation_reason)

    title = str(pr_brief.get("problem_summary") or "PR 审查与修复更新")
    headline = f"[{effective_level.upper()}] {title[:100]}"
    lines = [
        f"### {headline}",
        f"- 继承重要性等级：`{inherited_level}` → 当前生效等级：`{effective_level}`",
        f"- 打开阻塞项：`{len(open_blockers)}` · 已修复项：`{len(resolved)}`",
    ]
    if changes:
        lines.append("\n#### 修复摘要\n" + "\n".join(f"- {c}" for c in changes))
    if evidence:
        lines.append("\n#### 验证证据\n" + "\n".join(f"- {e}" for e in evidence))
    if residual:
        lines.append("\n#### 残余风险与人工关注点\n" + "\n".join(f"- {r}" for r in residual))

    return ReviewSummary(
        headline=headline,
        inherited_level=inherited_level,
        effective_level=effective_level,
        level_escalated=IMPORTANCE_RANK[effective_level] > IMPORTANCE_RANK[inherited_level],
        changes_made=changes,
        verification_evidence=evidence,
        residual_risks=residual,
        markdown_body="\n".join(lines),
    )


def generate_review_summary(
    ctx: Any,
    *,
    pr_brief: dict[str, Any],
    findings: list[dict[str, Any]],
    fix_history: list[dict[str, Any]],
    qa_history: list[dict[str, Any]],
    validation_report: dict[str, Any] | None,
    inherited_level: ImportanceLevel,
    effective_level: ImportanceLevel,
    escalation_reason: str = "",
) -> tuple[ReviewSummary, list[dict]]:
    """生成结构化 PR Summary；当 LLM 不可用或配置未覆盖 summary 时确定性兜底。"""
    merged_level = max_importance(inherited_level, effective_level)
    try:
        llm = ctx.llm("summary")
        if hasattr(llm, "_queue") and not llm._queue:
            raise RuntimeError("summary mock queue empty")
    except Exception:
        return (
            _deterministic_summary(
                pr_brief=pr_brief,
                findings=findings,
                fix_history=fix_history,
                validation_report=validation_report,
                inherited_level=inherited_level,
                effective_level=merged_level,
                escalation_reason=escalation_reason,
            ),
            [],
        )

    prompt = summary_prompt(
        pr_brief=pr_brief,
        findings=findings,
        fix_history=fix_history,
        qa_history=qa_history,
        validation_report=validation_report,
        inherited_level=inherited_level,
        effective_level=merged_level,
        escalation_reason=escalation_reason,
    )
    try:
        payload, costs = call_json(ctx, "summary", prompt, _SummaryPayload, temperature=0.1)
        summary = ReviewSummary(
            headline=payload.headline.strip() or f"[{merged_level.upper()}] PR 审查更新",
            inherited_level=inherited_level,
            effective_level=merged_level,
            level_escalated=IMPORTANCE_RANK[merged_level] > IMPORTANCE_RANK[inherited_level],
            changes_made=[str(c) for c in payload.changes_made],
            verification_evidence=[str(v) for v in payload.verification_evidence],
            residual_risks=[str(r) for r in payload.residual_risks],
            markdown_body=payload.markdown_body.strip(),
        )
        if not summary.markdown_body:
            fallback = _deterministic_summary(
                pr_brief=pr_brief,
                findings=findings,
                fix_history=fix_history,
                validation_report=validation_report,
                inherited_level=inherited_level,
                effective_level=merged_level,
                escalation_reason=escalation_reason,
            )
            summary = summary.model_copy(update={"markdown_body": fallback.markdown_body})
        return summary, costs
    except Exception:
        return (
            _deterministic_summary(
                pr_brief=pr_brief,
                findings=findings,
                fix_history=fix_history,
                validation_report=validation_report,
                inherited_level=inherited_level,
                effective_level=merged_level,
                escalation_reason=escalation_reason,
            ),
            [],
        )


def extract_procedural_memory(
    state: dict[str, Any],
    *,
    final_outcome: str,
) -> ProceduralMemoryRecord | None:
    """在终态收敛后，结合 Reviewer + Coder + QA + Verify Gate + 人工审批真实结果提炼程序化记忆。"""
    findings = list(state.get("findings") or [])
    fix_history = list(state.get("fix_history") or [])
    fix_patch = str(state.get("fix_patch") or "")
    if not findings and not fix_history and not fix_patch:
        return None

    reviewer_roles = sorted({
        str(r)
        for item in findings
        for r in (item.get("reported_by") or ([item.get("group")] if item.get("group") else []))
        if r
    })
    categories = sorted({
        str(item.get("category"))
        for item in findings
        if item.get("category")
    })
    file_patterns = sorted({
        str(p)
        for p in (
            [item.get("path") for item in findings if item.get("path")]
            + list(state.get("fix_files") or [])
        )
        if p
    })[:20]

    problems = [
        f"[{item.get('severity', 'minor')}] {item.get('path') or 'global'}:{item.get('line', 0)} "
        f"{str(item.get('message') or '')[:180]}"
        for item in findings[:10]
    ]
    problem_pattern = "；".join(problems) or str((state.get("pr_brief") or {}).get("problem_summary") or "")

    fix_explanations = [
        str(item.get("explanation") or "")[:240]
        for item in fix_history
        if item.get("explanation")
    ]
    fix_strategy = " | ".join(fix_explanations)
    patch_summary = fix_patch[:1500]

    validation = state.get("validation_report") or {}
    ci_passed = validation.get("status") == "passed"
    qa_report = state.get("qa_report") or {}
    qa_verdict = str(qa_report.get("verdict") or "")
    human_decision = state.get("human_decision")
    human_approved: bool | None = None
    if isinstance(human_decision, dict) and "approved" in human_decision:
        human_approved = bool(human_decision.get("approved"))

    has_fix = bool(fix_history or fix_patch)
    if has_fix:
        if ci_passed and qa_verdict in ("", "pass") and human_approved is not False and final_outcome in (
            "MERGED", "MERGE_READY", "BLOCKED_HUMAN",
        ):
            memory_type = "fix_pattern"
        else:
            memory_type = "anti_pattern"
    else:
        memory_type = "review_lesson"

    level = normalize_importance(
        state.get("importance_level") or (state.get("risk_tier") or {}).get("tier"),
        default="medium",
    )
    content_text = json.dumps(
        {
            "repo": state.get("repo", ""),
            "memory_type": memory_type,
            "categories": categories,
            "file_patterns": file_patterns,
            "problem_pattern": problem_pattern,
            "fix_strategy": fix_strategy,
            "patch_summary": patch_summary[:600],
            "qa_verdict": qa_verdict,
            "ci_passed": ci_passed,
            "human_approved": human_approved,
            "final_outcome": final_outcome,
        },
        ensure_ascii=False,
    )
    return ProceduralMemoryRecord(
        repo=str(state.get("repo") or ""),
        pr_number=int(state.get("pr_number") or 0),
        importance_level=level,
        reviewer_roles=reviewer_roles,
        categories=categories,
        file_patterns=file_patterns,
        problem_pattern=problem_pattern[:2000],
        fix_strategy=fix_strategy[:2000],
        patch_summary=patch_summary,
        qa_verdict=qa_verdict,
        ci_passed=ci_passed,
        human_approved=human_approved,
        final_outcome=final_outcome,
        memory_type=memory_type,  # type: ignore[arg-type]
        content_text=content_text,
    )


__all__ = ["generate_review_summary", "extract_procedural_memory"]
