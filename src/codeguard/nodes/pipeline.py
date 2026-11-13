"""PR 审查图节点实现（11 节点）。"""
from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path
from typing import Any

from langgraph.types import interrupt

from ..agents.fixer import propose_fix, propose_fix_with_tools
from ..agents.finding_verifier import verify_findings
from ..agents.issue_verifier import extract_claim
from ..agents.reviewer import review
from ..agents.semantic_detector import detect_semantics
from ..engine.api_contract import check_api_contract
from ..engine.diff import (
    apply_patch,
    diff_stats,
    patch_paths,
    tree_hash,
    unified_diff,
    worktree_diff,
)
from ..engine.findings import (
    blocking_findings,
    group_by_class,
    open_findings,
    reconcile,
)
from ..engine.guidance import load_guidance, select_guidance
from ..engine.hybrid import build_hybrid_report
from ..engine.patch import validate_fix_patch
from ..engine.policy import Policy, load_policy
from ..engine.profile import resolve_profile
from ..engine.repo_map import build_repo_map, is_test_path, read_context, related_files
from ..engine.rules import RuleEngine
from ..engine.static_scan import scan_tree
from ..engine.tests_guard import guard_tests
from ..engine.tier import classify_tier
from ..errors import AgentError, PolicyError
from ..github.merge import merge_blockers, valid_approvals
from ..github.report import build_summary, publish_review, write_local_report
from ..models import (
    Approval,
    DeterministicReport,
    IntentStatus,
    IntentVerification,
    IssueStatus,
    IssueVerification,
    MergeDecision,
    MergeMode,
    Outcome,
    PullRequestContext,
    ReviewDecision,
    ReviewVerdict,
    ReproEvidence,
    SandboxStatus,
    SemanticReport,
    ValidationReport,
)
from ..orchestration.tasks import Task, TaskStatus
from ..sandbox.models import SandboxResult, SandboxSpec
from ..sandbox.runner import validation_from_results
from ..sandbox.workspace import init_baseline_repo, materialize
from ..state import ReviewState
from ..tools.spec import ToolLimits


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _emit(ctx, job_id: str, event: str, data: dict | None = None) -> None:
    if getattr(ctx, "rt", None):
        ctx.rt.emit_event(job_id, event, data or {})


def _progress(ctx, job_id: str, text: str, pct: int) -> None:
    if getattr(ctx, "rt", None):
        ctx.rt.update_progress(job_id, text, pct)


def _issue(agent: str, kind: str, detail: str) -> dict:
    return {"agent": agent, "issue_type": kind, "detail": detail, "timestamp": _now()}


def _cost_update(state: ReviewState, costs: list[dict], cfg) -> dict:
    if not costs:
        return {}
    total = sum(float(e.get("cost_cny", 0.0)) for e in state.get("budget_events", []))
    added = sum(float(c.get("cost_cny", 0.0)) for c in costs)
    exceeded = (total + added) >= cfg.budget_max_cny
    return {
        "budget_events": costs,
        "llm_calls": int(state.get("llm_calls", 0)) + len(costs),
        "budget_exceeded": exceeded,
        "error": state.get("error") if not exceeded else "budget_exceeded",
    }


def _policy(state: ReviewState) -> Policy:
    raw = state.get("policy") or {}
    return Policy.model_validate(raw) if raw else Policy()


def _evidence_ok(state: ReviewState, policy: Policy, profile: str) -> bool:
    """合并前的证据门槛：只有 bugfix 且策略要求复现时，才硬性要求 CONFIRMED。"""
    if profile == "bugfix" and policy.require_reproduction:
        status = (state.get("issue_verification") or {}).get("status")
        return status == IssueStatus.confirmed.value
    return True


def _repo_root(state: ReviewState) -> Path:
    raw = state.get("repo_root", "")
    if not raw:
        raise ValueError("repo_root 未设置")
    return Path(raw)


def _cumulative_diff(base: Path, root: Path, paths: list[str], *, max_chars: int = 200_000) -> str:
    """base 树到当前工作区的累计差异。

    修复后 `repo_root` 已指向修复树，`diff_text` 必须同步重算，否则 reviewer 会拿到
    修复前的旧差异（与 validate 使用的树不一致）。
    """
    parts: list[str] = []
    for rel in dict.fromkeys(p for p in paths if p):
        old_path, new_path = base / rel, root / rel
        old = old_path.read_text(encoding="utf-8", errors="replace") if old_path.is_file() else ""
        new = new_path.read_text(encoding="utf-8", errors="replace") if new_path.is_file() else ""
        if old != new:
            parts.append(unified_diff(old, new, rel))
    return "\n".join(parts)[:max_chars]


def _base_root(state: ReviewState) -> Path:
    return Path(state.get("base_root") or state.get("repo_root", ""))


def _safe_tree_hash(state: ReviewState) -> str:
    try:
        return tree_hash(_repo_root(state))
    except Exception:  # noqa: BLE001 - 早期失败时工作区可能尚未建立
        return ""


def _changed_paths(state: ReviewState) -> list[str]:
    return [f.get("path", "") for f in state.get("changed_files", []) if f.get("path")]


def _task_summary(outcome) -> dict:
    return {
        tid: {
            "status": res.status.value,
            "error": res.error,
            "duration_ms": res.duration_ms,
        }
        for tid, res in outcome.results.items()
    }


def _tier_name(state: ReviewState) -> str:
    return str((state.get("risk_tier") or {}).get("tier") or "T1")


def _tool_limits(policy: Policy) -> ToolLimits:
    settings = policy.agents.tools
    return ToolLimits(
        max_steps=settings.max_steps,
        max_tool_calls=settings.max_tool_calls,
        max_wall_sec=settings.max_wall_sec,
        max_output_chars=settings.max_output_chars,
        sandbox_timeout_sec=settings.sandbox_timeout_sec,
        protocol=str(getattr(settings, "protocol", "auto") or "auto"),
    )


def _pr_context_for_tools(state: ReviewState) -> dict:
    raw = dict(state.get("pr_context") or {})
    if not raw.get("repo") and state.get("repo"):
        raw["repo"] = state.get("repo", "")
    if not raw.get("pr_number") and state.get("pr_number"):
        raw["pr_number"] = state.get("pr_number", 0)
    if not raw.get("title") and state.get("pr_title"):
        raw["title"] = state.get("pr_title", "")
    if not raw.get("body") and state.get("pr_body"):
        raw["body"] = state.get("pr_body", "")
    if not raw.get("author") and state.get("author"):
        raw["author"] = state.get("author", "")
    if not raw.get("base_ref") and state.get("base_ref"):
        raw["base_ref"] = state.get("base_ref", "")
    if not raw.get("head_ref") and state.get("resolved_branch"):
        raw["head_ref"] = state.get("resolved_branch", "")
    raw.setdefault("tier", _tier_name(state))
    if "changed_files" not in raw:
        raw["changed_files"] = [
            f.get("path", "") for f in (state.get("changed_files") or []) if isinstance(f, dict) and f.get("path")
        ]
    return raw


def _toolbox(ctx, role: str, state: ReviewState, root: Path | None, *, allowed_files=None):
    """按当前 policy/tier 构造工具箱；无工具或构造失败返回 None（回退单轮）。"""
    factory = getattr(ctx, "tools_for", None)
    if factory is None or root is None:
        return None
    policy = _policy(state)
    base_raw = state.get("base_root")
    base_root = Path(base_raw) if base_raw else None
    return factory(
        role,
        root=root,
        job_id=state.get("job_id", ""),
        policy=policy,
        tier=_tier_name(state),
        repo_map=state.get("repo_map") or {},
        allowed_files=allowed_files,
        limits=_tool_limits(policy),
        base_root=base_root,
        pr_context=_pr_context_for_tools(state),
        findings=list(state.get("findings") or []),
    )


