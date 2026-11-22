"""Node 5 (coder_agent)、Node 6 (qa_checker) 与前置 Node 9 (verify_gate)。"""
from __future__ import annotations

import ast
import shutil
from pathlib import Path
from typing import Any

from ..agents.fixer import run_coder_agent
from ..agents.qa_checker import run_qa_checker
from ..engine.diff import (
    apply_patch,
    diff_stats,
    patch_paths,
    tree_hash,
    worktree_diff,
)
from ..engine.patch import preserves_existing_tests, validate_fix_patch
from ..engine.repo_map import build_repo_map, is_test_path, read_context, related_files
from ..engine.tier import classify_tier
from ..errors import AgentError
from ..models import (
    IMPORTANCE_APPROVALS,
    IMPORTANCE_TO_TIER,
    FixDirective,
    Outcome,
    RiskTier,
    SandboxStatus,
    TIER_TO_IMPORTANCE,
    Tier,
    ValidationReport,
    max_importance,
)
from ..orchestration.tasks import Task
from ..sandbox.models import SandboxResult
from ..sandbox.runner import validation_from_results
from ..sandbox.workspace import init_baseline_repo, materialize
from ..state import ReviewState
from ._helpers import (
    base_root_path,
    build_role_toolbox,
    changed_paths_from_state,
    cost_update,
    cumulative_diff,
    emit_event,
    load_state_policy,
    make_issue,
    now_iso,
    repo_root_path,
    run_policy_checks,
    run_prepare_commands,
    run_sandbox_command,
    tier_from_state,
    tool_limits_for,
    tool_update,
    update_progress,
)

_FORBIDDEN_PREFIXES = (".github/workflows/", ".codeguard/")


def _validate_worktree_patch(
    root: Path,
    fix_root: Path,
    patch: str,
    *,
    allowed_sources: list[str],
    allowed_tests: list[str],
) -> str:
    """用干净副本校验隔离工作区产生的 diff，并对测试追加做 AST 断言保护。"""
    check_root = fix_root.parent / f"{fix_root.name}_check"
    try:
        materialize(root, check_root)
        init_baseline_repo(check_root)
        _, error = validate_fix_patch(
            check_root,
            patch,
            allowed_files=list(dict.fromkeys(allowed_sources + allowed_tests)),
            forbidden_prefixes=_FORBIDDEN_PREFIXES,
        )
        if error:
            return error
        for rel in patch_paths(patch):
            if is_test_path(rel):
                if rel not in allowed_tests:
                    return f"禁止修改未授权的测试文件: {rel}"
                before_text = (root / rel).read_text(encoding="utf-8", errors="replace") if (root / rel).is_file() else ""
                after_text = (fix_root / rel).read_text(encoding="utf-8", errors="replace") if (fix_root / rel).is_file() else ""
                guard_err = preserves_existing_tests(before_text, after_text, path=rel)
                if guard_err:
                    return guard_err
        return ""
    finally:
        shutil.rmtree(check_root, ignore_errors=True)


