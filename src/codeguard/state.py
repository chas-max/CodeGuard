"""LangGraph 全图状态 schema 与 reducers。

原则：
- 所有节点可能写入的 key 必须在此声明；LangGraph 会静默丢弃未声明键。
- job 级可变状态一律放 state，节点容器不持有跨 job 标志。
- 日志类列表使用有界 append，避免 SqliteSaver checkpoint 无限膨胀。
"""
from __future__ import annotations

import datetime
from typing import Annotated, Optional, TypedDict

from .models import PullRequestContext

MAX_LOG_ITEMS = 200


def _bounded_append(left: list | None, right: list | None, max_items: int) -> list:
    merged = (left or []) + (right or [])
    return merged[-max_items:]


def append_bounded(left: list | None, right: list | None) -> list:
    """单调追加，只保留最近 MAX_LOG_ITEMS 条（LangGraph reducer 必须是 2 参数）。"""
    return _bounded_append(left, right, MAX_LOG_ITEMS)


def merge_findings(left: list[dict] | None, right: list[dict] | None) -> list[dict]:
    """按 finding.id 幂等合并；后写入覆盖同 id。"""
    by_id: dict[str, dict] = {}
    for item in (left or []) + (right or []):
        if not isinstance(item, dict):
            continue
        key = item.get("id") or f"{item.get('rule_id', '')}:{item.get('path', '')}:{item.get('line', '')}"
        by_id[key] = {**by_id.get(key, {}), **item}
    return list(by_id.values())


def merge_files(left: list[dict] | None, right: list[dict] | None) -> list[dict]:
    """按 path 幂等合并文件变更。"""
    by_path: dict[str, dict] = {}
    for item in (left or []) + (right or []):
        if isinstance(item, dict) and item.get("path"):
            by_path[item["path"]] = {**by_path.get(item["path"], {}), **item}
    return list(by_path.values())


class ReviewState(TypedDict, total=False):
    # 身份 / profile
    job_id: str
    delivery_id: str
    repo: str
    pr_number: int
    base_ref: str
    base_sha: str
    head_sha: str
    author: str
    is_fork: bool
    pr_title: str
    pr_body: str
    profile: str
    started_at: str
    finished_at: str

    # 预检 / 工作区 / 风险分级
    pr_context: dict
    changed_files: Annotated[list[dict], merge_files]
    diff_text: str
    diff_stats: dict
    repo_map: dict
    policy: dict
    policy_sha: str
    risk_tier: Optional[dict]
    review_guidance: str
    resolved_branch: str
    repo_root: str
    base_root: str
    workspace_dir: str
    dry_run: bool

    # 混合检测
    deterministic_report: Optional[dict]
    semantic_report: Optional[dict]
    semantic_unavailable: bool
    hybrid_scan_report: Optional[dict]
    findings: list[dict]
    finding_history: Annotated[list[dict], append_bounded]
    blocking_findings: list[dict]
    candidate: str  # pr | fix

    # 问题 / 意图验证
    claim: Optional[dict]
    issue_verification: Optional[dict]
    intent_verification: Optional[dict]
    repro_tests: list[str]
    repro_evidence: Annotated[list[dict], append_bounded]

    # Docker 验证
    validation_report: Optional[dict]
    reviewed_sha: str
    reviewed_tree_hash: str

    # 审查（唯一 reviewer）
    review_decision: Optional[dict]
    review_round: int
    review_round_mode: str
    escalation_reason: str

    # 修复
    fix_round: int
    fix_patch: str
    fix_files: list[str]
    fix_tree_hash: str
    fix_pushed: bool
    fix_remote_sha: str
    fix_history: Annotated[list[dict], append_bounded]

    # 合并 / 终态
    merge_decision: Optional[dict]
    outcome: Optional[str]
    merge_commit_sha: str
    requires_human: bool
    human_decision: Optional[dict]
    human_approvals: list[dict]
    finding_outcomes: dict

    # 协调 / 可观测 / 成本
    task_results: dict
    task_events: Annotated[list[dict], append_bounded]
    tool_events: Annotated[list[dict], append_bounded]
    tool_calls: int
    events: Annotated[list[dict], append_bounded]
    agent_issues: Annotated[list[dict], append_bounded]
    open_issues: list[dict]
    errors: Annotated[list[dict], append_bounded]
    budget_events: Annotated[list[dict], append_bounded]
    budget_exceeded: bool
    status: str
    error: Optional[str]
    run_record: dict

    # 计数器
    verify_round: int
    infra_retry: int
    llm_calls: int


# 兼容旧测试/导入的别名（重构后逐渐移除）
CodeGuardState = ReviewState


def initial_state(pr: PullRequestContext | dict, job_id: str, **overrides) -> ReviewState:
    """构造图初始状态。pr 可以是模型或 dict。"""
    ctx = pr if isinstance(pr, PullRequestContext) else PullRequestContext.model_validate(pr)
    state: ReviewState = {
        "job_id": job_id,
        "delivery_id": "",
        "repo": ctx.repo,
        "pr_number": ctx.number,
        "base_ref": ctx.base_ref,
        "base_sha": ctx.base_sha,
        "head_sha": ctx.head_sha,
        "author": ctx.author,
        "is_fork": ctx.is_fork,
        "pr_title": ctx.title,
        "pr_body": ctx.body,
        "profile": ctx.profile or "",
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "finished_at": "",
        "pr_context": ctx.model_dump(mode="json"),
        "changed_files": [f.model_dump(mode="json") for f in ctx.changed_files],
        "diff_text": "",
        "diff_stats": {},
        "repo_map": {},
        "policy": {},
        "policy_sha": "",
        "risk_tier": None,
        "review_guidance": "",
        "resolved_branch": "",
        "repo_root": "",
        "base_root": "",
        "workspace_dir": "",
        "dry_run": False,
        "deterministic_report": None,
        "semantic_report": None,
        "semantic_unavailable": False,
        "hybrid_scan_report": None,
        "findings": [],
        "finding_history": [],
        "blocking_findings": [],
        "candidate": "pr",
        "claim": None,
        "issue_verification": None,
        "intent_verification": None,
        "repro_tests": [],
        "repro_evidence": [],
        "validation_report": None,
        "reviewed_sha": "",
        "reviewed_tree_hash": "",
        "review_decision": None,
        "review_round": 0,
        "review_round_mode": "full",
        "escalation_reason": "",
        "fix_round": 0,
        "fix_patch": "",
        "fix_files": [],
        "fix_tree_hash": "",
        "fix_pushed": False,
        "fix_remote_sha": "",
        "fix_history": [],
        "merge_decision": None,
        "outcome": None,
        "merge_commit_sha": "",
        "requires_human": False,
        "human_decision": None,
        "human_approvals": [],
        "finding_outcomes": {},
        "task_results": {},
        "task_events": [],
        "tool_events": [],
        "tool_calls": 0,
        "events": [],
        "agent_issues": [],
        "open_issues": [],
        "errors": [],
        "budget_events": [],
        "budget_exceeded": False,
        "status": "pending",
        "error": None,
        "run_record": {},
        "verify_round": 0,
        "infra_retry": 0,
        "llm_calls": 0,
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


__all__ = [
    "ReviewState", "CodeGuardState", "initial_state",
    "append_bounded", "merge_findings", "merge_files",
]