def _tool_update(state: ReviewState, events: list[dict] | None) -> dict:
    if not events:
        return {}
    return {
        "tool_events": events,
        "tool_calls": int(state.get("tool_calls", 0)) + len(events),
    }


def _tool_usage(events: list[dict]) -> dict:
    """按 role:tool 聚合工具调用，供 job 报告展示。"""
    usage: dict[str, dict] = {}
    for event in events:
        key = f"{event.get('role', '')}:{event.get('tool', '')}"
        row = usage.setdefault(key, {"calls": 0, "failed": 0, "duration_ms": 0})
        row["calls"] += 1
        row["duration_ms"] += int(event.get("duration_ms", 0) or 0)
        if event.get("status") != "ok":
            row["failed"] += 1
    return usage


def _validate_worktree_patch(
    root: Path,
    fix_root: Path,
    round_no: int,
    patch: str,
    allowed: list[str],
) -> str:
    """Agent 已在 fix_root 落盘改动，补丁不能再 apply 一次；用干净副本做校验。"""
    check_root = fix_root.parent / f"fix_{round_no}_check"
    try:
        materialize(root, check_root)
        init_baseline_repo(check_root)
        _, error = validate_fix_patch(
            check_root, patch, allowed_files=allowed,
            forbidden_prefixes=(".github/workflows/", ".codeguard/"),
        )
        return error
    finally:
        shutil.rmtree(check_root, ignore_errors=True)


# ────────────────────────────── 节点 ──────────────────────────────


def ingest(state: ReviewState, ctx) -> dict:
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "已接收 PR 事件，开始预检...", 3)
    return {"status": "ingested", "events": [{"event": "ingested", "timestamp": _now()}]}


def preflight(state: ReviewState, ctx) -> dict:
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "正在校验 PR、工作区与策略...", 8)
    try:
        pr = PullRequestContext.model_validate(state.get("pr_context") or {})
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"PR 上下文非法: {exc}", "outcome": Outcome.infra_failed.value}
    root_raw = state.get("repo_root") or ""
    if not root_raw:
        if ctx.github is None:
            return {
                "status": "failed",
                "error": "未提供本地工作区，且未配置 GitHub 客户端/Token",
                "outcome": Outcome.infra_failed.value,
            }
        from ..sandbox.workspace import clone_repo

        workspace = Path(ctx.cfg.data_dir) / job_id
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
                "status": "failed", "error": f"拉取 PR 代码失败: {exc}",
                "outcome": Outcome.infra_failed.value,
            }
        root, base = head_dir, base_dir
    else:
        root = Path(root_raw)
        base = Path(state.get("base_root") or root)
        workspace = Path(ctx.cfg.data_dir) / job_id
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
    if not state.get("diff_text"):
        parts: list[str] = []
        for item in changed:
            rel = item.get("path", "")
            bpath = base / rel
            hpath = root / rel
            old = bpath.read_text(encoding="utf-8", errors="replace") if bpath.is_file() else ""
            new = hpath.read_text(encoding="utf-8", errors="replace") if hpath.is_file() else ""
            if old != new:
                parts.append(unified_diff(old, new, rel))
        diff_text = "\n".join(parts)
    else:
        diff_text = state.get("diff_text", "")
    repo_map = build_repo_map(root)
    stats = diff_stats(diff_text)
    profile = resolve_profile(
        policy,
        explicit=pr.profile or state.get("profile", ""),
        labels=pr.labels,
    )
    if ctx.cfg.review_mode == "external":
        profile = "bugfix"
    risk = classify_tier(
        policy,
        changed_paths=[item.get("path", "") for item in changed],
        changed_lines=int(stats.get("lines", 0)),
        author=pr.author,
        pr_body=pr.body,
        labels=pr.labels,
    )
    guidance = select_guidance(
        load_guidance(base, policy.review.guidance_path),
        [item.get("path", "") for item in changed],
    )
    workspace = Path(ctx.cfg.data_dir) / job_id
    return {
        "pr_context": pr.model_dump(mode="json"),
        "changed_files": changed,
        "diff_text": diff_text,
        "diff_stats": stats,
        "profile": profile,
        "risk_tier": risk.model_dump(mode="json"),
        "review_guidance": guidance,
        "repo_map": repo_map,
        "policy": policy.model_dump(mode="json"),
        "policy_sha": pr.base_sha,
        "resolved_branch": pr.base_ref,
        "repo_root": str(root),
        "base_root": str(base),
        "workspace_dir": str(workspace),
        "status": "preflight",
        "events": [{"event": "preflight_done", "timestamp": _now()}],
    }