def coder_node(state: ReviewState, ctx: Any) -> dict[str, Any]:
    """Node 5：受 FixDirective 与最小权限约束的 Coder Agent（含 Docker 沙箱与 RAG 检索）。"""
    job_id = state.get("job_id", "")
    round_no = int(state.get("fix_round", 0)) + 1
    max_fix_rounds = int(getattr(ctx.cfg, "max_fix_rounds", 5) or 5)
    update_progress(ctx, job_id, f"Node 5: Coder Agent 第 {round_no} 轮修复中...", 62)

    root = repo_root_path(state)
    repo_map = state.get("repo_map") or build_repo_map(root)
    policy = load_state_policy(state)
    changed = changed_paths_from_state(state)
    related = related_files(root, repo_map, changed, limit=20)

    raw_directive = state.get("fix_directive") or {}
    directive = FixDirective.model_validate(raw_directive) if raw_directive else FixDirective(round_no=round_no)
    allowed_sources = list(directive.allowed_source_files or [
        p for p in dict.fromkeys(changed + related) if not is_test_path(p)
    ])
    allowed_tests = list(directive.allowed_test_addition_files or [])
    if not allowed_sources and not allowed_tests:
        return {
            "status": "fix_failed",
            "fix_round": round_no,
            "error": "没有可修复的授权文件",
            "open_issues": [make_issue("coder", "scope", "没有可修复的授权文件")],
        }

    fix_root = Path(ctx.cfg.data_dir) / job_id / f"fix_{round_no}"
    shutil.rmtree(fix_root, ignore_errors=True)
    materialize(root, fix_root)
    baseline_error = init_baseline_repo(fix_root)
    if baseline_error:
        emit_event(ctx, job_id, "fix_baseline_failed", {"error": baseline_error})

    tools = None if baseline_error else build_role_toolbox(
        ctx,
        "coder",
        state,
        fix_root,
        allowed_files=allowed_sources,
        allowed_test_addition_files=allowed_tests,
    )
    limits = tool_limits_for(policy, tier=tier_from_state(state))
    repo_context = read_context(fix_root, allowed_sources[:12], max_chars_per_file=4500, max_total=24000)
    validation_logs = str((state.get("validation_report") or {}).get("logs") or "")
    qa_feedback = state.get("qa_report") if (state.get("qa_report") or {}).get("verdict") == "revise" else None

    costs: list[dict] = []
    tool_events: list[dict] = []
    try:
        fix_res, costs, tool_events = run_coder_agent(
            ctx,
            fix_directive=directive,
            pr_brief=state.get("pr_brief") or {},
            diff_text=state.get("diff_text", ""),
            validation_logs=validation_logs,
            qa_feedback=qa_feedback,
            repo_context=repo_context,
            round_no=round_no,
            max_rounds=max_fix_rounds,
            tools=tools,
            limits=limits,
            root=fix_root,
        )
    except (AgentError, Exception) as exc:  # noqa: BLE001
        return {
            "status": "fix_failed",
            "fix_round": round_no,
            "error": str(exc),
            "open_issues": [make_issue("coder", "llm_error", str(exc))],
            "fix_history": [{"round": round_no, "error": str(exc), "timestamp": now_iso()}],
            **tool_update(tool_events),
            **cost_update(state, costs, ctx.cfg),
        }

    # 如果模型未通过写工具落盘而是返回 patch / edits 编译补丁，在这里应用并验证
    wt_patch = worktree_diff(fix_root) if (fix_root / ".git").exists() else ""
    patch_to_use = wt_patch if wt_patch.strip() else fix_res.patch
    if not patch_to_use.strip():
        return {
            "status": "fix_failed",
            "fix_round": round_no,
            "error": "Coder 未产出有效代码补丁",
            "open_issues": [make_issue("coder", "empty_patch", "Coder 未产出有效代码补丁")],
            "fix_history": [{"round": round_no, "error": "empty_patch", "timestamp": now_iso()}],
            **tool_update(tool_events),
            **cost_update(state, costs, ctx.cfg),
        }

    if wt_patch.strip():
        err = _validate_worktree_patch(
            root,
            fix_root,
            wt_patch,
            allowed_sources=allowed_sources,
            allowed_tests=allowed_tests,
        )
        if err:
            return {
                "status": "fix_failed",
                "fix_round": round_no,
                "error": err,
                "open_issues": [make_issue("coder", "invalid_patch", err)],
                "fix_history": [{"round": round_no, "error": err, "timestamp": now_iso()}],
                **tool_update(tool_events),
                **cost_update(state, costs, ctx.cfg),
            }
        final_files = patch_paths(wt_patch)
    else:
        _, err = validate_fix_patch(
            fix_root,
            patch_to_use,
            allowed_files=list(dict.fromkeys(allowed_sources + allowed_tests)),
            forbidden_prefixes=_FORBIDDEN_PREFIXES,
        )
        if err:
            return {
                "status": "fix_failed",
                "fix_round": round_no,
                "error": err,
                "open_issues": [make_issue("coder", "invalid_patch", err)],
                "fix_history": [{"round": round_no, "error": err, "timestamp": now_iso()}],
                **tool_update(tool_events),
                **cost_update(state, costs, ctx.cfg),
            }
        for rel in patch_paths(patch_to_use):
            if is_test_path(rel) and rel not in allowed_tests:
                err_t = f"禁止修改未授权测试文件: {rel}"
                return {
                    "status": "fix_failed",
                    "fix_round": round_no,
                    "error": err_t,
                    "open_issues": [make_issue("coder", "invalid_patch", err_t)],
                    **tool_update(tool_events),
                    **cost_update(state, costs, ctx.cfg),
                }
        ok, out = apply_patch(fix_root, patch_to_use)
        if not ok:
            return {
                "status": "fix_failed",
                "fix_round": round_no,
                "error": f"应用修复补丁失败: {out}",
                "open_issues": [make_issue("coder", "apply_failed", out)],
                **tool_update(tool_events),
                **cost_update(state, costs, ctx.cfg),
            }
        final_files = patch_paths(patch_to_use)

    fix_hash = tree_hash(fix_root)
    fix_res = fix_res.model_copy(update={
        "patch": patch_to_use,
        "modified_files": final_files,
        "tree_hash": fix_hash,
    })
    all_fix_files = list(dict.fromkeys(list(state.get("fix_files") or []) + final_files))
    diff_paths = list(dict.fromkeys(changed + all_fix_files))
    cum_diff = cumulative_diff(base_root_path(state), fix_root, diff_paths)

    update: dict[str, Any] = {
        "repo_root": str(fix_root),
        "candidate": "fix",
        "fix_round": round_no,
        "diff_text": cum_diff,
        "diff_stats": diff_stats(cum_diff),
        "fix_patch": patch_to_use,
        "fix_files": all_fix_files,
        "fix_tree_hash": fix_hash,
        "fix_result": fix_res.model_dump(mode="json"),
        "fix_history": [{
            "round": round_no,
            "files": final_files,
            "explanation": fix_res.explanation,
            "rag_memory_ids_used": fix_res.rag_memory_ids_used,
            "timestamp": now_iso(),
        }],
        "status": "fixed",
    }
    update.update(tool_update(tool_events))
    update.update(cost_update(state, costs, ctx.cfg))
    emit_event(ctx, job_id, "fix_done", {"round": round_no, "files": final_files})
    return update


