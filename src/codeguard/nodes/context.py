"""Node 1 (prepare_context) 与 Node 2 (pr_reader)：前置知识并行构建、确定性扫描、PR拆解与单调定级。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..agents.pr_reader import read_pr_context
from ..engine.api_contract import check_api_contract
from ..engine.diff import diff_stats, tree_hash, unified_diff
from ..engine.guidance import load_guidance, select_guidance
from ..engine.policy import load_policy
from ..engine.profile import resolve_profile
from ..engine.repo_map import build_repo_map, is_test_path
from ..engine.rules import RuleEngine
from ..engine.static_scan import scan_tree
from ..engine.tests_guard import guard_tests
from ..engine.tier import classify_tier
from ..errors import AgentError, PolicyError
from ..models import (
    IMPORTANCE_APPROVALS,
    IMPORTANCE_TO_TIER,
    REVIEWER_ROLES,
    DeterministicReport,
    IssueStatus,
    IssueVerification,
    Outcome,
    PullRequestContext,
    ReproEvidence,
    ReviewerMemory,
    RiskTier,
    SandboxStatus,
    TIER_TO_IMPORTANCE,
    Tier,
    max_importance,
)
from ..orchestration.tasks import Task, TaskStatus
from ..sandbox.models import SandboxResult
from ..state import ReviewState
from ._helpers import (
    base_root_path,
    build_role_toolbox,
    changed_paths_from_state,
    cost_update,
    emit_event,
    load_state_policy,
    make_issue,
    now_iso,
    partition_changed_files,
    repo_root_path,
    run_sandbox_command,
    task_summary,
    tool_limits_for,
    tool_update,
    update_progress,
)


def prepare_context(state: ReviewState, ctx: Any) -> dict[str, Any]:
    """Node 1：并行搭建 RepoMap、base 分支项目规范、代码划分、前置静态检查与确定性风险下限。"""
    job_id = state.get("job_id", "")
    update_progress(ctx, job_id, "Node 1: 并行构建 RepoMap、规范与前置确定性检查...", 10)
    try:
        pr = PullRequestContext.model_validate(state.get("pr_context") or {})
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "failed",
            "error": f"PR 上下文非法: {exc}",
            "outcome": Outcome.infra_failed.value,
        }

    workspace = Path(ctx.cfg.data_dir) / job_id
    root_raw = state.get("repo_root") or ""
    if not root_raw:
        if ctx.github is None:
            return {
                "status": "failed",
                "error": "未提供本地工作区，且未配置 GitHub 客户端/Token",
                "outcome": Outcome.infra_failed.value,
            }
        from ..sandbox.workspace import clone_repo

        base_dir = workspace / "base"
        head_dir = workspace / "head"
        repo_url = f"https://github.com/{pr.repo}.git"
        try:
            token = ctx.github_token(pr.repo)
            if not token:
                return {
                    "status": "failed",
                    "error": "未配置 GitHub 凭据（App installation 或 GITHUB_TOKEN）",
                    "outcome": Outcome.infra_failed.value,
                }
            clone_repo(repo_url, base_dir, ref=pr.base_sha or pr.base_ref, token=token)
            clone_repo(repo_url, head_dir, ref=pr.head_sha or pr.head_ref, token=token)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failed",
                "error": f"拉取 PR 代码失败: {exc}",
                "outcome": Outcome.infra_failed.value,
            }
        root, base = head_dir, base_dir
    else:
        root = Path(root_raw)
        base = Path(state.get("base_root") or root)

    if not root.is_dir():
        return {"status": "failed", "error": f"候选工作区不存在: {root}", "outcome": Outcome.infra_failed.value}
    if not base.is_dir():
        return {"status": "failed", "error": f"base 工作区不存在: {base}", "outcome": Outcome.infra_failed.value}
    if ctx.cfg.target_branch and pr.base_ref != ctx.cfg.target_branch:
        return {
            "status": "failed",
            "error": f"目标分支不匹配: PR base={pr.base_ref}, TARGET_BRANCH={ctx.cfg.target_branch}",
            "outcome": Outcome.blocked_human.value,
        }
    if pr.is_draft:
        return {"status": "skipped", "error": "draft PR 暂不处理", "outcome": Outcome.changes_requested.value}
    if max(len(pr.changed_files), pr.changed_file_count) > ctx.cfg.max_pr_files:
        return {
            "status": "failed",
            "error": f"变更文件数超限: {max(len(pr.changed_files), pr.changed_file_count)}",
            "outcome": Outcome.blocked_human.value,
        }

    # 策略与审查规范强制从 base 分支加载，防止 PR 篡改规则绕过检查
    try:
        policy = load_policy(base, ctx.cfg.policy_path)
        policy = policy.model_copy(update={
            "limits": policy.limits.model_copy(update={
                "max_files": min(policy.limits.max_files, ctx.cfg.max_pr_files),
                "max_changed_lines": min(policy.limits.max_changed_lines, ctx.cfg.max_changed_lines),
                "max_diff_bytes": min(policy.limits.max_diff_bytes, ctx.cfg.max_diff_bytes),
            })
        })
    except PolicyError as exc:
        return {"status": "failed", "error": f"策略非法: {exc}", "outcome": Outcome.blocked_human.value}

    if ctx.cfg.sandbox_backend == "docker":
        from ..sandbox.health import check_docker

        ok, message = check_docker()
        if not ok:
            return {"status": "failed", "error": f"Docker 不可用: {message}", "outcome": Outcome.infra_failed.value}

    changed = [f.model_dump(mode="json") for f in pr.changed_files]
    if not changed and state.get("changed_files"):
        changed = list(state.get("changed_files") or [])
    changed_paths = [item.get("path", "") for item in changed if item.get("path")]

    if not state.get("diff_text"):
        parts: list[str] = []
        for rel in changed_paths:
            bpath = base / rel
            hpath = root / rel
            old = bpath.read_text(encoding="utf-8", errors="replace") if bpath.is_file() else ""
            new = hpath.read_text(encoding="utf-8", errors="replace") if hpath.is_file() else ""
            if old != new:
                parts.append(unified_diff(old, new, rel))
        diff_text = "\n".join(parts)
    else:
        diff_text = state.get("diff_text", "")

    stats = diff_stats(diff_text)
    profile = resolve_profile(
        policy,
        explicit=pr.profile or state.get("profile", ""),
        labels=pr.labels,
    )
    if ctx.cfg.review_mode == "external":
        profile = "bugfix"

    # 并行执行前置知识建设与快速确定性静态检测
    pr_dict = pr.model_dump(mode="json")
    tasks = [
        Task("repo_map", "prepare_context", lambda: build_repo_map(root), timeout_sec=60),
        Task(
            "guidance",
            "prepare_context",
            lambda: select_guidance(load_guidance(base, policy.review.guidance_path), changed_paths),
            timeout_sec=20,
        ),
        Task("partition", "prepare_context", lambda: partition_changed_files(changed_paths, policy), timeout_sec=20),
        Task("static", "prepare_context", lambda: scan_tree(root, changed_paths, base_root=base), timeout_sec=60),
        Task(
            "rules",
            "prepare_context",
            lambda: RuleEngine(policy).evaluate(pr_dict, changed, diff_text, base_root=base, head_root=root),
            timeout_sec=60,
        ),
        Task("api", "prepare_context", lambda: check_api_contract(base, root, changed_paths), timeout_sec=60),
        Task("tests_guard", "prepare_context", lambda: guard_tests(base, root, changed_paths), timeout_sec=60),
    ]
    outcome = ctx.coordinator.run_stage(tasks, deadline=None)
    results = dict(outcome.results)
    failed_tasks = [
        tid for tid in ("repo_map", "guidance", "partition", "static", "rules", "api", "tests_guard")
        if not results.get(tid) or results[tid].status != TaskStatus.ok
    ]
    if failed_tasks:
        return {
            "status": "failed",
            "error": f"前置知识与静态检测失败: {', '.join(failed_tasks)}",
            "outcome": Outcome.infra_failed.value,
            "task_results": task_summary(outcome),
            "agent_issues": [make_issue("prepare_context", "tool_error", f"{tid} failed") for tid in failed_tasks],
        }

    repo_map = results["repo_map"].value
    guidance = results["guidance"].value
    partition = results["partition"].value
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
    det_dicts = [f.model_dump(mode="json") for f in det_report.findings]
    blocking = [item for item in det_dicts if item.get("blocking")]

    api_break = any(
        item.get("rule_id") == "API-001" and item.get("blocking")
        for item in det_dicts
    )
    risk = classify_tier(
        policy,
        changed_paths=changed_paths,
        changed_lines=int(stats.get("lines", 0)),
        author=pr.author,
        pr_body=pr.body,
        labels=pr.labels,
        api_break=api_break,
    )
    det_level = TIER_TO_IMPORTANCE.get(risk.tier.value, "medium")

    emit_event(ctx, job_id, "context_prepared", {
        "level": det_level,
        "deterministic_findings": len(det_dicts),
    })
    return {
        "pr_context": pr_dict,
        "changed_files": changed,
        "code_partition": partition.model_dump(mode="json"),
        "diff_text": diff_text,
        "active_diff": diff_text,
        "diff_stats": stats,
        "profile": profile,
        "risk_tier": risk.model_dump(mode="json"),
        "importance_level": det_level,
        "inherited_importance_level": det_level,
        "review_guidance": guidance,
        "repo_map": repo_map,
        "policy": policy.model_dump(mode="json"),
        "policy_sha": pr.base_sha,
        "resolved_branch": pr.head_ref or pr.base_ref,
        "repo_root": str(root),
        "base_root": str(base),
        "workspace_dir": str(workspace),
        "deterministic_report": det_report.model_dump(mode="json"),
        "findings": det_dicts,
        "blocking_findings": blocking,
        "task_results": task_summary(outcome),
        "task_events": [{"stage": "prepare_context", "wall_ms": outcome.wall_ms,
                         "results": task_summary(outcome), "timestamp": now_iso()}],
        "status": "context_prepared",
        "events": [{"event": "context_prepared", "timestamp": now_iso()}],
    }


def _verify_bugfix_reproduction(
    state: ReviewState,
    ctx: Any,
    *,
    root: Path,
    base: Path,
    repo_map: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """在 bugfix 模式下执行 base/head 复现对比，过滤无复现依据的伪 bugfix PR。"""
    repro_tests = list(state.get("repro_tests") or [])
    if not repro_tests:
        repro_tests = [p for p in changed_paths_from_state(state) if is_test_path(p)]
    if not repro_tests:
        repro_tests = list(repo_map.get("test_files", []))[:20]
    if not repro_tests:
        iv = IssueVerification(
            status=IssueStatus.insufficient_evidence,
            confidence=0.3,
            reason="PR 未提供可复现测试，且未找到可运行的测试文件",
        )
        return iv.model_dump(mode="json"), [], []

    verify_tasks = [
        Task("base_repro", "pr_reader", lambda: run_sandbox_command(ctx, state, base, repro_tests),
             timeout_sec=ctx.cfg.sandbox_timeout_sec + 30),
        Task("head_repro", "pr_reader", lambda: run_sandbox_command(ctx, state, root, repro_tests),
             timeout_sec=ctx.cfg.sandbox_timeout_sec + 30),
    ]
    verify_outcome = ctx.coordinator.run_stage(verify_tasks)
    base_res = verify_outcome.results.get("base_repro").value if verify_outcome.results.get("base_repro") else None
    head_res = verify_outcome.results.get("head_repro").value if verify_outcome.results.get("head_repro") else None
    if base_res is None or head_res is None:
        base_res = base_res or SandboxResult(status=SandboxStatus.infra_error, infra_error="base 验证任务失败")
        head_res = head_res or SandboxResult(status=SandboxStatus.infra_error, infra_error="head 验证任务失败")

    ev = ReproEvidence(
        test_source="pr",
        test_file=", ".join(repro_tests),
        command="python -m pytest -q " + " ".join(repro_tests),
        base_exit_code=base_res.exit_code,
        head_exit_code=head_res.exit_code,
        base_failed=(base_res.status == SandboxStatus.failed or base_res.exit_code != 0),
        head_passed=(head_res.status == SandboxStatus.passed and head_res.exit_code == 0),
        log=(base_res.stdout + "\n" + head_res.stdout)[-8000:],
    )
    if base_res.status == SandboxStatus.infra_error or head_res.status == SandboxStatus.infra_error:
        iv = IssueVerification(
            status=IssueStatus.insufficient_evidence,
            confidence=0.0,
            reason="沙箱环境异常，无法验证问题是否存在",
            evidence=[ev],
        )
    elif ev.base_failed:
        iv = IssueVerification(
            status=IssueStatus.confirmed,
            confidence=0.85,
            reason="base 上复现测试失败，问题存在",
            evidence=[ev],
            test_source="pr",
        )
    elif ev.head_passed and base_res.exit_code == 0:
        iv = IssueVerification(
            status=IssueStatus.not_reproduced,
            confidence=0.9,
            reason="复现测试在 base 上通过，未能复现声称的问题",
            evidence=[ev],
            test_source="pr",
        )
    else:
        iv = IssueVerification(
            status=IssueStatus.insufficient_evidence,
            confidence=0.4,
            reason="复现结果不一致",
            evidence=[ev],
        )
    return iv.model_dump(mode="json"), repro_tests, [ev.model_dump(mode="json")]


def pr_reader_node(state: ReviewState, ctx: Any) -> dict[str, Any]:
    """Node 2：提取 PR 结构化信息（背景/前后代码/划分）、单调风险定级并初始化 4 位 Reviewer 短期记忆。"""
    job_id = state.get("job_id", "")
    update_progress(ctx, job_id, "Node 2: PR 信息读取 Agent 正在拆解意图与风险定级...", 20)
    root = repo_root_path(state)
    base = base_root_path(state)
    policy = load_state_policy(state)
    repo_map = state.get("repo_map") or {}
    risk_dict = dict(state.get("risk_tier") or {})
    det_level = max_importance(
        state.get("importance_level") or TIER_TO_IMPORTANCE.get(str(risk_dict.get("tier") or "T1"), "medium")
    )
    det_reasons = list(risk_dict.get("reasons") or [])

    tools = build_role_toolbox(ctx, "pr_reader", state, root)
    limits = tool_limits_for(policy, tier=IMPORTANCE_TO_TIER[det_level])

    costs: list[dict] = []
    tool_events: list[dict] = []
    try:
        brief, costs, tool_events = read_pr_context(
            ctx,
            pr_context=state.get("pr_context") or {},
            code_partition=state.get("code_partition") or {},
            diff_text=state.get("diff_text", ""),
            repo_map=repo_map,
            guidance=state.get("review_guidance", ""),
            deterministic_level=det_level,
            deterministic_reasons=det_reasons,
            tools=tools,
            limits=limits,
            tier=IMPORTANCE_TO_TIER[det_level],
        )
    except AgentError as exc:
        return {
            "status": "failed",
            "error": f"pr_reader 失败: {exc}",
            "outcome": Outcome.infra_failed.value,
            "agent_issues": [make_issue("pr_reader", "llm_error", str(exc))],
            **tool_update(tool_events),
            **cost_update(state, costs, ctx.cfg),
        }

    # 单调只升不降：同步更新 risk_tier 与所需审批人数
    final_level = max_importance(det_level, brief.final_level)
    mapped_tier = IMPORTANCE_TO_TIER[final_level]
    min_approvals = max(policy.min_approvals(mapped_tier), IMPORTANCE_APPROVALS[final_level])
    if final_level == "low" and policy.merge.allow_zero_human:
        min_approvals = 0
    updated_risk = RiskTier(
        tier=Tier(mapped_tier),
        reasons=brief.escalation_reasons,
        min_human_approvals=min_approvals,
        require_non_author=(final_level == "critical") or bool(risk_dict.get("require_non_author")),
        depth=policy.depth_for(mapped_tier),
        trusted_author=bool(risk_dict.get("trusted_author", False)),
        agent_authored=bool(risk_dict.get("agent_authored", False)),
    )

    # 初始化 4 个并行 Reviewer 各自的短期记忆
    memories = {
        role: ReviewerMemory(role=role).model_dump(mode="json")
        for role in REVIEWER_ROLES
    }

    update: dict[str, Any] = {
        "pr_brief": brief.model_dump(mode="json"),
        "importance_level": final_level,
        "inherited_importance_level": final_level,
        "risk_tier": updated_risk.model_dump(mode="json"),
        "reviewer_memories": memories,
        "status": "pr_read",
        "events": [{"event": "pr_read", "level": final_level, "timestamp": now_iso()}],
    }

    # bugfix 模式下执行 base/head 复现核查（发现未复现伪 bugfix 时直接拦截）
    if (state.get("profile") or "change") == "bugfix":
        iv_dict, repro_tests, repro_ev = _verify_bugfix_reproduction(
            state, ctx, root=root, base=base, repo_map=repo_map,
        )
        update["issue_verification"] = iv_dict
        update["repro_tests"] = repro_tests
        if repro_ev:
            update["repro_evidence"] = repro_ev
        if iv_dict.get("status") == IssueStatus.not_reproduced.value:
            update["outcome"] = Outcome.rejected_no_issue.value
        elif policy.require_reproduction and iv_dict.get("status") != IssueStatus.confirmed.value:
            update["outcome"] = Outcome.changes_requested.value
    else:
        update["intent_verification"] = {
            "status": "SUFFICIENT",
            "claimed_kind": brief.change_intent,
            "confidence": 0.9,
            "reason": brief.problem_summary or "PR 意图已结构化提取",
            "evidence": list(brief.risk_highlights or brief.affected_modules),
        }

    update.update(tool_update(tool_events))
    update.update(cost_update(state, costs, ctx.cfg))
    emit_event(ctx, job_id, "pr_read_done", {"level": final_level, "intent": brief.change_intent})
    return update


__all__ = ["prepare_context", "pr_reader_node"]