def hybrid_scan(state: ReviewState, ctx) -> dict:
    """确定性检测 + LLM 跨文件语义检测，由 Coordinator 并行调度。"""
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "正在执行静态/规则/语义混合检测...", 30)
    root = _repo_root(state)
    base = _base_root(state)
    changed = _changed_paths(state)
    policy = _policy(state)
    repo_map = state.get("repo_map") or build_repo_map(root)
    ctx_limit = 10 if len(changed) > 8 else 20
    ctx_chars = 3500 if len(changed) > 8 else 6000
    repo_context = read_context(
        root,
        related_files(root, repo_map, changed, limit=ctx_limit),
        max_chars_per_file=ctx_chars,
    )

    def _static():
        return scan_tree(root, changed, base_root=base)

    def _rules():
        return RuleEngine(policy).evaluate(
            state.get("pr_context", {}), state.get("changed_files", []),
            state.get("diff_text", ""), base_root=base, head_root=root,
        )

    def _api():
        return check_api_contract(base, root, changed)

    def _guard():
        return guard_tests(base, root, changed)

    # 先并行跑完四个确定性任务，再将结果传给语义检测 Agent，避免重复报告且消除时序错位。
    det_task_objs = [
        Task("static", "hybrid_scan", _static, timeout_sec=60),
        Task("rules", "hybrid_scan", _rules, timeout_sec=60),
        Task("api", "hybrid_scan", _api, timeout_sec=60),
        Task("tests_guard", "hybrid_scan", _guard, timeout_sec=60),
    ]
    det_outcome = ctx.coordinator.run_stage(det_task_objs, deadline=None)
    results = dict(det_outcome.results)

    det_tasks = ("static", "rules", "api", "tests_guard")
    det_failed = [tid for tid in det_tasks if not results.get(tid) or results[tid].status != TaskStatus.ok]
    if det_failed:
        return {
            "status": "failed",
            "error": f"确定性检测任务失败: {', '.join(det_failed)}",
            "outcome": Outcome.infra_failed.value,
            "task_results": _task_summary(det_outcome),
            "agent_issues": [_issue("hybrid_scan", "tool_error", f"{tid} failed") for tid in det_failed],
        }

    static_rep: DeterministicReport = results["static"].value
    rule_findings = results["rules"].value
    api_findings = results["api"].value
    guard_findings = results["tests_guard"].value

    deterministic = DeterministicReport(
        findings=list(static_rep.findings) + list(rule_findings) + list(api_findings) + list(guard_findings),
        tree_hash=tree_hash(root),
        scanned_files=static_rep.scanned_files,
        notes=list(static_rep.notes),
    )
    det_messages = [
        f"{f.path}:{f.line} [{f.rule_id or f.source.value}] {f.message}"
        for f in deterministic.findings
    ]
    if not det_messages:
        det_messages = [f.get("message", "") for f in state.get("findings", []) if f.get("message")]

    def _semantic():
        return detect_semantics(
            ctx, root=root, repo_map=repo_map, repo_context=repo_context,
            diff_text=state.get("diff_text", ""), changed_files=changed,
            deterministic_findings=det_messages,
            threshold=policy.gates.semantic_block_threshold,
            tools=_toolbox(ctx, "semantic_detector", state, root),
            limits=_tool_limits(policy),
        )

    sem_outcome = ctx.coordinator.run_stage(
        [Task("semantic", "hybrid_scan", _semantic, timeout_sec=180, retries=1)],
        deadline=None,
    )
    results.update(sem_outcome.results)
    task_summary = {**_task_summary(det_outcome), **_task_summary(sem_outcome)}
    total_wall_ms = det_outcome.wall_ms + sem_outcome.wall_ms

    semantic_rep = SemanticReport()
    costs: list[dict] = []
    tool_events: list[dict] = []
    sem = results.get("semantic")
    semantic_unavailable = not (sem and sem.status == TaskStatus.ok)
    if not semantic_unavailable:
        semantic_rep, costs, sem_tool_events = sem.value
        tool_events.extend(sem_tool_events)
    else:
        semantic_rep = SemanticReport(
            limitations=[f"semantic detector 不可用: {sem.error if sem else 'missing'}"]
        )

    hybrid = build_hybrid_report(
        deterministic, semantic_rep, tree_hash=deterministic.tree_hash,
        semantic_block_threshold=policy.gates.semantic_block_threshold,
    )
    current_findings = [f.model_dump(mode="json") for f in hybrid.findings]
    reconciled, newly_resolved = reconcile(state.get("findings") or [], current_findings)
    verify_issues: list[dict] = []
    if policy.review.max_findings_verified and not semantic_unavailable:
        # 人类看到之前先复核 blocking 语义 finding：两票都判“不是问题”才降级。
        reconciled, verify_costs, verify_issues, verify_tool_events = verify_findings(
            ctx, reconciled,
            repo_context=repo_context,
            diff_text=state.get("diff_text", ""),
            max_items=policy.review.max_findings_verified,
            tools=_toolbox(ctx, "finding_verifier", state, root),
            limits=_tool_limits(policy),
        )
        costs.extend(verify_costs)
        tool_events.extend(verify_tool_events)
    blocking = blocking_findings(reconciled)
    update: dict[str, Any] = {
        "deterministic_report": deterministic.model_dump(mode="json"),
        "semantic_report": semantic_rep.model_dump(mode="json"),
        "semantic_unavailable": semantic_unavailable,
        "hybrid_scan_report": hybrid.model_dump(mode="json"),
        "findings": reconciled,
        "finding_history": newly_resolved,
        "blocking_findings": blocking,
        "agent_issues": verify_issues,
        "task_results": task_summary,
        "task_events": [{"stage": "hybrid_scan", "wall_ms": total_wall_ms,
                         "results": task_summary, "timestamp": _now()}],
        "status": "failed" if semantic_unavailable else "scanned",
        "error": "跨文件语义检测不可用" if semantic_unavailable else state.get("error"),
        "outcome": Outcome.infra_failed.value if semantic_unavailable else state.get("outcome"),
    }
    update.update(_tool_update(state, tool_events))
    update.update(_cost_update(state, costs, ctx.cfg))
    _emit(ctx, job_id, "scan_done", {"blocking": len(blocking), "findings": len(hybrid.findings)})
    return update


def _test_command(policy: Policy, tests: list[str]) -> str:
    """按策略生成测试命令：优先 test_command_template，其次 test_commands。"""
    joined = " ".join(tests)
    template = policy.sandbox.test_command_template or ""
    if "{tests}" in template:
        return template.replace("{tests}", joined).strip()
    commands = list(policy.sandbox.test_commands or ["python -m pytest -q"])
    first = commands[0] + ((" " + joined) if joined else "")
    return " && ".join([first] + commands[1:])


def _sandbox_result(
    ctx, state: ReviewState, root: Path, tests: list[str], *,
    command: str | None = None, network: str | None = None,
) -> SandboxResult:
    policy = _policy(state)
    spec = SandboxSpec(
        job_id=state.get("job_id", ""),
        workspace=root,
        image=policy.sandbox.image,
        commands=[command or _test_command(policy, tests)],
        timeout_sec=min(policy.sandbox.timeout_sec, ctx.cfg.sandbox_timeout_sec),
        network=network or "none",
        memory=ctx.cfg.sandbox_memory,
        cpus=ctx.cfg.sandbox_cpus,
        pids_limit=ctx.cfg.sandbox_pids,
    )
    try:
        return ctx.sandbox.run(spec)
    except Exception as exc:  # noqa: BLE001
        return SandboxResult(status=SandboxStatus.infra_error, exit_code=-1, infra_error=f"沙箱调用失败: {exc}")


def _run_prepare(ctx, state: ReviewState, root: Path) -> str:
    """按策略执行 prepare_commands（装依赖等）；返回错误信息，空串表示成功。"""
    policy = _policy(state)
    if not policy.sandbox.prepare_commands:
        return ""
    result = _sandbox_result(
        ctx, state, root, [],
        command=" && ".join(policy.sandbox.prepare_commands),
        network=policy.sandbox.prepare_network,
    )
    if result.status == SandboxStatus.passed:
        return ""
    return result.infra_error or f"prepare 失败（exit={result.exit_code}）"


def _run_policy_checks(ctx, state: ReviewState, root: Path) -> tuple[list[dict], bool]:
    """执行策略里的额外检查（lint/type 等）；required 失败会阻断。"""
    policy = _policy(state)
    results: list[dict] = []
    required_ok = True
    for check in policy.sandbox.checks:
        result = _sandbox_result(ctx, state, root, [], command=check.command)
        passed = result.status == SandboxStatus.passed
        results.append({
            "name": check.name,
            "status": "passed" if passed else "failed",
            "required": check.required,
        })
        if check.required and not passed:
            required_ok = False
    return results, required_ok


def verify_issue(state: ReviewState, ctx) -> dict:
    """按 profile 分派：bugfix 走复现门禁，change 只做意图/证据判定。"""
    if (state.get("profile") or "change") == "bugfix":
        return _verify_bugfix(state, ctx)
    return _verify_change(state, ctx)


