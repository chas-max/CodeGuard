"""CodeGuard v2 LangGraph 确定性条件路由表（实现 5 轮双层闭环与快速短路）。"""
from __future__ import annotations

from typing import Any

from ..github.merge import valid_approvals
from ..models import Outcome, SandboxStatus
from ..state import ReviewState
from ._helpers import safe_tree_hash

_FIN = "persist_memory_and_finalize"


def route_after_prepare(state: ReviewState, ctx: Any) -> str:
    del ctx
    if state.get("budget_exceeded"):
        return _FIN
    if state.get("status") in ("failed", "skipped") or state.get("outcome"):
        return _FIN
    return "pr_reader"


def route_after_pr_reader(state: ReviewState, ctx: Any) -> str:
    del ctx
    if state.get("budget_exceeded"):
        return _FIN
    if state.get("status") == "failed" or state.get("outcome"):
        return _FIN
    return "dispatch_reviewers"


def route_after_synthesize(state: ReviewState, ctx: Any) -> str:
    if state.get("budget_exceeded") or state.get("status") == "failed" or state.get("outcome"):
        return _FIN

    directive = state.get("fix_directive") or {}
    non_fixable = directive.get("non_fixable_blockers") or []
    if non_fixable:
        # 存在无法自动修复的策略/敏感密钥/删测阻塞项，直接提交结论拦截
        return "summary_and_submit"

    max_fix = int(getattr(ctx.cfg, "max_fix_rounds", 5) or 5)
    max_rev = int(getattr(ctx.cfg, "max_review_rounds", 5) or 5)
    cur_fix = int(state.get("fix_round", 0) or 0)
    cur_rev = int(state.get("review_round", 0) or 0)

    if directive.get("needs_modification"):
        if state.get("escalation_reason") or cur_fix >= max_fix or (cur_rev >= max_rev and cur_fix > 0):
            return "summary_and_submit"
        return "coder_agent"

    # 无需修改：若当前代码树尚未过 verify_gate（如首轮干净 PR），则走 verify_gate；
    # 若复审确认且刚刚在 verify_gate 通过（verified_tree_hash == current_hash），直接短路到 summary_and_submit。
    current_hash = safe_tree_hash(state)
    verified_hash = str(state.get("verified_tree_hash") or "")
    if verified_hash and current_hash and verified_hash == current_hash:
        return "summary_and_submit"
    return "verify_gate"


def route_after_coder(state: ReviewState, ctx: Any) -> str:
    del ctx
    if state.get("budget_exceeded"):
        return _FIN
    if state.get("status") == "fixed" and state.get("fix_patch"):
        return "qa_checker"
    return "summary_and_submit"


def route_after_qa(state: ReviewState, ctx: Any) -> str:
    if state.get("budget_exceeded") or state.get("status") == "qa_failed" or state.get("outcome"):
        return _FIN
    verdict = str((state.get("qa_report") or {}).get("verdict") or "")
    max_fix = int(getattr(ctx.cfg, "max_fix_rounds", 5) or 5)
    cur_fix = int(state.get("fix_round", 0) or 0)
    if verdict == "pass":
        return "verify_gate"
    if verdict == "revise" and cur_fix < max_fix:
        return "coder_agent"
    return "summary_and_submit"


def route_after_verify_gate(state: ReviewState, ctx: Any) -> str:
    if state.get("budget_exceeded"):
        return _FIN
    report = state.get("validation_report") or {}
    status = report.get("status")
    if status == SandboxStatus.infra_error.value:
        return _FIN

    max_fix = int(getattr(ctx.cfg, "max_fix_rounds", 5) or 5)
    cur_fix = int(state.get("fix_round", 0) or 0)
    if status != SandboxStatus.passed.value:
        if cur_fix < max_fix:
            return "coder_agent"
        return "summary_and_submit"

    # 验证通过：若本次候选代码来自 Coder 修复且尚未被 Reviewer 复审过，在轮次上限内回流到 4 路并行 Reviewer
    max_rev = int(getattr(ctx.cfg, "max_review_rounds", 5) or 5)
    cur_rev = int(state.get("review_round", 0) or 0)
    current_hash = safe_tree_hash(state)
    reviewed_hash = str(state.get("reviewed_tree_hash") or "")
    if (
        state.get("candidate") == "fix"
        and cur_rev < max_rev
        and (not reviewed_hash or reviewed_hash != current_hash)
    ):
        return "dispatch_reviewers"
    return "summary_and_submit"


def route_after_summary(state: ReviewState, ctx: Any) -> str:
    del ctx
    if state.get("budget_exceeded") or state.get("status") == "submit_failed" or state.get("outcome"):
        return _FIN
    return "merge_gate"


def route_after_merge_gate(state: ReviewState, ctx: Any) -> str:
    del ctx
    if state.get("budget_exceeded"):
        return _FIN
    decision = state.get("merge_decision") or {}
    if decision.get("requires_human"):
        return "human_approval"
    if decision.get("outcome") in (Outcome.merged.value, Outcome.merge_ready.value):
        return "merge"
    return _FIN


def route_after_human(state: ReviewState, ctx: Any) -> str:
    del ctx
    human = state.get("human_decision") or {}
    if not human.get("approved"):
        return _FIN
    risk = state.get("risk_tier") or {}
    required = int(risk.get("min_human_approvals", 1))
    approvals = valid_approvals(
        state.get("human_approvals") or [],
        head_sha=state.get("head_sha", ""),
        current_tree_hash=safe_tree_hash(state),
    )
    if len(approvals) < required:
        return "human_approval"
    if risk.get("require_non_author"):
        author = str(state.get("author", "")).strip().lower()
        approvers = {str(item.get("approver", "")).strip().lower() for item in approvals}
        if author and not (approvers - {author}):
            return "human_approval"
    return "merge_gate"


__all__ = [
    "route_after_prepare",
    "route_after_pr_reader",
    "route_after_synthesize",
    "route_after_coder",
    "route_after_qa",
    "route_after_verify_gate",
    "route_after_summary",
    "route_after_merge_gate",
    "route_after_human",
]
