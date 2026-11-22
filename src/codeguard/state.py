"""LangGraph 全图状态 schema 与并行安全 reducers。

原则：
- 所有节点可能写入的 key 必须在此声明；LangGraph 会静默丢弃未声明键。
- job 级可变状态一律放 state，节点容器不持有跨 job 标志。
- 并行分支写入的字典与计数必须使用幂等/累加 reducer，避免并发覆盖冲突。
- 日志类列表使用有界 append，避免 SqliteSaver checkpoint 无限膨胀。
"""
from __future__ import annotations

import datetime
from typing import Annotated, Any, Optional, TypedDict

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


def merge_dict_by_key(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """按顶层 key 合并字典，供 4 路并行 Reviewer 分支并发写入自身输出与短期记忆。

    当 `right` 携带 `{"__reset__": True, ...}` 时，重置为新字典（用于新一轮分发清空当轮输出）。
    """
    if isinstance(right, dict) and right.get("__reset__"):
        return {k: v for k, v in right.items() if k != "__reset__"}
    merged = dict(left or {})
    if isinstance(right, dict):
        for key, value in right.items():
            if key == "__reset__":
                continue
            merged[key] = value
    return merged


def add_int(left: int | None, right: int | None) -> int:
    """整型增量累加 reducer：节点只需返回本步增量 delta。"""
    return int(left or 0) + int(right or 0)


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

    # 预检 / 前置知识 / PR 拆解 / 单调风险分级
    pr_context: dict
    changed_files: Annotated[list[dict], merge_files]
    code_partition: dict
    pr_brief: Optional[dict]
    diff_text: str
    active_diff: str
    diff_stats: dict
    repo_map: dict
    policy: dict
    policy_sha: str
    risk_tier: Optional[dict]
    importance_level: str
    inherited_importance_level: str
    review_guidance: str
    resolved_branch: str
    repo_root: str
    base_root: str
    workspace_dir: str
    dry_run: bool

    # 前置静态检测 & 综合问题快照
    deterministic_report: Optional[dict]
    semantic_report: Optional[dict]
    semantic_unavailable: bool
    hybrid_scan_report: Optional[dict]
    findings: list[dict]
    finding_history: Annotated[list[dict], append_bounded]
    blocking_findings: list[dict]
    candidate: str  # pr | fix

    # 意图/复现字段（兼容保留）
    claim: Optional[dict]
    issue_verification: Optional[dict]
    intent_verification: Optional[dict]
    repro_tests: list[str]
    repro_evidence: Annotated[list[dict], append_bounded]

    # Docker 验证（verify_gate）
    validation_report: Optional[dict]
    reviewed_sha: str
    reviewed_tree_hash: str
    verified_tree_hash: str

    # 4 视角并行审查 & 各自独立短期记忆
    reviewer_memories: Annotated[dict[str, dict], merge_dict_by_key]
    reviewer_outputs: Annotated[dict[str, dict], merge_dict_by_key]
    review_decision: Optional[dict]
    review_round: int
    review_round_mode: str
    escalation_reason: str

    # Coder 修复 & QA Checker 防偏航监督
    fix_directive: Optional[dict]
    fix_result: Optional[dict]
    qa_report: Optional[dict]
    qa_history: Annotated[list[dict], append_bounded]
    fix_round: int
    fix_patch: str
    fix_files: list[str]
    fix_tree_hash: str
    fix_pushed: bool
    fix_remote_sha: str
    fix_history: Annotated[list[dict], append_bounded]

    # PR 提交总结 / 合并 / 分级人工审批 / 向量记忆落库
    review_summary: Optional[dict]
    submitted_pr_number: Optional[int]
    memory_records_written: Annotated[list[str], append_bounded]
    merge_decision: Optional[dict]
    outcome: Optional[str]
    merge_commit_sha: str
    requires_human: bool
    human_decision: Optional[dict]
    human_approvals: list[dict]
    finding_outcomes: dict

    # 协调 / 可观测 / 成本（并行分支安全累加）
    task_results: dict
    task_events: Annotated[list[dict], append_bounded]
    tool_events: Annotated[list[dict], append_bounded]
    tool_calls: Annotated[int, add_int]
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
    llm_calls: Annotated[int, add_int]


# 兼容旧测试/导入的别名
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
        "code_partition": {},
        "pr_brief": None,
        "diff_text": "",
        "active_diff": "",
        "diff_stats": {},
        "repo_map": {},
        "policy": {},
        "policy_sha": "",
        "risk_tier": None,
        "importance_level": "medium",
        "inherited_importance_level": "medium",
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
        "verified_tree_hash": "",
        "reviewer_memories": {},
        "reviewer_outputs": {},
        "review_decision": None,
        "review_round": 0,
        "review_round_mode": "full",
        "escalation_reason": "",
        "fix_directive": None,
        "fix_result": None,
        "qa_report": None,
        "qa_history": [],
        "fix_round": 0,
        "fix_patch": "",
        "fix_files": [],
        "fix_tree_hash": "",
        "fix_pushed": False,
        "fix_remote_sha": "",
        "fix_history": [],
        "review_summary": None,
        "submitted_pr_number": None,
        "memory_records_written": [],
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
    "merge_dict_by_key", "add_int",
]