def _verify_change(state: ReviewState, ctx) -> dict:
    """change profile：解析 PR 意图，产出 advisory 的意图判定，不阻断合并。"""
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "正在解析 PR 意图与证据...", 45)
    root = _repo_root(state)
    policy = _policy(state)
    repo_map = state.get("repo_map") or build_repo_map(root)
    costs: list[dict] = []
    tool_events: list[dict] = []
    try:
        claim, c, events = extract_claim(
            ctx,
            pr_title=state.get("pr_title", ""),
            pr_body=state.get("pr_body", ""),
            linked_issue="",
            repo_files=[item["path"] for item in repo_map.get("files", [])][:200],
            diff_text=state.get("diff_text", ""),
            profile="change",
            tools=_toolbox(ctx, "issue_verifier", state, root),
            limits=_tool_limits(policy),
        )
        costs.extend(c)
        tool_events.extend(events)
    except AgentError as exc:
        return {
            "claim": None,
            "intent_verification": IntentVerification(
                status=IntentStatus.infra_failed, reason=f"意图解析失败: {exc}",
            ).model_dump(mode="json"),
            "agent_issues": [_issue("issue_verifier", "llm_error", str(exc))],
            "status": "verified",
            **_cost_update(state, costs, ctx.cfg),
        }

    changed = _changed_paths(state)
    tests_changed = any(is_test_path(p) for p in changed)
    tier = state.get("risk_tier") or {}
    docs_only = tier.get("tier") == "T0"
    statement = (claim.statement or "").strip()
    evidence: list[str] = []
    if statement:
        evidence.append(f"声明: {statement[:200]}")
    if claim.expected or claim.actual:
        evidence.append("给出了期望/实际行为")
    if tests_changed:
        evidence.append("变更包含测试文件")
    if docs_only:
        evidence.append("改动只涉及文档/配置说明")

    sufficient = bool(statement) and bool(
        claim.expected or claim.actual or tests_changed or docs_only
    )
    verification = IntentVerification(
        status=IntentStatus.sufficient if sufficient else IntentStatus.unclear,
        claimed_kind=claim.kind,
        confidence=claim.confidence,
        reason=(
            "意图与验收证据充分" if sufficient
            else "PR 未说明要解决的问题或缺少验收证据（advisory，不阻断）"
        ),
        evidence=evidence,
    )
    update: dict[str, Any] = {
        "claim": claim.model_dump(mode="json"),
        "intent_verification": verification.model_dump(mode="json"),
        "status": "verified",
    }
    update.update(_tool_update(state, tool_events))
    update.update(_cost_update(state, costs, ctx.cfg))
    _emit(ctx, job_id, "intent_verified", {
        "status": verification.status.value, "claimed_kind": claim.kind,
    })
    return update


def _verify_bugfix(state: ReviewState, ctx) -> dict:
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "正在提取 bug claim 并运行 base/head 复现测试...", 45)
    root = _repo_root(state)
    base = _base_root(state)
    policy = _policy(state)
    repo_map = state.get("repo_map") or build_repo_map(root)
    costs: list[dict] = []
    tool_events: list[dict] = []
    try:
        claim, c, events = extract_claim(
            ctx,
            pr_title=state.get("pr_title", ""),
            pr_body=state.get("pr_body", ""),
            linked_issue="",
            repo_files=[item["path"] for item in repo_map.get("files", [])][:200],
            diff_text=state.get("diff_text", ""),
            tools=_toolbox(ctx, "issue_verifier", state, root),
            limits=_tool_limits(policy),
        )
        costs.extend(c)
        tool_events.extend(events)
    except AgentError as exc:
        update = {
            "claim": None,
            "issue_verification": IssueVerification(
                status=IssueStatus.insufficient_evidence, confidence=0.0,
                reason=f"claim 提取失败: {exc}",
            ).model_dump(mode="json"),
            "agent_issues": [_issue("issue_verifier", "llm_error", str(exc))],
            "status": "verified",
        }
        update.update(_tool_update(state, tool_events))
        update.update(_cost_update(state, costs, ctx.cfg))
        return update

    if claim.kind != "bugfix":
        return {
            "claim": claim.model_dump(mode="json"),
            "issue_verification": IssueVerification(
                status=IssueStatus.out_of_scope, confidence=claim.confidence,
                reason=f"非 bugfix PR（kind={claim.kind}）",
            ).model_dump(mode="json"),
            "status": "verified",
            **_tool_update(state, tool_events),
            **_cost_update(state, costs, ctx.cfg),
        }

    repro_tests = list(state.get("repro_tests") or [])
    if not repro_tests:
        repro_tests = [p for p in _changed_paths(state) if is_test_path(p)]
    if not repro_tests:
        # PR 未改测试时，退化为运行仓库既有测试作为问题证据。
        repro_tests = list(repo_map.get("test_files", []))[:20]
    if not repro_tests:
        return {
            "claim": claim.model_dump(mode="json"),
            "issue_verification": IssueVerification(
                status=IssueStatus.insufficient_evidence, confidence=claim.confidence,
                reason="PR 未提供可复现测试，且未找到可运行的测试文件",
            ).model_dump(mode="json"),
            "repro_tests": [],
            "status": "verified",
            **_tool_update(state, tool_events),
            **_cost_update(state, costs, ctx.cfg),
        }

    verify_tasks = [
        Task("base_repro", "verify_issue", lambda: _sandbox_result(ctx, state, base, repro_tests),
             timeout_sec=ctx.cfg.sandbox_timeout_sec + 30),
        Task("head_repro", "verify_issue", lambda: _sandbox_result(ctx, state, root, repro_tests),
             timeout_sec=ctx.cfg.sandbox_timeout_sec + 30),
    ]
    verify_outcome = ctx.coordinator.run_stage(verify_tasks)
    base_res = verify_outcome.results.get("base_repro").value if verify_outcome.results.get("base_repro") else None
    head_res = verify_outcome.results.get("head_repro").value if verify_outcome.results.get("head_repro") else None
    if base_res is None or head_res is None:
        base_res = base_res or SandboxResult(status=SandboxStatus.infra_error, infra_error="base 验证任务失败")
        head_res = head_res or SandboxResult(status=SandboxStatus.infra_error, infra_error="head 验证任务失败")
    evidence = ReproEvidence(
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
        verification = IssueVerification(
            status=IssueStatus.insufficient_evidence, confidence=0.0,
            reason="沙箱环境异常，无法验证问题是否存在", evidence=[evidence],
        )
    elif evidence.base_failed:
        verification = IssueVerification(
            status=IssueStatus.confirmed, confidence=max(claim.confidence, 0.6),
            reason="base 上复现测试失败，问题存在", evidence=[evidence], test_source="pr",
        )
    elif evidence.head_passed and base_res.exit_code == 0:
        confidence = claim.confidence
        status = IssueStatus.not_reproduced if confidence >= ctx.cfg.reject_confidence else IssueStatus.insufficient_evidence
        verification = IssueVerification(
            status=status, confidence=confidence,
            reason="复现测试在 base 上通过，未能复现声称的问题", evidence=[evidence], test_source="pr",
        )
    else:
        verification = IssueVerification(
            status=IssueStatus.insufficient_evidence, confidence=claim.confidence,
            reason="复现结果不一致", evidence=[evidence],
        )

    update: dict[str, Any] = {
        "claim": claim.model_dump(mode="json"),
        "issue_verification": verification.model_dump(mode="json"),
        "repro_tests": repro_tests,
        "repro_evidence": [evidence.model_dump(mode="json")],
        "status": "verified",
    }
    update.update(_tool_update(state, tool_events))
    update.update(_cost_update(state, costs, ctx.cfg))
    _emit(ctx, job_id, "issue_verified", {"status": verification.status.value,
                                          "confidence": verification.confidence})
    return update


def validate(state: ReviewState, ctx) -> dict:
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "正在 Docker 中运行复现与回归测试...", 70)
    root = _repo_root(state)
    repo_map = state.get("repo_map") or build_repo_map(root)
    repro = list(state.get("repro_tests") or [])
    all_tests = [p for p in repo_map.get("test_files", []) if p not in repro]
    profile = state.get("profile") or "change"
    prepare_error = _run_prepare(ctx, state, root)
    if prepare_error:
        result = ValidationReport(
            status=SandboxStatus.infra_error, commands=[], tree_hash=tree_hash(root),
            error=f"prepare 失败: {prepare_error}",
        )
        _emit(ctx, job_id, "validation_done", {"status": result.status.value})
        return {
            "validation_report": result.model_dump(mode="json"),
            "reviewed_sha": state.get("head_sha", ""),
            "reviewed_tree_hash": tree_hash(root),
            "status": "validated",
        }
    checks, checks_ok = _run_policy_checks(ctx, state, root)
    if not repro:
        if profile == "bugfix":
            result = ValidationReport(
                status=SandboxStatus.failed, commands=[], repro_passed=False,
                regression_passed=False, error="缺少复现测试", tree_hash=tree_hash(root),
                checks=checks,
            )
            return {"validation_report": result.model_dump(mode="json"), "status": "validated"}
        # change profile 没有复现锚点：退化为“按策略跑回归”；仓库无测试时跳过并放行。
        if not all_tests:
            result = ValidationReport(
                status=SandboxStatus.passed if checks_ok else SandboxStatus.failed,
                commands=[], repro_passed=True,
                regression_passed=True, tree_hash=tree_hash(root),
                error="" if checks_ok else "required 检查失败",
                checks=checks + [{"name": "tests", "status": "skipped", "reason": "仓库没有可运行测试"}],
                logs="change profile：无可运行测试，跳过沙箱验证",
            )
            _emit(ctx, job_id, "validation_done", {"status": result.status.value})
            return {
                "validation_report": result.model_dump(mode="json"),
                "reviewed_sha": state.get("head_sha", ""),
                "reviewed_tree_hash": tree_hash(root),
                "status": "validated",
            }
        run = _sandbox_result(ctx, state, root, all_tests)
        if run.status == SandboxStatus.infra_error:
            status = SandboxStatus.infra_error
        else:
            status = SandboxStatus.passed if (run.exit_code == 0 and checks_ok) else SandboxStatus.failed
        result = ValidationReport(
            status=status, commands=[], repro_passed=True,
            regression_passed=run.exit_code == 0, logs=run.stdout[-8000:],
            error=run.infra_error or ("" if checks_ok else "required 检查失败"),
            tree_hash=tree_hash(root),
            checks=checks + [{"name": "tests", "status": "passed" if run.exit_code == 0 else "failed"}],
        )
        _emit(ctx, job_id, "validation_done", {"status": result.status.value})
        return {
            "validation_report": result.model_dump(mode="json"),
            "reviewed_sha": state.get("head_sha", ""),
            "reviewed_tree_hash": tree_hash(root),
            "status": "validated",
        }

    validate_tasks = [
        Task("repro", "validate", lambda: _sandbox_result(ctx, state, root, repro),
             timeout_sec=ctx.cfg.sandbox_timeout_sec + 30),
    ]
    if all_tests:
        validate_tasks.append(
            Task("regression", "validate", lambda: _sandbox_result(ctx, state, root, all_tests),
                 timeout_sec=ctx.cfg.sandbox_timeout_sec + 30)
        )
    validate_outcome = ctx.coordinator.run_stage(validate_tasks)
    repro_res = validate_outcome.results.get("repro").value if validate_outcome.results.get("repro") else None
    if repro_res is None:
        repro_res = SandboxResult(status=SandboxStatus.infra_error, infra_error="repro 验证任务失败")
    if all_tests:
        regression_res = validate_outcome.results.get("regression").value if validate_outcome.results.get("regression") else None
        if regression_res is None:
            regression_res = SandboxResult(status=SandboxStatus.infra_error, infra_error="regression 验证任务失败")
        report = validation_from_results(repro=repro_res, regression=regression_res, tree_hash=tree_hash(root))
    else:
        report = ValidationReport(
            status=SandboxStatus.passed if repro_res.status == SandboxStatus.passed else (
                SandboxStatus.infra_error if repro_res.status == SandboxStatus.infra_error else SandboxStatus.failed
            ),
            commands=[], repro_passed=repro_res.status == SandboxStatus.passed,
            regression_passed=True, logs=repro_res.stdout[-8000:],
            error=repro_res.infra_error, tree_hash=tree_hash(root),
        )
    if checks:
        if not checks_ok and report.status == SandboxStatus.passed:
            report = report.model_copy(update={
                "status": SandboxStatus.failed, "error": "required 检查失败",
            })
        report = report.model_copy(update={"checks": checks})
    update = {
        "validation_report": report.model_dump(mode="json"),
        "reviewed_sha": state.get("head_sha", ""),
        "reviewed_tree_hash": tree_hash(root),
        "status": "validated",
    }
    _emit(ctx, job_id, "validation_done", {"status": report.status.value})
    return update