def qa_checker_node(state: ReviewState, ctx: Any) -> dict[str, Any]:
    """Node 6：QA Checker 监督防偏航，未对齐时直接打回 Coder 返工。"""
    job_id = state.get("job_id", "")
    round_no = int(state.get("fix_round", 1) or 1)
    max_fix_rounds = int(getattr(ctx.cfg, "max_fix_rounds", 5) or 5)
    update_progress(ctx, job_id, f"Node 6: QA Checker 核查第 {round_no} 轮修复一致性...", 72)

    root = repo_root_path(state)
    policy = load_state_policy(state)
    tools = build_role_toolbox(ctx, "qa_checker", state, root)
    limits = tool_limits_for(policy, tier=tier_from_state(state))

    costs: list[dict] = []
    tool_events: list[dict] = []
    try:
        report, costs, tool_events = run_qa_checker(
            ctx,
            fix_directive=state.get("fix_directive") or {},
            fix_result=state.get("fix_result") or {},
            actual_patch=state.get("fix_patch", ""),
            round_no=round_no,
            max_fix_rounds=max_fix_rounds,
            tools=tools,
            limits=limits,
        )
    except (AgentError, Exception) as exc:  # noqa: BLE001
        return {
            "status": "qa_failed",
            "error": f"qa_checker 调用失败: {exc}",
            "outcome": Outcome.infra_failed.value,
            "agent_issues": [make_issue("qa_checker", "llm_error", str(exc))],
            **tool_update(tool_events),
            **cost_update(state, costs, ctx.cfg),
        }

    escalation = str(state.get("escalation_reason") or "")
    if report.verdict == "escalate":
        escalation = report.summary or "QA Checker 判定修复偏航且超出可自动修复范围"

    update: dict[str, Any] = {
        "qa_report": report.model_dump(mode="json"),
        "qa_history": [{"round": round_no, **report.model_dump(mode="json"), "timestamp": now_iso()}],
        "escalation_reason": escalation,
        "status": f"qa_{report.verdict}",
        "events": [{"event": "qa_checked", "round": round_no, "verdict": report.verdict, "timestamp": now_iso()}],
    }
    update.update(tool_update(tool_events))
    update.update(cost_update(state, costs, ctx.cfg))
    emit_event(ctx, job_id, "qa_checked", {
        "round": round_no,
        "verdict": report.verdict,
        "is_aligned": report.is_aligned,
        "has_scope_drift": report.has_scope_drift,
    })
    return update


