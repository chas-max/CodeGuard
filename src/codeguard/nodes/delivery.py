"""Node 7 (summary_and_submit)、Node 8 (merge_gate / human_approval / merge) 与终态向量记忆落库。"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from langgraph.types import interrupt

from ..agents.summary import extract_procedural_memory, generate_review_summary
from ..engine.tier import classify_tier
from ..github.merge import merge_blockers, valid_approvals
from ..github.report import build_summary, publish_review, write_local_report
from ..models import (
    IMPORTANCE_APPROVALS,
    IMPORTANCE_TO_TIER,
    Approval,
    MergeDecision,
    MergeMode,
    Outcome,
    ReviewDecision,
    RiskTier,
    TIER_TO_IMPORTANCE,
    Tier,
    ValidationReport,
    max_importance,
    normalize_importance,
)
from ..state import ReviewState
from ._helpers import (
    changed_paths_from_state,
    cost_update,
    emit_event,
    load_state_policy,
    now_iso,
    repo_root_path,
    safe_tree_hash,
    update_progress,
)


def summary_and_submit_node(state: ReviewState, ctx: Any) -> dict[str, Any]:
    """Node 7：生成结构化 Evidence Summary 并更新 PR（继承不降级的重要性等级）。"""
    job_id = state.get("job_id", "")
    update_progress(ctx, job_id, "Node 7: 生成结构化审查总结并提交更新...", 86)

    inherited = normalize_importance(state.get("inherited_importance_level"), default="medium")
    current_lvl = normalize_importance(state.get("importance_level"), default=inherited)
    effective_lvl = max_importance(inherited, current_lvl)

    summary, costs = generate_review_summary(
        ctx,
        pr_brief=state.get("pr_brief") or {},
        findings=list(state.get("findings") or []),
        fix_history=list(state.get("fix_history") or []),
        qa_history=list(state.get("qa_history") or []),
        validation_report=state.get("validation_report"),
        inherited_level=inherited,
        effective_level=effective_lvl,
        escalation_reason=str(state.get("escalation_reason") or ""),
    )

    fix_pushed = bool(state.get("fix_pushed"))
    remote_sha = str(state.get("fix_remote_sha") or "")
    head_sha = str(state.get("head_sha") or "")
    reviewed_sha = str(state.get("reviewed_sha") or "")
    submitted_pr_number = state.get("submitted_pr_number")
    fix_files = list(state.get("fix_files") or [])
    qa_verdict = str((state.get("qa_report") or {}).get("verdict") or "")
    val_status = str((state.get("validation_report") or {}).get("status") or "")

    can_submit_fix = (
        state.get("candidate") == "fix"
        and bool(state.get("fix_patch"))
        and val_status == "passed"
        and qa_verdict == "pass"
        and not state.get("dry_run")
        and ctx.github is not None
    )

    if can_submit_fix and getattr(ctx.cfg, "push_fixes", True):
        use_new_pr = bool(state.get("is_fork")) or getattr(ctx.cfg, "submit_mode", "push_commit") == "new_pr"
        repo = str(state.get("repo") or "")
        pr_num = int(state.get("pr_number") or 0)
        if use_new_pr and hasattr(ctx.github, "create_pull_request"):
            try:
                head_branch = f"codeguard/fix-{pr_num}"
                created = ctx.github.create_pull_request(
                    repo,
                    title=summary.headline or f"CodeGuard Fix for #{pr_num}",
                    body=summary.markdown_body,
                    head=head_branch,
                    base=str(state.get("base_ref") or "main"),
                    labels=[f"importance:{effective_lvl}", "codeguard:fix"],
                )
                submitted_pr_number = created.get("number")
                fix_pushed = True
                summary = summary.model_copy(update={
                    "submitted_mode": "new_pr",
                    "submitted_ref": head_branch,
                    "submitted_pr_number": submitted_pr_number,
                })
            except Exception as exc:  # noqa: BLE001
                return {
                    "review_summary": summary.model_dump(mode="json"),
                    "importance_level": effective_lvl,
                    "status": "submit_failed",
                    "error": f"创建更新 PR 失败: {exc}",
                    **cost_update(state, costs, ctx.cfg),
                }
        else:
            root = repo_root_path(state)
            contents = {}
            for rel in fix_files:
                file_path = root / rel
                if file_path.is_file():
                    contents[rel] = file_path.read_text(encoding="utf-8", errors="replace")
            if contents:
                try:
                    branch = (state.get("pr_context") or {}).get("head_ref") or f"codeguard/fix-{pr_num}"
                    pushed_res = ctx.github.push_fix(
                        repo,
                        branch,
                        message=f"CodeGuard: fix review findings (level={effective_lvl})",
                        files=contents,
                        expected_head_sha=head_sha,
                    )
                    remote_sha = str((pushed_res or {}).get("sha") or "")
                except Exception as exc:  # noqa: BLE001
                    return {
                        "review_summary": summary.model_dump(mode="json"),
                        "importance_level": effective_lvl,
                        "status": "submit_failed",
                        "error": f"推送修复提交失败: {exc}",
                        **cost_update(state, costs, ctx.cfg),
                    }
                if not remote_sha:
                    return {
                        "review_summary": summary.model_dump(mode="json"),
                        "importance_level": effective_lvl,
                        "status": "submit_failed",
                        "error": "GitHub 未返回修复提交 SHA",
                        **cost_update(state, costs, ctx.cfg),
                    }
                fix_pushed = True
                head_sha = remote_sha
                reviewed_sha = remote_sha
                summary = summary.model_copy(update={
                    "submitted_mode": "push_commit",
                    "submitted_ref": str(branch),
                    "submitted_pr_number": pr_num,
                    "submitted_sha": remote_sha,
                })

    update: dict[str, Any] = {
        "review_summary": summary.model_dump(mode="json"),
        "importance_level": effective_lvl,
        "fix_pushed": fix_pushed,
        "fix_remote_sha": remote_sha,
        "head_sha": head_sha,
        "reviewed_sha": reviewed_sha,
        "submitted_pr_number": submitted_pr_number,
        "status": "summarized",
        "events": [{"event": "summary_submitted", "level": effective_lvl, "mode": summary.submitted_mode, "timestamp": now_iso()}],
    }
    update.update(cost_update(state, costs, ctx.cfg))
    emit_event(ctx, job_id, "summary_submitted", {"level": effective_lvl, "pushed": fix_pushed})
    return update


def merge_gate_node(state: ReviewState, ctx: Any) -> dict[str, Any]:
    """Node 8a：确定性合并门控（low=0人直接Merge，medium=1人，critical=2人含非作者）。"""
    job_id = state.get("job_id", "")
    update_progress(ctx, job_id, "Node 8: 执行分级合并门控...", 92)

    policy = load_state_policy(state)
    validation = ValidationReport.model_validate(state["validation_report"]) if state.get("validation_report") else None
    review = ReviewDecision.model_validate(state["review_decision"]) if state.get("review_decision") else None
    pr = state.get("pr_context") or {}
    human = state.get("human_decision") or {}
    outcomes_map = dict(state.get("finding_outcomes") or {})
    hard_rules = {"SEC-001", "POL-001", "TEST-001"}
    dismissed_states = {"accepted_risk", "false_positive", "wont_fix"}
    raw_blocking_open = [
        item for item in (state.get("blocking_findings") or [])
        if item.get("lifecycle", "open") == "open" and item.get("blocking")
    ]
    blocking_open = [
        item for item in raw_blocking_open
        if not (
            outcomes_map.get(str(item.get("id") or "")) in dismissed_states
            and str(item.get("rule_id") or "") not in hard_rules
        )
    ]
    changed = list(dict.fromkeys(changed_paths_from_state(state) + list(state.get("fix_files") or [])))
    stats = state.get("diff_stats") or {}
    current_hash = safe_tree_hash(state)

    api_break = any(
        item.get("rule_id") == "API-001" and item.get("blocking")
        for item in (state.get("findings") or [])
    )
    det_risk = classify_tier(
        policy,
        changed_paths=changed,
        changed_lines=int(stats.get("lines", 0)),
        author=str(state.get("author") or ""),
        pr_body=str(state.get("pr_body") or ""),
        labels=list(pr.get("labels") or []),
        api_break=api_break,
    )
    det_level = TIER_TO_IMPORTANCE.get(det_risk.tier.value, "medium")
    final_level = max_importance(
        det_level,
        state.get("inherited_importance_level"),
        state.get("importance_level"),
    )
    mapped_tier = IMPORTANCE_TO_TIER[final_level]
    min_approvals = max(policy.min_approvals(mapped_tier), IMPORTANCE_APPROVALS[final_level])
    if final_level == "low" and policy.merge.allow_zero_human:
        min_approvals = 0
    if state.get("escalation_reason") and min_approvals < 1:
        min_approvals = 1
    require_non_author = (final_level == "critical") or det_risk.require_non_author

    prev_risk = dict(state.get("risk_tier") or {})
    risk = RiskTier(
        tier=Tier(mapped_tier),
        reasons=list(dict.fromkeys(list(prev_risk.get("reasons") or []) + list(det_risk.reasons))),
        min_human_approvals=min_approvals,
        require_non_author=require_non_author,
        depth=policy.depth_for(mapped_tier),
        trusted_author=det_risk.trusted_author,
        agent_authored=det_risk.agent_authored,
    )

    mode = MergeMode(policy.gates.merge_mode)
    if ctx.cfg.merge_mode == MergeMode.human:
        mode = MergeMode.human

    blockers = merge_blockers(
        pr_open=state.get("status") not in ("closed", "skipped"),
        is_draft=bool(pr.get("is_draft")),
        mergeable=bool(pr.get("mergeable", True)),
        head_sha=state.get("head_sha", ""),
        reviewed_sha=state.get("reviewed_sha", ""),
        validation=validation,
        review=review,
        changed_paths=changed,
        policy=policy,
        merge_mode=mode,
        high_risk_approved=bool(human.get("approved")),
        reviewed_tree_hash=state.get("reviewed_tree_hash", ""),
        current_tree_hash=current_hash,
        approvals=state.get("human_approvals") or [],
        min_approvals=risk.min_human_approvals,
        require_non_author=risk.require_non_author,
        author=state.get("author", ""),
        evidence_ok=True,
        tier=risk.tier.value,
        blocking_open=len(blocking_open),
    )

    if state.get("candidate") == "fix":
        qa_verdict = str((state.get("qa_report") or {}).get("verdict") or "")
        if qa_verdict != "pass":
            blockers.append(f"QA Checker 未通过（verdict={qa_verdict or 'none'}）")
        if state.get("fix_patch") and not state.get("fix_pushed"):
            blockers.append("修复补丁未推送到 PR 分支，不能自动合并")
        if (state.get("review_summary") or {}).get("submitted_mode") == "new_pr":
            blockers.append(
                f"修复已提交为关联新 PR #{state.get('submitted_pr_number') or ''}（继承等级 {final_level}），"
                "请在新 PR 上完成审阅与合并"
            )

    if blocking_open:
        blockers.append(
            "仍存在 blocking 检测问题: "
            + ", ".join(sorted({f.get("rule_id") or f.get("id", "") for f in blocking_open})[:5])
        )
    if state.get("escalation_reason") and not human.get("approved"):
        blockers.append(f"需要人工确认: {state['escalation_reason']}")

    new_pr_submitted = (state.get("review_summary") or {}).get("submitted_mode") == "new_pr"
    has_hard_defect = bool(
        blocking_open
        or (validation is None or validation.status.value != "passed")
        or (state.get("candidate") == "fix" and (state.get("qa_report") or {}).get("verdict") != "pass")
        or new_pr_submitted
    )
    requires_human = (not has_hard_defect) and any("人工" in b or "MERGE_MODE" in b for b in blockers)

    if blockers:
        if new_pr_submitted or state.get("escalation_reason") or requires_human:
            outcome = Outcome.blocked_human
        else:
            outcome = Outcome.changes_requested
        decision = MergeDecision(
            outcome=outcome,
            reason="；".join(dict.fromkeys(blockers)),
            requires_human=requires_human,
        )
    else:
        decision = MergeDecision(
            outcome=Outcome.merge_ready,
            reason="分级门控通过",
            requires_human=False,
            actions=["merge"],
        )

    emit_event(ctx, job_id, "merge_gate_done", {
        "outcome": decision.outcome.value,
        "requires_human": decision.requires_human,
        "level": final_level,
        "tier": risk.tier.value,
        "required_approvals": risk.min_human_approvals,
    })
    return {
        "importance_level": final_level,
        "risk_tier": risk.model_dump(mode="json"),
        "merge_decision": decision.model_dump(mode="json"),
        "requires_human": decision.requires_human,
        "status": "merge_ready" if not blockers else "merge_blocked",
    }


def human_approval_node(state: ReviewState, ctx: Any) -> dict[str, Any]:
    """Node 8b：按 importance_level (medium=1, critical=2) 中断收集人工审批。"""
    del ctx
    risk = state.get("risk_tier") or {}
    required = int(risk.get("min_human_approvals", 1))
    approvals = list(state.get("human_approvals") or [])
    decision = interrupt({
        "type": "human_approval",
        "pr": f"{state.get('repo')}#{state.get('pr_number')}",
        "reason": (state.get("merge_decision") or {}).get("reason", ""),
        "importance_level": state.get("importance_level", "medium"),
        "tier": risk.get("tier", "T1"),
        "required_approvals": required,
        "collected_approvals": [item.get("approver") for item in approvals],
        "require_non_author": bool(risk.get("require_non_author")),
        "author": state.get("author", ""),
        "summary": state.get("review_summary"),
        "validation": state.get("validation_report"),
        "diff": state.get("fix_patch") or state.get("diff_text", ""),
    })
    if isinstance(decision, dict):
        approved = bool(decision.get("approved"))
        approver = str(decision.get("approver") or "").strip() or "operator"
        feedback = str(decision.get("feedback", ""))
        outcomes = decision.get("finding_outcomes") or {}
    else:
        approved = str(decision).strip().lower() in ("y", "yes", "approve", "true", "1")
        approver, feedback, outcomes = "operator", "", {}

    merged_outcomes = dict(state.get("finding_outcomes") or {})
    if isinstance(outcomes, dict):
        merged_outcomes.update({str(k): str(v) for k, v in outcomes.items()})
    if approved:
        approvals.append(Approval(
            approver=approver,
            sha=state.get("head_sha", ""),
            tree_hash=safe_tree_hash(state),
            at=now_iso(),
            finding_outcomes={str(k): str(v) for k, v in outcomes.items()} if isinstance(outcomes, dict) else {},
        ).model_dump(mode="json"))
    return {
        "human_approvals": approvals,
        "human_decision": {"approved": approved, "approver": approver, "feedback": feedback},
        "finding_outcomes": merged_outcomes,
        "status": "human_decided",
        "events": [{"event": "human_approval", "approved": approved,
                    "approver": approver, "timestamp": now_iso()}],
    }


def merge_node(state: ReviewState, ctx: Any) -> dict[str, Any]:
    """Node 8c：二次校验审批与哈希一致性后执行 Merge，随后流向记忆持久化收尾。"""
    job_id = state.get("job_id", "")
    update_progress(ctx, job_id, "Node 8: 正在执行 GitHub Merge...", 96)
    if state.get("dry_run"):
        return {
            "status": "done",
            "outcome": Outcome.blocked_human.value,
            "error": "dry-run 模式不执行合并",
            "merge_decision": MergeDecision(
                outcome=Outcome.blocked_human,
                reason="dry-run 模式不执行合并",
                requires_human=True,
            ).model_dump(mode="json"),
        }
    if ctx.github is None:
        return {
            "status": "failed",
            "outcome": Outcome.infra_failed.value,
            "error": "GitHub 客户端未配置",
        }
    risk = state.get("risk_tier") or {}
    required = int(risk.get("min_human_approvals", 0))
    approvals = valid_approvals(
        state.get("human_approvals") or [],
        head_sha=state.get("head_sha", ""),
        current_tree_hash=safe_tree_hash(state),
    )
    if required and len(approvals) < required:
        return {
            "status": "failed",
            "outcome": Outcome.blocked_human.value,
            "error": f"审批不足（{len(approvals)}/{required}），拒绝合并",
            "merge_decision": MergeDecision(
                outcome=Outcome.blocked_human,
                reason="审批不足",
                requires_human=True,
            ).model_dump(mode="json"),
        }
    try:
        result = ctx.github.merge(
            state.get("repo", ""),
            int(state.get("pr_number", 0)),
            sha=state.get("head_sha", ""),
            method=ctx.cfg.merge_method,
            title=f"CodeGuard: {state.get('pr_title', '')}"[:200],
            message="由 CodeGuard v2 自动审查并合并。",
        )
        sha = str((result or {}).get("sha") or "")
        if not sha:
            return {
                "status": "failed",
                "outcome": Outcome.blocked_human.value,
                "error": "GitHub merge API 未返回 merge commit SHA",
                "merge_decision": MergeDecision(
                    outcome=Outcome.blocked_human,
                    reason="merge API 未返回 SHA",
                    requires_human=True,
                ).model_dump(mode="json"),
            }
        return {
            "status": "merged",
            "outcome": Outcome.merged.value,
            "merge_commit_sha": sha,
            "merge_decision": MergeDecision(
                outcome=Outcome.merged,
                reason="merged",
                merge_commit_sha=sha,
            ).model_dump(mode="json"),
            "events": [{"event": "merged", "sha": sha, "timestamp": now_iso()}],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "failed",
            "outcome": Outcome.blocked_human.value,
            "error": f"合并失败: {exc}",
            "merge_decision": MergeDecision(
                outcome=Outcome.blocked_human,
                reason=str(exc),
                requires_human=True,
            ).model_dump(mode="json"),
        }


def _parse_ts(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _job_record(state: ReviewState, outcome: str, message: str) -> dict[str, Any]:
    risk = state.get("risk_tier") or {}
    approvals = valid_approvals(
        state.get("human_approvals") or [],
        head_sha=state.get("head_sha", ""),
        current_tree_hash=safe_tree_hash(state),
    )
    findings = list(state.get("findings") or [])
    by_severity: dict[str, int] = {}
    for item in findings:
        key = str(item.get("severity", "minor"))
        by_severity[key] = by_severity.get(key, 0) + 1
    finished = now_iso()
    created = _parse_ts((state.get("pr_context") or {}).get("created_at"))
    first_approval = _parse_ts(approvals[0].get("at")) if approvals else None
    latency = (
        round((first_approval - created).total_seconds(), 1)
        if created and first_approval else None
    )
    started = _parse_ts(state.get("started_at"))
    finished_at = _parse_ts(finished)
    duration = round((finished_at - started).total_seconds(), 1) if started and finished_at else None

    tool_usage: dict[str, dict] = {}
    for event in state.get("tool_events") or []:
        key = f"{event.get('role', '')}:{event.get('tool', '')}"
        row = tool_usage.setdefault(key, {"calls": 0, "failed": 0, "duration_ms": 0})
        row["calls"] += 1
        row["duration_ms"] += int(event.get("duration_ms", 0) or 0)
        if event.get("status") != "ok":
            row["failed"] += 1

    blocking_open = [
        item for item in findings
        if item.get("lifecycle", "open") == "open" and item.get("blocking")
    ]
    return {
        "job_id": state.get("job_id", ""),
        "repo": state.get("repo", ""),
        "pr_number": state.get("pr_number", 0),
        "title": state.get("pr_title", ""),
        "author": state.get("author", ""),
        "profile": state.get("profile", ""),
        "importance_level": state.get("importance_level", "medium"),
        "tier": risk.get("tier", "T1"),
        "outcome": outcome,
        "message": message,
        "started_at": state.get("started_at", ""),
        "finished_at": finished,
        "duration_sec": duration,
        "approvals": len(approvals),
        "required_approvals": int(risk.get("min_human_approvals", 0)),
        "approvers": [item.get("approver", "") for item in approvals],
        "approval_latency_sec": latency,
        "review_rounds": int(state.get("review_round", 0)),
        "fix_rounds": int(state.get("fix_round", 0)),
        "review_round_mode": state.get("review_round_mode", "full"),
        "escalation_reason": state.get("escalation_reason", ""),
        "findings_total": len(findings),
        "findings_blocking": len(blocking_open),
        "findings_resolved": sum(
            1 for item in (state.get("finding_history") or [])
            if item.get("lifecycle") == "resolved"
        ),
        "findings_by_severity": by_severity,
        "finding_outcomes": state.get("finding_outcomes") or {},
        "cost_cny": round(sum(
            float(item.get("cost_cny", 0.0)) for item in (state.get("budget_events") or [])
        ), 4),
        "llm_calls": int(state.get("llm_calls", 0)),
        "tool_calls": int(state.get("tool_calls", 0)),
        "tool_usage": tool_usage,
    }


def persist_memory_and_finalize_node(state: ReviewState, ctx: Any) -> dict[str, Any]:
    """终态收尾：仅在结果验证与审批确定后写入本地向量库（程序化记忆），并落盘报告。"""
    job_id = state.get("job_id", "")
    update_progress(ctx, job_id, "收尾: 写入程序化向量记忆与终态报告...", 99)

    outcome = state.get("outcome")
    escalation = str(state.get("escalation_reason") or "")
    if not outcome:
        iv = state.get("issue_verification") or {}
        md_outcome = str((state.get("merge_decision") or {}).get("outcome") or "")
        if iv.get("status") == "NOT_REPRODUCED":
            outcome = Outcome.rejected_no_issue.value
        elif iv.get("status") == "OUT_OF_SCOPE":
            outcome = Outcome.rejected_out_of_scope.value
        elif escalation:
            outcome = Outcome.blocked_human.value
        elif md_outcome and md_outcome != Outcome.merge_ready.value:
            outcome = md_outcome
        elif state.get("budget_exceeded") or state.get("error"):
            outcome = Outcome.infra_failed.value
        else:
            outcome = Outcome.changes_requested.value

    message = {
        Outcome.rejected_no_issue.value: "未复现 PR 声称的问题。",
        Outcome.rejected_out_of_scope.value: "该 PR 不属于 bugfix 范围。",
        Outcome.changes_requested.value: "需要修改后再合并。",
        Outcome.blocked_human.value: "需要人工确认。",
        Outcome.infra_failed.value: "基础设施异常，未合并。",
        Outcome.merged.value: "已合并。",
    }.get(outcome, outcome)
    if escalation:
        message = f"{message}（{escalation}）"

    # 1. 将经过验证/审批的 Reviewer + Coder 经验写入本地 sqlite-vec 向量库
    written_ids: list[str] = []
    store = getattr(ctx, "memory_store", None)
    if store is not None:
        try:
            mem_rec = extract_procedural_memory(dict(state), final_outcome=str(outcome))
            if mem_rec is not None:
                mid = store.upsert(mem_rec)
                if mid:
                    written_ids.append(mid)
        except Exception:  # noqa: BLE001 - 记忆持久化失败绝不回滚已完成的 PR 终态
            pass

    # 2. 落盘本地报告与发布 GitHub Review Summary
    record = _job_record(state, str(outcome), message)
    risk = state.get("risk_tier") or {}
    review_decision = state.get("review_decision") or {}
    findings = list(state.get("findings") or [])
    reports = Path(ctx.cfg.reports_dir) / "jobs"
    try:
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"{job_id}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except OSError:
        pass

    summary_text = build_summary(
        job_id=job_id,
        outcome=str(outcome),
        profile=state.get("profile", "change"),
        tier=str(risk.get("tier") or "T1"),
        required_approvals=int(risk.get("min_human_approvals", 0)),
        findings=findings,
        verdict=str(review_decision.get("verdict") or ""),
        reason=(state.get("merge_decision") or {}).get("reason", "") or message,
    )
    if (state.get("review_summary") or {}).get("markdown_body"):
        summary_text = f"{summary_text}\n\n{state['review_summary']['markdown_body']}"

    posted = False
    if state.get("dry_run"):
        try:
            write_local_report(reports / f"{job_id}.md", summary=summary_text, findings=findings)
        except OSError:
            pass
    elif ctx.github is not None and ctx.cfg.post_review:
        try:
            stats = publish_review(
                ctx.github,
                repo=state.get("repo", ""),
                number=int(state.get("pr_number", 0)),
                head_sha=state.get("head_sha", ""),
                job_id=job_id,
                findings=findings,
                outcome=str(outcome),
                profile=state.get("profile", "change"),
                tier=str(risk.get("tier") or "T1"),
                required_approvals=int(risk.get("min_human_approvals", 0)),
                verdict=str(review_decision.get("verdict") or ""),
                reason=(state.get("merge_decision") or {}).get("reason", "") or message,
            )
            posted = bool(stats.get("summary_posted"))
        except Exception:  # noqa: BLE001
            posted = False
        if not posted:
            try:
                ctx.github.comment(state.get("repo", ""), int(state.get("pr_number", 0)), summary_text)
            except Exception:  # noqa: BLE001
                pass

    return {
        "outcome": str(outcome),
        "status": "done",
        "finished_at": record["finished_at"],
        "run_record": record,
        "memory_records_written": written_ids,
        "events": [{"event": "finalized", "outcome": str(outcome), "memories": written_ids, "timestamp": now_iso()}],
    }


__all__ = [
    "summary_and_submit_node",
    "merge_gate_node",
    "human_approval_node",
    "merge_node",
    "persist_memory_and_finalize_node",
]