def review_node(state: ReviewState, ctx) -> dict:
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "唯一审查 Agent 正在做最终整体审查...", 85)
    profile = state.get("profile") or "change"
    policy = _policy(state)
    round_no = int(state.get("review_round", 0)) + 1
    incremental = round_no > 1 and bool(state.get("fix_patch"))
    if incremental:
        # 收敛：第二轮起只看新改动，不重新挑上一轮已通过的代码。
        diff = state.get("fix_patch", "")
        round_mode = "incremental"
    else:
        diff = state.get("diff_text", "")
        round_mode = "full"
    try:
        decision, costs, tool_events = review(
            ctx,
            claim=state.get("claim") or {},
            hybrid_report=state.get("hybrid_scan_report") or {},
            validation_report=state.get("validation_report"),
            diff_text=diff,
            fix_note=(state.get("fix_history") or [{}])[-1].get("explanation", ""),
            profile=profile,
            risk_tier=state.get("risk_tier"),
            intent=state.get("intent_verification") or state.get("claim") or {},
            guidance=state.get("review_guidance", ""),
            round_mode=round_mode,
            max_nits=policy.review.max_nits,
            open_findings=open_findings(state.get("findings") or []),
            changed_paths=_changed_paths(state),
            tools=_toolbox(
                ctx, "reviewer", state,
                Path(state["repo_root"]) if state.get("repo_root") else None,
            ),
            limits=_tool_limits(policy),
        )
    except AgentError as exc:
        return {
            "review_decision": None,
            "agent_issues": [_issue("reviewer", "llm_error", str(exc))],
            "status": "review_failed",
            "error": str(exc),
            **_cost_update(state, [], ctx.cfg),
        }
    update: dict[str, Any] = {
        "review_decision": decision.model_dump(mode="json"),
        "review_round": round_no,
        "review_round_mode": round_mode,
        "status": "approved" if decision.verdict == ReviewVerdict.approve else "review_changes",
    }
    if decision.verdict != ReviewVerdict.approve and round_no >= ctx.cfg.max_review_rounds:
        # 不再让 agent 互相刷轮次：达到上限就停下来要求改方案。
        update["escalation_reason"] = "达到审查轮次上限，建议改方案后由人工接手"
        update["status"] = "review_escalated"
    update.update(_tool_update(state, tool_events))
    update.update(_cost_update(state, costs, ctx.cfg))
    _emit(ctx, job_id, "review_done", {
        "verdict": decision.verdict.value,
        "confidence": decision.confidence,
        "round": round_no,
        "mode": round_mode,
    })
    return update