def _syntax_check_python_files(root: Path, paths: list[str]) -> str:
    """对改动中的 Python 文件执行确定性 AST 语法编译检查。"""
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        target = root / rel
        if not target.is_file():
            continue
        try:
            ast.parse(target.read_text(encoding="utf-8", errors="replace"), filename=rel)
        except SyntaxError as exc:
            return f"语法错误 {rel}:{exc.lineno}: {exc.msg}"
    return ""


def verify_gate_node(state: ReviewState, ctx: Any) -> dict[str, Any]:
    """前置后的 Node 9：在 PR 提交与 Merge 之前执行语法/Lint/Docker 回归测试，并重算风险等级。"""
    job_id = state.get("job_id", "")
    update_progress(ctx, job_id, "Node 9 (Verify Gate): 正在运行语法/Lint/回归测试门控...", 80)

    root = repo_root_path(state)
    repo_map = state.get("repo_map") or build_repo_map(root)
    policy = load_state_policy(state)
    current_hash = tree_hash(root)
    changed = list(dict.fromkeys(changed_paths_from_state(state) + list(state.get("fix_files") or [])))

    # 1. 确定性 Python 语法检查（即便在无 Docker 离线环境也拦截语法破坏）
    syntax_err = _syntax_check_python_files(root, changed)
    if syntax_err:
        report = ValidationReport(
            status=SandboxStatus.failed,
            commands=["ast.parse"],
            repro_passed=False,
            regression_passed=False,
            logs=syntax_err,
            error=syntax_err,
            tree_hash=current_hash,
            checks=[{"name": "syntax", "status": "failed", "required": True}],
        )
        return _validation_failure_update(state, report, current_hash, changed)

    # 2. 沙箱 prepare_commands
    prepare_err = run_prepare_commands(ctx, state, root)
    if prepare_err:
        report = ValidationReport(
            status=SandboxStatus.infra_error,
            commands=[],
            tree_hash=current_hash,
            error=f"prepare 失败: {prepare_err}",
        )
        emit_event(ctx, job_id, "verify_gate_done", {"status": report.status.value})
        return {
            "validation_report": report.model_dump(mode="json"),
            "status": "verify_infra_error",
        }

    # 3. 策略声明的 Lint / Typecheck 等 checks
    checks, checks_ok = run_policy_checks(ctx, state, root)
    checks.insert(0, {"name": "syntax", "status": "passed", "required": True})

    # 4. 复现测试与回归测试
    repro = list(state.get("repro_tests") or [])
    all_tests = [p for p in repo_map.get("test_files", []) if p not in repro]
    profile = state.get("profile") or "change"

    if not repro and not all_tests:
        if profile == "bugfix" and policy.require_reproduction:
            report = ValidationReport(
                status=SandboxStatus.failed,
                commands=[],
                repro_passed=False,
                regression_passed=False,
                error="缺少复现测试",
                tree_hash=current_hash,
                checks=checks,
            )
            return _validation_failure_update(state, report, current_hash, changed)
        report = ValidationReport(
            status=SandboxStatus.passed if checks_ok else SandboxStatus.failed,
            commands=[],
            repro_passed=True,
            regression_passed=True,
            error="" if checks_ok else "required 检查失败",
            checks=checks + [{"name": "tests", "status": "skipped", "reason": "仓库无可运行测试"}],
            logs="无可运行测试，语法与策略检查通过" if checks_ok else "required 检查失败",
            tree_hash=current_hash,
        )
    elif not repro:
        run = run_sandbox_command(ctx, state, root, all_tests)
        if run.status == SandboxStatus.infra_error:
            status = SandboxStatus.infra_error
        else:
            status = SandboxStatus.passed if (run.exit_code == 0 and checks_ok) else SandboxStatus.failed
        report = ValidationReport(
            status=status,
            commands=[],
            repro_passed=True,
            regression_passed=(run.exit_code == 0),
            logs=run.stdout[-8000:],
            error=getattr(run, "infra_error", "") or ("" if checks_ok else "required 检查失败"),
            tree_hash=current_hash,
            checks=checks + [{"name": "tests", "status": "passed" if run.exit_code == 0 else "failed"}],
        )
    else:
        tasks = [
            Task("repro", "verify_gate", lambda: run_sandbox_command(ctx, state, root, repro),
                 timeout_sec=ctx.cfg.sandbox_timeout_sec + 30),
        ]
        if all_tests:
            tasks.append(
                Task("regression", "verify_gate", lambda: run_sandbox_command(ctx, state, root, all_tests),
                     timeout_sec=ctx.cfg.sandbox_timeout_sec + 30)
            )
        outcome = ctx.coordinator.run_stage(tasks)
        repro_res = outcome.results.get("repro").value if outcome.results.get("repro") else None
        if repro_res is None:
            repro_res = SandboxResult(status=SandboxStatus.infra_error, infra_error="repro 任务失败")
        if all_tests:
            reg_res = outcome.results.get("regression").value if outcome.results.get("regression") else None
            if reg_res is None:
                reg_res = SandboxResult(status=SandboxStatus.infra_error, infra_error="regression 任务失败")
            report = validation_from_results(repro=repro_res, regression=reg_res, tree_hash=current_hash)
        else:
            report = ValidationReport(
                status=(
                    SandboxStatus.passed
                    if repro_res.status == SandboxStatus.passed
                    else (
                        SandboxStatus.infra_error
                        if repro_res.status == SandboxStatus.infra_error
                        else SandboxStatus.failed
                    )
                ),
                commands=[],
                repro_passed=(repro_res.status == SandboxStatus.passed),
                regression_passed=True,
                logs=repro_res.stdout[-8000:],
                error=repro_res.infra_error,
                tree_hash=current_hash,
            )
        if not checks_ok and report.status == SandboxStatus.passed:
            report = report.model_copy(update={"status": SandboxStatus.failed, "error": "required 检查失败"})
        report = report.model_copy(update={"checks": checks})

    emit_event(ctx, job_id, "verify_gate_done", {"status": report.status.value})
    if report.status == SandboxStatus.infra_error:
        return {
            "validation_report": report.model_dump(mode="json"),
            "status": "verify_infra_error",
        }
    if report.status == SandboxStatus.failed:
        return _validation_failure_update(state, report, current_hash, changed)

    # 通过 Verify Gate：重新计算风险等级（单调只升不降）并打上 verified_tree_hash 邮戳
    stats = state.get("diff_stats") or {}
    pr = state.get("pr_context") or {}
    recomputed = classify_tier(
        policy,
        changed_paths=changed,
        changed_lines=int(stats.get("lines", 0)),
        author=str(state.get("author") or ""),
        pr_body=str(state.get("pr_body") or ""),
        labels=list(pr.get("labels") or []),
    )
    recomputed_level = TIER_TO_IMPORTANCE.get(recomputed.tier.value, "medium")
    merged_level = max_importance(state.get("importance_level"), recomputed_level)
    mapped_tier = IMPORTANCE_TO_TIER[merged_level]
    prev_risk = dict(state.get("risk_tier") or {})
    min_approvals = max(policy.min_approvals(mapped_tier), IMPORTANCE_APPROVALS[merged_level])
    if merged_level == "low" and policy.merge.allow_zero_human:
        min_approvals = 0
    updated_risk = RiskTier(
        tier=Tier(mapped_tier),
        reasons=list(dict.fromkeys(list(prev_risk.get("reasons") or []) + list(recomputed.reasons))),
        min_human_approvals=min_approvals,
        require_non_author=(merged_level == "critical") or bool(prev_risk.get("require_non_author")),
        depth=policy.depth_for(mapped_tier),
        trusted_author=bool(prev_risk.get("trusted_author", recomputed.trusted_author)),
        agent_authored=bool(prev_risk.get("agent_authored", recomputed.agent_authored)),
    )

    escalation = str(state.get("escalation_reason") or "")
    max_reviews = int(getattr(ctx.cfg, "max_review_rounds", 5) or 5)
    if (
        state.get("candidate") == "fix"
        and state.get("reviewed_tree_hash") != current_hash
        and int(state.get("review_round", 0) or 0) >= max_reviews
    ):
        escalation = "达到最大审查轮次上限，最终修复尚未经复审，需人工确认"

    return {
        "validation_report": report.model_dump(mode="json"),
        "verified_tree_hash": current_hash,
        "importance_level": merged_level,
        "risk_tier": updated_risk.model_dump(mode="json"),
        "escalation_reason": escalation,
        "status": "verified_passed",
        "events": [{"event": "verify_gate_passed", "tree_hash": current_hash[:12], "timestamp": now_iso()}],
    }


