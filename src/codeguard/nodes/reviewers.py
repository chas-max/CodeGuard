"""Node 3 (4 路并行审查) 与 Node 4 (规则化综合 + FixDirective 下达)。"""
from __future__ import annotations

from typing import Any

from ..agents.reviewers import run_perspective_review
from ..engine.api_contract import check_api_contract
from ..engine.diff import tree_hash
from ..engine.repo_map import build_repo_map, read_context, related_files
from ..engine.rules import RuleEngine
from ..engine.static_scan import scan_tree
from ..engine.synthesis import finding_fingerprint, synthesize_reviewer_outputs
from ..engine.tests_guard import guard_tests
from ..errors import AgentError
from ..models import (
    REVIEWER_ROLES,
    DeterministicReport,
    Outcome,
    PerspectiveReviewOutput,
    ReviewDecision,
    ReviewerMemory,
    ReviewerRole,
    ReviewVerdict,
)
from ..orchestration.tasks import Task, TaskStatus
from ..state import ReviewState
from ._helpers import (
    base_root_path,
    build_role_toolbox,
    changed_paths_from_state,
    emit_event,
    load_state_policy,
    make_issue,
    now_iso,
    parallel_cost_update,
    repo_root_path,
    safe_tree_hash,
    tier_from_state,
    tool_limits_for,
    tool_update,
    update_progress,
)


def _refresh_deterministic_report(state: ReviewState, ctx: Any) -> dict[str, Any] | None:
    """修复后树哈希变化时，重跑快速确定性扫描，确保已修复静态/规则项在复审中转为 resolved。"""
    root = repo_root_path(state)
    base = base_root_path(state)
    policy = load_state_policy(state)
    changed_dicts = list(state.get("changed_files") or [])
    known_paths = {item.get("path", "") for item in changed_dicts if isinstance(item, dict) and item.get("path")}
    all_paths = list(dict.fromkeys(changed_paths_from_state(state) + list(state.get("fix_files") or [])))
    for rel in all_paths:
        if rel not in known_paths:
            changed_dicts.append({"path": rel, "status": "modified"})
    pr_dict = dict(state.get("pr_context") or {})
    diff_text = str(state.get("diff_text") or "")

    tasks = [
        Task("static", "dispatch_reviewers", lambda: scan_tree(root, all_paths, base_root=base), timeout_sec=60),
        Task(
            "rules",
            "dispatch_reviewers",
            lambda: RuleEngine(policy).evaluate(pr_dict, changed_dicts, diff_text, base_root=base, head_root=root),
            timeout_sec=60,
        ),
        Task("api", "dispatch_reviewers", lambda: check_api_contract(base, root, all_paths), timeout_sec=60),
        Task("tests_guard", "dispatch_reviewers", lambda: guard_tests(base, root, all_paths), timeout_sec=60),
    ]
    outcome = ctx.coordinator.run_stage(tasks, deadline=None)
    results = dict(outcome.results)
    if any(not results.get(tid) or results[tid].status != TaskStatus.ok for tid in ("static", "rules", "api", "tests_guard")):
        return None
    static_rep: DeterministicReport = results["static"].value
    det_findings = (
        list(static_rep.findings)
        + list(results["rules"].value)
        + list(results["api"].value)
        + list(results["tests_guard"].value)
    )
    det_report = DeterministicReport(
        findings=det_findings,
        tree_hash=tree_hash(root),
        scanned_files=static_rep.scanned_files,
        notes=list(static_rep.notes),
    )
    return det_report.model_dump(mode="json")


def dispatch_reviewers(state: ReviewState, ctx: Any) -> dict[str, Any]:
    """分发节点：切换全量/增量模式、按需刷新修复后确定性扫描并清空当轮 reviewer_outputs。"""
    job_id = state.get("job_id", "")
    next_round = int(state.get("review_round", 0)) + 1
    incremental = next_round > 1 and bool(state.get("fix_patch"))
    round_mode = "incremental" if incremental else "full"
    active_diff = str(state.get("fix_patch") or "") if incremental else str(state.get("diff_text") or "")
    update_progress(
        ctx,
        job_id,
        f"Node 3: 启动第 {next_round} 轮 4 视角并行审查（{round_mode}）...",
        35,
    )
    update: dict[str, Any] = {
        "review_round": next_round,
        "review_round_mode": round_mode,
        "active_diff": active_diff,
        "reviewer_outputs": {"__reset__": True},
        "events": [{"event": "dispatch_reviewers", "round": next_round, "mode": round_mode, "timestamp": now_iso()}],
    }
    if state.get("candidate") == "fix":
        current_hash = safe_tree_hash(state)
        prev_hash = str((state.get("deterministic_report") or {}).get("tree_hash") or "")
        if current_hash and prev_hash != current_hash:
            refreshed = _refresh_deterministic_report(state, ctx)
            if refreshed is not None:
                update["deterministic_report"] = refreshed
    return update