def fix_node(state: ReviewState, ctx) -> dict:
    job_id = state.get("job_id", "")
    round_no = int(state.get("fix_round", 0)) + 1
    _progress(ctx, job_id, f"修复 Agent 第 {round_no} 轮生成补丁...", 75)
    root = _repo_root(state)
    repo_map = state.get("repo_map") or build_repo_map(root)
    changed = _changed_paths(state)
    related = related_files(root, repo_map, changed, limit=20)
    allowed = [p for p in dict.fromkeys(changed + related) if not is_test_path(p)]
    if not allowed:
        return {"status": "fix_failed", "error": "没有可修复的源文件",
                "open_issues": [_issue("fixer", "scope", "没有可修复的源文件")]}
    open_items = open_findings(state.get("findings") or [])
    review_decision = state.get("review_decision") or {}
    for item in review_decision.get("findings") or []:
        if isinstance(item, dict):
            open_items.append({**item, "lifecycle": "open"})
    blocking_items = [item for item in open_items if item.get("blocking")]
    grouped = group_by_class(blocking_items or open_items)
    validation = state.get("validation_report") or {}
    if not grouped and validation.get("status") == SandboxStatus.failed.value:
        # 没有显式 finding，但沙箱验证失败：把失败本身作为一类问题交给 fixer，
        # 具体失败信息仍由 validation_logs 传入。
        grouped = [{
            "class_id": "VALIDATION", "id": "VALIDATION", "category": "tests",
            "severity": "major", "blocking": True, "path": "",
            "message": "沙箱验证失败，需要修复使测试通过",
            "suggestion": "根据验证日志定位失败用例并修复根因",
            "paths": [], "instances": [], "instance_count": 0,
        }]
    if not grouped:
        return {"status": "fix_failed", "error": "没有未解决的 finding 可修",
                "open_issues": [_issue("fixer", "scope", "没有未解决的 finding 可修")]}
    logs = (state.get("validation_report") or {}).get("logs", "")
    policy = _policy(state)
    limits = _tool_limits(policy)
    fix_root = Path(ctx.cfg.data_dir) / job_id / f"fix_{round_no}"
    shutil.rmtree(fix_root, ignore_errors=True)
    materialize(root, fix_root)
    baseline_error = init_baseline_repo(fix_root)
    if baseline_error:
        # 无法建立 git 基线时退回单轮 patch 流程，不阻断修复。
        _emit(ctx, job_id, "fix_baseline_failed", {"error": baseline_error})
    tools = None if baseline_error else _toolbox(
        ctx, "fixer", state, fix_root, allowed_files=allowed,
    )
    write_mode = tools is not None and getattr(tools, "capability", "") == "write"
    costs: list[dict] = []
    tool_events: list[dict] = []
    try:
        if write_mode:
            proposal, costs, tool_events = propose_fix_with_tools(
                ctx,
                diff_text=state.get("diff_text", ""),
                findings=grouped,
                validation_logs=logs,
                repo_context=read_context(root, allowed, max_chars_per_file=5000),
                allowed_files=allowed,
                round_no=round_no,
                tools=tools,
                limits=limits,
            )
        else:
            proposal, costs = propose_fix(
                ctx,
                diff_text=state.get("diff_text", ""),
                findings=grouped,
                validation_logs=logs,
                repo_context=read_context(root, allowed, max_chars_per_file=5000),
                allowed_files=allowed,
                round_no=round_no,
            )
    except AgentError as exc:
        return {"status": "fix_failed", "error": str(exc),
                "open_issues": [_issue("fixer", "llm_error", str(exc))],
                **_tool_update(state, tool_events),
                **_cost_update(state, costs, ctx.cfg)}

    # 写模式下以工作区最终状态为准：Agent 可能只改文件、没在 JSON 里给 patch。
    worktree_patch = worktree_diff(fix_root) if write_mode else ""
    if worktree_patch.strip():
        error = _validate_worktree_patch(
            root, fix_root, round_no, worktree_patch, allowed,
        )
        if error:
            return {
                "status": "fix_failed", "error": error,
                "open_issues": [_issue("fixer", "invalid_patch", error)],
                "fix_history": [{"round": round_no, "error": error}],
                **_tool_update(state, tool_events),
                **_cost_update(state, costs, ctx.cfg),
            }
        proposal = proposal.model_copy(update={
            "patch": worktree_patch,
            "files": patch_paths(worktree_patch),
            "applies_cleanly": True,
        })
    else:
        proposal, error = validate_fix_patch(
            fix_root, proposal.patch, allowed_files=allowed,
            forbidden_prefixes=(".github/workflows/", ".codeguard/"),
        )
        if error:
            return {
                "status": "fix_failed", "error": error,
                "open_issues": [_issue("fixer", "invalid_patch", error)],
                "fix_history": [{"round": round_no, "error": error}],
                **_tool_update(state, tool_events),
                **_cost_update(state, costs, ctx.cfg),
            }
        ok, output = apply_patch(fix_root, proposal.patch)
        if not ok:
            return {
                "status": "fix_failed", "error": f"应用修复补丁失败: {output}",
                "open_issues": [_issue("fixer", "apply_failed", output)],
                "fix_history": [{"round": round_no, "error": output}],
                **_tool_update(state, tool_events),
                **_cost_update(state, costs, ctx.cfg),
            }
    proposal = proposal.model_copy(update={"applies_cleanly": True, "tree_hash": tree_hash(fix_root)})
    pushed = False
    remote_sha = ""
    pr_ctx = state.get("pr_context") or {}
    if ctx.github is not None and ctx.cfg.push_fixes and not state.get("dry_run") and not pr_ctx.get("is_fork"):
        contents = {}
        for rel in proposal.files:
            file_path = fix_root / rel
            if file_path.is_file():
                contents[rel] = file_path.read_text(encoding="utf-8", errors="replace")
        try:
            pushed_result = ctx.github.push_fix(
                state.get("repo", ""), state.get("pr_context", {}).get("head_ref", ""),
                message=f"CodeGuard: fix review findings (round {round_no})",
                files=contents, expected_head_sha=state.get("head_sha", ""),
            )
            remote_sha = (pushed_result or {}).get("sha", "")
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "fix_failed", "error": f"推送修复提交失败: {exc}",
                "open_issues": [_issue("fixer", "push_failed", str(exc))],
                "fix_history": [{"round": round_no, "error": str(exc)}],
                **_tool_update(state, tool_events),
                **_cost_update(state, costs, ctx.cfg),
            }
        if not remote_sha:
            return {
                "status": "fix_failed", "error": "GitHub 未返回修复提交 SHA",
                "open_issues": [_issue("fixer", "push_failed", "missing commit sha")],
                **_tool_update(state, tool_events),
                **_cost_update(state, costs, ctx.cfg),
            }
        pushed = True

    # 修复树已成为新的审查对象：同步重算 PR 级累计 diff，避免 reviewer
    # 拿到「修复前差异 + 修复后验证结果」这种互相矛盾的输入。
    diff_paths = list(dict.fromkeys(_changed_paths(state) + list(proposal.files)))
    cumulative = _cumulative_diff(_base_root(state), fix_root, diff_paths)

    update: dict[str, Any] = {
        "repo_root": str(fix_root),
        "candidate": "fix",
        "fix_round": round_no,
        "diff_text": cumulative,
        "diff_stats": diff_stats(cumulative),
        "fix_patch": proposal.patch,
        "fix_files": proposal.files,
        "fix_tree_hash": proposal.tree_hash,
        "fix_pushed": pushed,
        "fix_remote_sha": remote_sha,
        "head_sha": remote_sha or state.get("head_sha", ""),
        "fix_history": [{"round": round_no, "files": proposal.files,
                         "explanation": proposal.explanation, "timestamp": _now()}],
        "status": "fixed",
    }
    update.update(_tool_update(state, tool_events))
    update.update(_cost_update(state, costs, ctx.cfg))
    _emit(ctx, job_id, "fix_done", {"round": round_no, "files": proposal.files})
    return update