def _validation_failure_update(
    state: ReviewState,
    report: ValidationReport,
    current_hash: str,
    changed: list[str],
) -> dict[str, Any]:
    """当 Verify Gate 失败时构造或补充 FixDirective，便于打回 Coder 精准修复。"""
    raw_dir = state.get("fix_directive") or {}
    directive = FixDirective.model_validate(raw_dir) if raw_dir else FixDirective()
    if not directive.needs_modification or not directive.target_findings:
        src_files = [p for p in changed if not is_test_path(p)]
        if not src_files:
            src_files = list((state.get("code_partition") or {}).get("source_files") or [])
        directive = FixDirective(
            directive_id=f"val_{current_hash[:10]}",
            round_no=int(state.get("fix_round", 0)) + 1,
            needs_modification=True,
            target_findings=[{
                "directive_item_id": "VAL-001",
                "category": "tests",
                "severity": "major",
                "blocking": True,
                "path": src_files[0] if src_files else "",
                "message": report.error or "Verify Gate（语法/Lint/测试）失败，需定位根因并修复",
                "evidence": (report.logs or report.error or "")[:800],
            }],
            allowed_source_files=src_files,
            allowed_test_addition_files=[],
            acceptance_criteria=["修复后通过 Verify Gate 的语法、Lint 与全部测试"],
            summary="Verify Gate 失败自动生成修复指令",
        )
    return {
        "validation_report": report.model_dump(mode="json"),
        "fix_directive": directive.model_dump(mode="json"),
        "status": "verify_failed",
        "events": [{"event": "verify_gate_failed", "error": report.error[:120], "timestamp": now_iso()}],
    }


__all__ = ["coder_node", "qa_checker_node", "verify_gate_node"]