def make_reviewer_node(role: ReviewerRole):
    """构造单一审查视角 LangGraph 并行节点；只写入所属 key 与增量指标。"""

    def _node(state: ReviewState, ctx: Any) -> dict[str, Any]:
        root = repo_root_path(state)
        policy = load_state_policy(state)
        repo_map = state.get("repo_map") or build_repo_map(root)
        changed = changed_paths_from_state(state)
        fix_files = list(state.get("fix_files") or [])
        focus_paths = list(dict.fromkeys(fix_files + changed))
        rel_files = related_files(root, repo_map, focus_paths, limit=16)
        repo_context = read_context(root, rel_files, max_chars_per_file=4000, max_total=24000)

        memories = state.get("reviewer_memories") or {}
        raw_mem = memories.get(role) or {"role": role}
        memory = ReviewerMemory.model_validate({"role": role, **raw_mem})
        round_no = int(state.get("review_round", 1) or 1)
        round_mode = str(state.get("review_round_mode") or "full")
        diff_text = str(state.get("active_diff") or state.get("diff_text") or "")

        tools = build_role_toolbox(ctx, role, state, root)
        limits = tool_limits_for(policy, tier=tier_from_state(state))

        try:
            output, costs, events = run_perspective_review(
                ctx,
                role=role,
                pr_brief=state.get("pr_brief") or {},
                diff_text=diff_text,
                repo_context=repo_context,
                guidance=state.get("review_guidance", ""),
                deterministic_findings=list((state.get("deterministic_report") or {}).get("findings") or []),
                memory=memory,
                round_no=round_no,
                round_mode=round_mode,
                tools=tools,
                limits=limits,
                tier=tier_from_state(state),
            )
        except (AgentError, Exception) as exc:  # noqa: BLE001
            # 单个并行分支失败不拖垮其它 3 个分支；交由 synthesize_findings 统一判定降级/失败
            fallback = PerspectiveReviewOutput(
                role=role,
                needs_modification=False,
                summary=f"unavailable: {exc}",
                findings=[],
                confidence=0.0,
            )
            return {
                "reviewer_outputs": {role: fallback.model_dump(mode="json")},
                "agent_issues": [make_issue(role, "llm_error", str(exc))],
            }

        new_fps = list(dict.fromkeys(
            list(memory.raised_fingerprints)
            + [finding_fingerprint(item) for item in output.findings]
        ))[-100:]
        new_notes = list(dict.fromkeys(
            list(memory.notes) + list(output.memory_notes)
        ))[-30:]
        updated_memory = memory.model_copy(update={
            "rounds_completed": memory.rounds_completed + 1,
            "raised_fingerprints": new_fps,
            "notes": new_notes,
        })

        update: dict[str, Any] = {
            "reviewer_outputs": {role: output.model_dump(mode="json")},
            "reviewer_memories": {role: updated_memory.model_dump(mode="json")},
        }
        update.update(tool_update(events))
        update.update(parallel_cost_update(costs))
        return update

    _node.__name__ = role
    return _node


def _sync_resolved_memories(
    memories: dict[str, dict],
    newly_resolved: list[dict],
) -> dict[str, dict]:
    """将已收敛解决的问题指纹写入各 Reviewer 短期记忆的 resolved_fingerprints。"""
    if not newly_resolved:
        return {}
    resolved_fps = [finding_fingerprint(item) for item in newly_resolved]
    updated: dict[str, dict] = {}
    for role in REVIEWER_ROLES:
        raw = memories.get(role) or {"role": role}
        mem = ReviewerMemory.model_validate({"role": role, **raw})
        merged_res = list(dict.fromkeys(list(mem.resolved_fingerprints) + resolved_fps))[-100:]
        updated[role] = mem.model_copy(update={"resolved_fingerprints": merged_res}).model_dump(mode="json")
    return updated