def merge_gate(state: ReviewState, ctx) -> dict:
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "正在执行确定性合并门控...", 92)
    policy = _policy(state)
    validation = ValidationReport.model_validate(state["validation_report"]) if state.get("validation_report") else None
    review = ReviewDecision.model_validate(state["review_decision"]) if state.get("review_decision") else None
    pr = state.get("pr_context") or {}
    human = state.get("human_decision") or {}
    profile = state.get("profile") or "change"
    blocking_open = list(state.get("blocking_findings") or [])

    changed = list(dict.fromkeys(_changed_paths(state) + list(state.get("fix_files") or [])))
    stats = state.get("diff_stats") or {}
    current_hash = _safe_tree_hash(state)
    api_break = any(
        item.get("rule_id") == "API-001" and item.get("blocking")
        for item in (state.get("findings") or [])
    )
    risk = classify_tier(
        policy,
        changed_paths=changed,
        changed_lines=int(stats.get("lines", 0)),
        author=state.get("author", ""),
        pr_body=state.get("pr_body", ""),
        labels=list(pr.get("labels") or []),
        api_break=api_break,
    )
    mode = MergeMode(policy.gates.merge_mode)
    if ctx.cfg.merge_mode == MergeMode.human:
        mode = MergeMode.human  # 环境变量只允许收紧

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
        evidence_ok=_evidence_ok(state, policy, profile),
        tier=risk.tier.value,
        blocking_open=len(blocking_open),
    )
    if state.get("semantic_unavailable"):
        blockers.append("跨文件语义检测不可用，不能自动合并")
    if blocking_open:
        blockers.append(
            "仍存在 blocking 检测问题: "
            + ", ".join(sorted({f.get("rule_id") or f.get("id", "") for f in blocking_open})[:5])
        )
    if state.get("fix_patch") and not state.get("fix_pushed"):
        blockers.append("修复补丁未推送到 PR 分支（fork/禁用/本地模式），不能自动合并")
    requires_human = any("人工" in b or "MERGE_MODE" in b for b in blockers)
    if blockers:
        outcome = Outcome.blocked_human if requires_human else Outcome.changes_requested
        decision = MergeDecision(outcome=outcome, reason="；".join(blockers),
                                 requires_human=requires_human)
    else:
        decision = MergeDecision(outcome=Outcome.merge_ready, reason="合并门控通过",
                                 requires_human=False, actions=["merge"])
    update = {
        "risk_tier": risk.model_dump(mode="json"),
        "merge_decision": decision.model_dump(mode="json"),
        "requires_human": decision.requires_human,
        "status": "merge_ready" if not decision.requires_human and not blockers else "merge_blocked",
    }
    if decision.outcome == Outcome.changes_requested:
        update["status"] = "merge_blocked"
    _emit(ctx, job_id, "merge_gate_done", {
        "outcome": decision.outcome.value,
        "requires_human": decision.requires_human,
        "tier": risk.tier.value,
        "required_approvals": risk.min_human_approvals,
    })
    return update


def human_approval(state: ReviewState, ctx) -> dict:
    """一次中断收集一次审批；凑够所需人数后回到 merge_gate。"""
    risk = state.get("risk_tier") or {}
    required = int(risk.get("min_human_approvals", 1))
    approvals = list(state.get("human_approvals") or [])
    decision = interrupt({
        "type": "human_approval",
        "pr": f"{state.get('repo')}#{state.get('pr_number')}",
        "reason": (state.get("merge_decision") or {}).get("reason", ""),
        "tier": risk.get("tier", "T1"),
        "required_approvals": required,
        "collected_approvals": [item.get("approver") for item in approvals],
        "require_non_author": bool(risk.get("require_non_author")),
        "author": state.get("author", ""),
        "review": state.get("review_decision"),
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
            tree_hash=_safe_tree_hash(state),
            at=_now(),
            finding_outcomes={str(k): str(v) for k, v in outcomes.items()} if isinstance(outcomes, dict) else {},
        ).model_dump(mode="json"))
    return {
        "human_approvals": approvals,
        "human_decision": {"approved": approved, "approver": approver, "feedback": feedback},
        "finding_outcomes": merged_outcomes,
        "status": "human_decided",
        "events": [{"event": "human_approval", "approved": approved,
                    "approver": approver, "timestamp": _now()}],
    }


def merge_node(state: ReviewState, ctx) -> dict:
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "正在通过 GitHub merge API 合并...", 98)
    if state.get("dry_run"):
        return {
            "status": "done",
            "outcome": Outcome.blocked_human.value,
            "error": "dry-run 模式不执行合并",
            "merge_decision": MergeDecision(
                outcome=Outcome.blocked_human, reason="dry-run 模式不执行合并",
                requires_human=True,
            ).model_dump(mode="json"),
        }
    if ctx.github is None:
        return {"status": "failed", "outcome": Outcome.infra_failed.value,
                "error": "GitHub 客户端未配置"}
    risk = state.get("risk_tier") or {}
    required = int(risk.get("min_human_approvals", 0))
    approvals = valid_approvals(
        state.get("human_approvals") or [],
        head_sha=state.get("head_sha", ""),
        current_tree_hash=_safe_tree_hash(state),
    )
    if required and len(approvals) < required:
        return {
            "status": "failed",
            "outcome": Outcome.blocked_human.value,
            "error": f"审批不足（{len(approvals)}/{required}），拒绝合并",
            "merge_decision": MergeDecision(
                outcome=Outcome.blocked_human, reason="审批不足", requires_human=True,
            ).model_dump(mode="json"),
        }
    try:
        result = ctx.github.merge(
            state.get("repo", ""), int(state.get("pr_number", 0)),
            sha=state.get("head_sha", ""),
            method=ctx.cfg.merge_method,
            title=f"CodeGuard: {state.get('pr_title', '')}"[:200],
            message="由 CodeGuard 自动审查并合并。",
        )
        sha = (result or {}).get("sha", "")
        if not sha:
            return {
                "status": "failed",
                "outcome": Outcome.blocked_human.value,
                "error": "GitHub merge API 未返回 merge commit SHA",
                "merge_decision": MergeDecision(
                    outcome=Outcome.blocked_human, reason="merge API 未返回 SHA",
                    requires_human=True,
                ).model_dump(mode="json"),
            }
        merged_state = {**state, "outcome": Outcome.merged.value, "merge_commit_sha": sha}
        try:
            _publish_outcome(merged_state, ctx, Outcome.merged.value, "已合并。")
        except Exception:  # noqa: BLE001 - 报告失败不回滚已完成的合并
            pass
        return {
            "status": "done",
            "outcome": Outcome.merged.value,
            "merge_commit_sha": sha,
            "merge_decision": MergeDecision(
                outcome=Outcome.merged, reason="merged", merge_commit_sha=sha,
            ).model_dump(mode="json"),
            "events": [{"event": "merged", "sha": sha, "timestamp": _now()}],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "failed",
            "outcome": Outcome.blocked_human.value,
            "error": f"合并失败: {exc}",
            "merge_decision": MergeDecision(
                outcome=Outcome.blocked_human, reason=str(exc), requires_human=True,
            ).model_dump(mode="json"),
        }


