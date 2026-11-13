"""确定性合并门控。"""
from __future__ import annotations

from ..engine.policy import Policy
from ..models import (
    MergeMode,
    Outcome,
    ReviewDecision,
    ReviewVerdict,
    SandboxStatus,
    ValidationReport,
)


def valid_approvals(
    approvals: list[dict] | None,
    *,
    head_sha: str = "",
    current_tree_hash: str = "",
) -> list[dict]:
    """一次审批只覆盖一个 commit：sha / tree_hash 不匹配即作废；同一人只计一次。"""
    seen: set[str] = set()
    result: list[dict] = []
    for item in approvals or []:
        if not isinstance(item, dict):
            continue
        who = str(item.get("approver", "")).strip().lower()
        if not who or who in seen:
            continue
        sha = str(item.get("sha", "") or "")
        tree = str(item.get("tree_hash", "") or "")
        if sha and head_sha and sha != head_sha:
            continue
        if tree and current_tree_hash and tree != current_tree_hash:
            continue
        seen.add(who)
        result.append(item)
    return result


def merge_blockers(
    *,
    pr_open: bool,
    is_draft: bool,
    mergeable: bool,
    head_sha: str,
    reviewed_sha: str,
    validation: ValidationReport | None,
    review: ReviewDecision | None,
    changed_paths: list[str],
    policy: Policy,
    merge_mode: MergeMode,
    high_risk_approved: bool = False,
    reviewed_tree_hash: str = "",
    current_tree_hash: str = "",
    approvals: list[dict] | None = None,
    min_approvals: int = 0,
    require_non_author: bool = False,
    author: str = "",
    evidence_ok: bool = True,
    tier: str = "",
    blocking_open: int = 0,
) -> list[str]:
    blockers: list[str] = []
    if not pr_open:
        blockers.append("PR 不是 open 状态")
    if is_draft:
        blockers.append("PR 仍是 draft")
    if not mergeable:
        blockers.append("PR 不可合并（冲突/base 变化）")
    if not head_sha or head_sha != reviewed_sha:
        blockers.append(f"head SHA 已变化: reviewed={reviewed_sha[:8]} current={head_sha[:8]}")
    if reviewed_tree_hash and current_tree_hash and reviewed_tree_hash != current_tree_hash:
        blockers.append("审查树与当前工作区树不一致")
    if validation is None or validation.status != SandboxStatus.passed:
        blockers.append("Docker 验证未通过或缺失")
    if review is None or review.verdict != ReviewVerdict.approve:
        blockers.append("审查 Agent 未 approve")
    if review is not None and any(
        f.blocking and f.severity.value in ("critical", "major") for f in review.findings
    ):
        blockers.append("审查仍有 blocking finding")
    if blocking_open:
        blockers.append(f"仍有 {blocking_open} 条未解决的 blocking finding")
    if not changed_paths:
        blockers.append("没有可合并的变更文件")
    if not evidence_ok:
        blockers.append("问题/意图证据不足，不能合并")

    if tier:
        # tier 感知：高风险路径由 tier 规则兜底，仍需至少 2 人。
        high_risk = [p for p in changed_paths if policy.is_high_risk(p)]
        required = max(min_approvals, 2 if high_risk else 0)
        valid = valid_approvals(
            approvals, head_sha=head_sha, current_tree_hash=current_tree_hash,
        )
        if high_risk and min_approvals < 2:
            blockers.append("包含高风险路径，需要 2 名人工批准: " + ", ".join(high_risk[:5]))
        if required:
            if len(valid) < required:
                blockers.append(
                    f"需要 {required} 名人工批准（当前有效 {len(valid)} 名）"
                )
            if require_non_author:
                approvers = {str(a.get("approver", "")).strip().lower() for a in valid}
                author_key = author.strip().lower()
                if author_key and not (approvers - {author_key}):
                    blockers.append("需要至少一名非作者人工批准")
    else:
        high_risk = [p for p in changed_paths if policy.is_high_risk(p)]
        if high_risk and not high_risk_approved:
            blockers.append("包含高风险路径，需要人工批准: " + ", ".join(high_risk[:5]))

    if merge_mode == MergeMode.human and not high_risk_approved:
        blockers.append("MERGE_MODE=human，需要人工批准")
    return blockers


def outcome_for_blockers(blockers: list[str], *, infra: bool = False) -> Outcome:
    if infra:
        return Outcome.infra_failed
    if any("SHA" in b or b.startswith("stale") for b in blockers):
        return Outcome.stale
    if any("人工" in b or "MERGE_MODE" in b for b in blockers):
        return Outcome.blocked_human
    return Outcome.changes_requested


__all__ = ["merge_blockers", "outcome_for_blockers", "valid_approvals"]