def synthesize_findings(state: ReviewState, ctx: Any) -> dict[str, Any]:
    """Node 4：规则化综合 4 路审查结论、Grounding 过滤、融合去重并生成 FixDirective。"""
    job_id = state.get("job_id", "")
    update_progress(ctx, job_id, "Node 4: 规则综合 4 路审查结果与生成修复工单...", 50)
    root = repo_root_path(state)
    policy = load_state_policy(state)
    repo_map = state.get("repo_map") or build_repo_map(root)
    repo_files = {item["path"] for item in repo_map.get("files", [])}
    outputs = dict(state.get("reviewer_outputs") or {})

    # 汇总预算超限检查（并行分支不直接写标量 budget_exceeded）
    total_cost = sum(float(e.get("cost_cny", 0.0)) for e in (state.get("budget_events") or []))
    if total_cost >= float(getattr(ctx.cfg, "budget_max_cny", 10.0)):
        return {
            "budget_exceeded": True,
            "error": "budget_exceeded",
            "outcome": Outcome.infra_failed.value,
            "status": "budget_exceeded",
        }

    # 若全部 4 个审查分支都不可用，fail closed 标记 infra_failed
    unavailable_count = sum(
        1 for role in REVIEWER_ROLES
        if str((outputs.get(role) or {}).get("summary", "")).startswith("unavailable:")
    )
    if unavailable_count == len(REVIEWER_ROLES):
        return {
            "status": "failed",
            "error": "全部 4 个并行 Reviewer Agent 均不可用",
            "outcome": Outcome.infra_failed.value,
        }

    det_findings = list((state.get("deterministic_report") or {}).get("findings") or [])
    previous = list(state.get("findings") or [])
    partition = state.get("code_partition") or {}
    changed = changed_paths_from_state(state)
    related = related_files(root, repo_map, changed, limit=20)
    allowed_sources = [p for p in dict.fromkeys(list(partition.get("source_files") or []) + changed + related)]
    candidate_tests = list(dict.fromkeys(
        list(partition.get("test_files") or [])
        + [p for p in related if p in set(repo_map.get("test_files") or [])]
    ))

    round_no = int(state.get("review_round", 1) or 1)
    reconciled, newly_resolved, directive, notes = synthesize_reviewer_outputs(
        reviewer_outputs=outputs,
        deterministic_findings=det_findings,
        previous_findings=previous,
        root=root,
        repo_files=repo_files,
        max_nits=policy.review.max_nits,
        block_threshold=policy.gates.semantic_block_threshold,
        round_no=round_no,
        allowed_source_files=allowed_sources,
        candidate_test_files=candidate_tests,
    )

    blocking = [
        item for item in reconciled
        if item.get("lifecycle", "open") == "open" and item.get("blocking")
    ]
    current_hash = safe_tree_hash(state)
    max_rounds = int(getattr(ctx.cfg, "max_review_rounds", 5) or 5)
    max_fixes = int(getattr(ctx.cfg, "max_fix_rounds", 5) or 5)

    escalation_reason = str(state.get("escalation_reason") or "")
    fix_round_cur = int(state.get("fix_round", 0) or 0)
    if directive.needs_modification and (
        fix_round_cur >= max_fixes or (round_no >= max_rounds and fix_round_cur > 0)
    ):
        escalation_reason = "达到最大审查/修复轮次上限，仍存在未收敛问题，需人工介入"

    verdict = ReviewVerdict.approve if not blocking else ReviewVerdict.request_changes
    review_decision = ReviewDecision(
        verdict=verdict,
        confidence=0.9 if not blocking else 0.85,
        summary=directive.summary,
        findings=[],  # blocking/advisory 全量在 state["findings"] 维护，避免 approve validator 冲突
        fix_instructions=directive.acceptance_criteria,
        residual_risks=notes[:20],
    )

    resolved_mem_updates = _sync_resolved_memories(
        state.get("reviewer_memories") or {}, newly_resolved,
    )
    update: dict[str, Any] = {
        "findings": reconciled,
        "finding_history": newly_resolved,
        "blocking_findings": blocking,
        "fix_directive": directive.model_dump(mode="json"),
        "review_decision": review_decision.model_dump(mode="json"),
        "reviewed_sha": state.get("head_sha", ""),
        "reviewed_tree_hash": current_hash,
        "escalation_reason": escalation_reason,
        "status": "synthesized",
        "events": [{
            "event": "synthesized",
            "round": round_no,
            "blocking": len(blocking),
            "needs_modification": directive.needs_modification,
            "timestamp": now_iso(),
        }],
    }
    if resolved_mem_updates:
        update["reviewer_memories"] = resolved_mem_updates
    emit_event(ctx, job_id, "synthesized", {
        "round": round_no,
        "blocking": len(blocking),
        "needs_modification": directive.needs_modification,
    })
    return update


review_logic = make_reviewer_node("review_logic")
review_security = make_reviewer_node("review_security")
review_architecture = make_reviewer_node("review_architecture")
review_tests = make_reviewer_node("review_tests")

__all__ = [
    "dispatch_reviewers",
    "review_logic",
    "review_security",
    "review_architecture",
    "review_tests",
    "synthesize_findings",
    "make_reviewer_node",
]