def _parse_ts(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _job_record(state: ReviewState, ctx, outcome: str, message: str) -> dict:
    risk = state.get("risk_tier") or {}
    approvals = valid_approvals(
        state.get("human_approvals") or [],
        head_sha=state.get("head_sha", ""),
        current_tree_hash=_safe_tree_hash(state),
    )
    findings = list(state.get("findings") or [])
    findings.extend((state.get("review_decision") or {}).get("findings") or [])
    by_severity: dict[str, int] = {}
    for item in findings:
        key = str(item.get("severity", "minor"))
        by_severity[key] = by_severity.get(key, 0) + 1
    finished = _now()
    created = _parse_ts((state.get("pr_context") or {}).get("created_at"))
    first_approval = _parse_ts(approvals[0].get("at")) if approvals else None
    latency = (
        round((first_approval - created).total_seconds(), 1)
        if created and first_approval else None
    )
    started = _parse_ts(state.get("started_at"))
    finished_at = _parse_ts(finished)
    duration = round((finished_at - started).total_seconds(), 1) if started and finished_at else None
    return {
        "job_id": state.get("job_id", ""),
        "repo": state.get("repo", ""),
        "pr_number": state.get("pr_number", 0),
        "title": state.get("pr_title", ""),
        "author": state.get("author", ""),
        "profile": state.get("profile", ""),
        "tier": risk.get("tier", ""),
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
        "findings_blocking": len(blocking_findings(state.get("findings") or [])),
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
        "tool_usage": _tool_usage(state.get("tool_events") or []),
        "semantic_unavailable": bool(state.get("semantic_unavailable")),
    }


def _publish_outcome(state: ReviewState, ctx, outcome: str, message: str) -> dict:
    job_id = state.get("job_id", "")
    risk = state.get("risk_tier") or {}
    review_decision = state.get("review_decision") or {}
    findings = list(state.get("findings") or [])
    findings.extend(review_decision.get("findings") or [])
    record = _job_record(state, ctx, outcome, message)

    reports = Path(ctx.cfg.reports_dir) / "jobs"
    try:
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"{job_id}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except OSError:
        pass

    summary = build_summary(
        job_id=job_id, outcome=outcome, profile=state.get("profile", "change"),
        tier=risk.get("tier", "T1"),
        required_approvals=int(risk.get("min_human_approvals", 0)),
        findings=findings, verdict=review_decision.get("verdict", ""),
        reason=(state.get("merge_decision") or {}).get("reason", "") or message,
    )
    posted = False
    if state.get("dry_run"):
        try:
            write_local_report(reports / f"{job_id}.md", summary=summary, findings=findings)
        except OSError:
            pass
    elif ctx.github is not None and ctx.cfg.post_review:
        stats = publish_review(
            ctx.github, repo=state.get("repo", ""), number=int(state.get("pr_number", 0)),
            head_sha=state.get("head_sha", ""), job_id=job_id, findings=findings,
            outcome=outcome, profile=state.get("profile", "change"),
            tier=risk.get("tier", "T1"),
            required_approvals=int(risk.get("min_human_approvals", 0)),
            verdict=review_decision.get("verdict", ""),
            reason=(state.get("merge_decision") or {}).get("reason", "") or message,
        )
        posted = bool(stats.get("summary_posted"))
    if not posted and ctx.github is not None:
        try:
            ctx.github.comment(
                state.get("repo", ""), int(state.get("pr_number", 0)), summary,
            )
        except Exception:  # noqa: BLE001 — 评论失败不改变终态
            pass
    return record


def finalize(state: ReviewState, ctx) -> dict:
    outcome = state.get("outcome")
    escalation = state.get("escalation_reason", "")
    if not outcome:
        verification = state.get("issue_verification") or {}
        status = verification.get("status")
        if status == IssueStatus.not_reproduced.value:
            outcome = Outcome.rejected_no_issue.value
        elif status == IssueStatus.out_of_scope.value:
            outcome = Outcome.rejected_out_of_scope.value
        elif escalation:
            outcome = Outcome.blocked_human.value
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
    record = _publish_outcome(state, ctx, outcome, message)
    return {
        "outcome": outcome,
        "status": "done",
        "finished_at": record["finished_at"],
        "run_record": record,
        "events": [{"event": "finalized", "outcome": outcome, "timestamp": _now()}],
    }


def route_after_preflight(state: ReviewState, ctx) -> str:
    if state.get("budget_exceeded"):
        return "finalize"
    if state.get("status") in ("failed", "skipped") or state.get("outcome"):
        return "finalize"
    return "hybrid_scan"


def route_after_hybrid(state: ReviewState, ctx) -> str:
    if state.get("budget_exceeded"):
        return "finalize"
    if state.get("status") == "failed" or state.get("outcome"):
        return "finalize"
    blocking = state.get("blocking_findings") or []
    non_fixable = [f for f in blocking if not f.get("auto_fixable")]
    if non_fixable:
        return "finalize"
    if state.get("candidate") == "fix":
        return "validate"
    return "verify_issue"


def route_after_verify(state: ReviewState, ctx) -> str:
    if state.get("budget_exceeded"):
        return "finalize"
    if (state.get("profile") or "change") == "bugfix":
        status = (state.get("issue_verification") or {}).get("status")
        return "validate" if status == IssueStatus.confirmed.value else "finalize"
    intent = state.get("intent_verification") or {}
    if intent.get("status") == IntentStatus.infra_failed.value:
        return "finalize"
    # change profile：意图不清晰只做 advisory，继续验证与审查。
    return "validate"


def route_after_validate(state: ReviewState, ctx) -> str:
    if state.get("budget_exceeded"):
        return "finalize"
    report = state.get("validation_report") or {}
    if report.get("status") == SandboxStatus.passed.value:
        return "review"
    if report.get("status") == SandboxStatus.infra_error.value:
        return "finalize"
    if int(state.get("fix_round", 0)) < ctx.cfg.max_fix_rounds:
        return "fix"
    return "finalize"


def route_after_review(state: ReviewState, ctx) -> str:
    if state.get("budget_exceeded"):
        return "finalize"
    if not state.get("review_decision"):
        return "finalize"
    decision = state.get("review_decision") or {}
    if decision.get("verdict") == ReviewVerdict.approve.value:
        return "merge_gate"
    if int(state.get("review_round", 0)) < ctx.cfg.max_review_rounds and int(
        state.get("fix_round", 0)
    ) < ctx.cfg.max_fix_rounds:
        return "fix"
    return "finalize"


def route_after_fix(state: ReviewState, ctx) -> str:
    if state.get("status") == "fixed":
        return "hybrid_scan"
    return "finalize"


def route_after_merge_gate(state: ReviewState, ctx) -> str:
    if state.get("budget_exceeded"):
        return "finalize"
    decision = state.get("merge_decision") or {}
    if decision.get("requires_human"):
        return "human_approval"
    if decision.get("outcome") in (Outcome.merged.value, Outcome.merge_ready.value):
        return "merge"
    return "finalize"


def route_after_human(state: ReviewState, ctx) -> str:
    human = state.get("human_decision") or {}
    if not human.get("approved"):
        return "finalize"
    risk = state.get("risk_tier") or {}
    required = int(risk.get("min_human_approvals", 1))
    approvals = valid_approvals(
        state.get("human_approvals") or [],
        head_sha=state.get("head_sha", ""),
        current_tree_hash=_safe_tree_hash(state),
    )
    if len(approvals) < required:
        return "human_approval"  # 自环：继续收集审批
    if risk.get("require_non_author"):
        author = str(state.get("author", "")).strip().lower()
        approvers = {str(item.get("approver", "")).strip().lower() for item in approvals}
        if author and not (approvers - {author}):
            return "human_approval"
    return "merge_gate"


__all__ = [
    "ingest", "preflight", "hybrid_scan", "verify_issue", "validate", "review_node",
    "fix_node", "merge_gate", "human_approval", "merge_node", "finalize",
    "route_after_preflight", "route_after_hybrid", "route_after_verify",
    "route_after_validate", "route_after_review", "route_after_fix",
    "route_after_merge_gate", "route_after_human",
]
