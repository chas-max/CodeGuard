"""LangGraph 节点公共辅助函数：工作区、沙箱、权限工具箱与增量成本统计。"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from ..engine.diff import tree_hash, unified_diff
from ..engine.policy import Policy
from ..engine.repo_map import is_test_path
from ..engine.rules import CI_PATTERNS, DEPENDENCY_PATTERNS
from ..engine.policy import matches_any
from ..models import CodePartition, SandboxStatus
from ..sandbox.models import SandboxResult, SandboxSpec
from ..state import ReviewState
from ..tools.spec import ToolLimits

_DOC_EXTS = (".md", ".rst", ".txt", ".adoc")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def emit_event(ctx: Any, job_id: str, event: str, data: dict | None = None) -> None:
    if getattr(ctx, "rt", None):
        ctx.rt.emit_event(job_id, event, data or {})


def update_progress(ctx: Any, job_id: str, text: str, pct: int) -> None:
    if getattr(ctx, "rt", None):
        ctx.rt.update_progress(job_id, text, pct)


def make_issue(agent: str, kind: str, detail: str) -> dict[str, Any]:
    return {"agent": agent, "issue_type": kind, "detail": detail, "timestamp": now_iso()}


def cost_update(state: ReviewState, costs: list[dict], cfg: Any) -> dict[str, Any]:
    """返回本节点 LLM 成本增量与 delta llm_calls（适配并行 add_int reducer）。"""
    if not costs:
        return {}
    total = sum(float(e.get("cost_cny", 0.0)) for e in state.get("budget_events", []))
    added = sum(float(c.get("cost_cny", 0.0)) for c in costs)
    exceeded = (total + added) >= float(getattr(cfg, "budget_max_cny", 10.0))
    update: dict[str, Any] = {
        "budget_events": costs,
        "llm_calls": len(costs),
    }
    if exceeded:
        update["budget_exceeded"] = True
        update["error"] = "budget_exceeded"
    return update


def parallel_cost_update(costs: list[dict]) -> dict[str, Any]:
    """供并行分支使用的安全成本更新（不触碰非 reducer 标量 budget_exceeded / error）。"""
    if not costs:
        return {}
    return {
        "budget_events": costs,
        "llm_calls": len(costs),
    }


def tool_update(events: list[dict] | None) -> dict[str, Any]:
    """返回本节点工具审计事件与 delta tool_calls（适配并行 add_int reducer）。"""
    if not events:
        return {}
    return {
        "tool_events": events,
        "tool_calls": len(events),
    }


def load_state_policy(state: ReviewState) -> Policy:
    raw = state.get("policy") or {}
    return Policy.model_validate(raw) if raw else Policy()


def repo_root_path(state: ReviewState) -> Path:
    raw = state.get("repo_root", "")
    if not raw:
        raise ValueError("repo_root 未设置")
    return Path(raw)


def base_root_path(state: ReviewState) -> Path:
    return Path(state.get("base_root") or state.get("repo_root", ""))


def safe_tree_hash(state: ReviewState) -> str:
    try:
        return tree_hash(repo_root_path(state))
    except Exception:  # noqa: BLE001
        return ""


def changed_paths_from_state(state: ReviewState) -> list[str]:
    return [f.get("path", "") for f in state.get("changed_files", []) if f.get("path")]


def tier_from_state(state: ReviewState) -> str:
    return str((state.get("risk_tier") or {}).get("tier") or "T1")


def cumulative_diff(
    base: Path,
    root: Path,
    paths: list[str],
    *,
    max_chars: int = 200_000,
) -> str:
    """计算 base 树到当前工作区的累计 unified diff。"""
    parts: list[str] = []
    for rel in dict.fromkeys(p for p in paths if p):
        old_path, new_path = base / rel, root / rel
        old = old_path.read_text(encoding="utf-8", errors="replace") if old_path.is_file() else ""
        new = new_path.read_text(encoding="utf-8", errors="replace") if new_path.is_file() else ""
        if old != new:
            parts.append(unified_diff(old, new, rel))
    return "\n".join(parts)[:max_chars]


def partition_changed_files(paths: list[str], policy: Policy | None = None) -> CodePartition:
    """将变更文件确定性拆分为源码、测试、配置依赖、文档与 CI/基础设施五类。"""
    source_files: list[str] = []
    test_files: list[str] = []
    config_dep_files: list[str] = []
    doc_files: list[str] = []
    ci_infra_files: list[str] = []
    other_files: list[str] = []

    for rel in dict.fromkeys(p for p in paths if p):
        lower = rel.lower()
        if is_test_path(rel):
            test_files.append(rel)
        elif matches_any(rel, CI_PATTERNS) or (policy and policy.is_protected(rel)):
            ci_infra_files.append(rel)
        elif matches_any(rel, DEPENDENCY_PATTERNS):
            config_dep_files.append(rel)
        elif lower.startswith("docs/") or lower.endswith(_DOC_EXTS):
            doc_files.append(rel)
        elif lower.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".rs", ".c", ".cpp")):
            source_files.append(rel)
        else:
            other_files.append(rel)

    return CodePartition(
        source_files=source_files,
        test_files=test_files,
        config_dep_files=config_dep_files,
        doc_files=doc_files,
        ci_infra_files=ci_infra_files,
        other_files=other_files,
    )


def tool_limits_for(policy: Policy, *, tier: str = "T1") -> ToolLimits:
    settings = policy.agents.tools
    max_steps = settings.max_steps
    max_calls = settings.max_tool_calls
    max_wall = settings.max_wall_sec
    if tier == "T0":
        max_steps = min(max_steps, 4)
        max_calls = min(max_calls, 4)
        max_wall = min(max_wall, 60)
    return ToolLimits(
        max_steps=max_steps,
        max_tool_calls=max_calls,
        max_wall_sec=max_wall,
        max_output_chars=settings.max_output_chars,
        sandbox_timeout_sec=settings.sandbox_timeout_sec,
        protocol=str(getattr(settings, "protocol", "auto") or "auto"),
    )


def pr_context_for_tools(state: ReviewState) -> dict[str, Any]:
    raw = dict(state.get("pr_context") or {})
    raw.setdefault("repo", state.get("repo", ""))
    raw.setdefault("pr_number", state.get("pr_number", 0))
    raw.setdefault("title", state.get("pr_title", ""))
    raw.setdefault("body", state.get("pr_body", ""))
    raw.setdefault("author", state.get("author", ""))
    raw.setdefault("base_ref", state.get("base_ref", ""))
    raw.setdefault("head_ref", state.get("resolved_branch", ""))
    raw.setdefault("tier", tier_from_state(state))
    if "changed_files" not in raw:
        raw["changed_files"] = [
            f.get("path", "")
            for f in (state.get("changed_files") or [])
            if isinstance(f, dict) and f.get("path")
        ]
    return raw


def build_role_toolbox(
    ctx: Any,
    role: str,
    state: ReviewState,
    root: Path | None,
    *,
    allowed_files: list[str] | None = None,
    allowed_test_addition_files: list[str] | None = None,
):
    """按角色/策略构造最小权限工具箱；不可用时安全返回 None。"""
    factory = getattr(ctx, "tools_for", None)
    if factory is None or root is None:
        return None
    policy = load_state_policy(state)
    base_raw = state.get("base_root")
    base_root = Path(base_raw) if base_raw else None
    kwargs: dict[str, Any] = {
        "root": root,
        "job_id": state.get("job_id", ""),
        "policy": policy,
        "tier": tier_from_state(state),
        "repo_map": state.get("repo_map") or {},
        "allowed_files": allowed_files,
        "allowed_test_addition_files": allowed_test_addition_files,
        "limits": tool_limits_for(policy, tier=tier_from_state(state)),
        "base_root": base_root,
        "pr_context": pr_context_for_tools(state),
        "findings": list(state.get("findings") or []),
        "memory_store": getattr(ctx, "memory_store", None),
    }
    try:
        return factory(role, **kwargs)
    except TypeError:
        # 兼容自定义 mock tools_for 签名
        for extra_key in ("allowed_test_addition_files", "memory_store"):
            kwargs.pop(extra_key, None)
        return factory(role, **kwargs)


def test_command(policy: Policy, tests: list[str]) -> str:
    joined = " ".join(tests)
    template = policy.sandbox.test_command_template or ""
    if "{tests}" in template:
        return template.replace("{tests}", joined).strip()
    commands = list(policy.sandbox.test_commands or ["python -m pytest -q"])
    first = commands[0] + ((" " + joined) if joined else "")
    return " && ".join([first] + commands[1:])


def run_sandbox_command(
    ctx: Any,
    state: ReviewState,
    root: Path,
    tests: list[str],
    *,
    command: str | None = None,
    network: str | None = None,
) -> SandboxResult:
    policy = load_state_policy(state)
    spec = SandboxSpec(
        job_id=state.get("job_id", ""),
        workspace=root,
        image=policy.sandbox.image,
        commands=[command or test_command(policy, tests)],
        timeout_sec=min(policy.sandbox.timeout_sec, ctx.cfg.sandbox_timeout_sec),
        network=network or "none",
        memory=ctx.cfg.sandbox_memory,
        cpus=ctx.cfg.sandbox_cpus,
        pids_limit=ctx.cfg.sandbox_pids,
    )
    try:
        return ctx.sandbox.run(spec)
    except Exception as exc:  # noqa: BLE001
        return SandboxResult(
            status=SandboxStatus.infra_error,
            exit_code=-1,
            infra_error=f"沙箱调用失败: {exc}",
        )


def run_prepare_commands(ctx: Any, state: ReviewState, root: Path) -> str:
    policy = load_state_policy(state)
    if not policy.sandbox.prepare_commands:
        return ""
    result = run_sandbox_command(
        ctx,
        state,
        root,
        [],
        command=" && ".join(policy.sandbox.prepare_commands),
        network=policy.sandbox.prepare_network,
    )
    if result.status == SandboxStatus.passed:
        return ""
    return result.infra_error or f"prepare 失败（exit={result.exit_code}）"


def run_policy_checks(ctx: Any, state: ReviewState, root: Path) -> tuple[list[dict], bool]:
    policy = load_state_policy(state)
    results: list[dict] = []
    required_ok = True
    for check in policy.sandbox.checks:
        result = run_sandbox_command(ctx, state, root, [], command=check.command)
        passed = result.status == SandboxStatus.passed
        results.append({
            "name": check.name,
            "status": "passed" if passed else "failed",
            "required": check.required,
            "logs": (result.stdout or "")[-2000:],
        })
        if check.required and not passed:
            required_ok = False
    return results, required_ok


def task_summary(outcome: Any) -> dict[str, Any]:
    return {
        tid: {
            "status": res.status.value,
            "error": res.error,
            "duration_ms": res.duration_ms,
        }
        for tid, res in outcome.results.items()
    }


__all__ = [
    "now_iso", "emit_event", "update_progress", "make_issue",
    "cost_update", "parallel_cost_update", "tool_update",
    "load_state_policy", "repo_root_path", "base_root_path", "safe_tree_hash",
    "changed_paths_from_state", "tier_from_state", "cumulative_diff",
    "partition_changed_files", "tool_limits_for", "pr_context_for_tools",
    "build_role_toolbox", "test_command", "run_sandbox_command",
    "run_prepare_commands", "run_policy_checks", "task_summary",
]
