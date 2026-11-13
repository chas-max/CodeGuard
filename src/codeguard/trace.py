"""全链路追踪：把 LangGraph 检查点历史还原为「节点级时间线」。

约定：
- 输入是已按 step 升序排列的快照列表，每个快照包含 values / next / created_at。
- 某个快照的 `next` 指向「即将执行的节点」，因此 `states[i].next[0]` 就是产出
  `states[i+1]` 的节点。
- 输出只做展示用途的摘要，不参与任何门控判定。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

NODE_ORDER = [
    "ingest", "preflight", "hybrid_scan", "verify_issue", "validate",
    "review", "fix", "merge_gate", "human_approval", "merge", "finalize",
]

NODE_LABELS = {
    "ingest": "接入",
    "preflight": "预检",
    "hybrid_scan": "混合检测",
    "verify_issue": "意图/复现验证",
    "validate": "沙箱验证",
    "review": "审查",
    "fix": "修复",
    "merge_gate": "合并门控",
    "human_approval": "人工审批",
    "merge": "合并",
    "finalize": "收尾",
}

NODE_DESCRIPTIONS = {
    "ingest": "接收 PR 事件并登记任务",
    "preflight": "校验 PR、工作区与策略，做确定性风险分级",
    "hybrid_scan": "静态/规则/语义混合检测并合并 findings",
    "verify_issue": "解析 PR 意图，bugfix 在 base 上复现失败",
    "validate": "在 Docker 沙箱中运行复现与回归测试",
    "review": "唯一 reviewer 做整体审查并给出结论",
    "fix": "修复 Agent 生成补丁并推回 head 分支",
    "merge_gate": "确定性门控：验证/审查/审批/树哈希一致性",
    "human_approval": "人工介入：审批、驳回或处置 findings",
    "merge": "通过 GitHub merge API 合并",
    "finalize": "写 job record、发布 review 与指标",
}

# 只用于展示的「变更键」，避免把大文本字段刷到时间线上。
_IGNORED_CHANGE_KEYS = {
    "diff_text", "pr_body", "pr_context", "run_record", "policy",
    "repo_map", "review_guidance", "repro_evidence", "events",
    "finding_history", "task_events", "tool_events", "errors", "budget_events",
    "fix_history", "agent_issues", "open_issues", "changed_files",
}


def parse_ts(value: Any) -> _dt.datetime | None:
    if not value:
        return None
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


def _duration_ms(start: str | None, end: str | None) -> int | None:
    a, b = parse_ts(start), parse_ts(end)
    if not a or not b:
        return None
    return max(0, int((b - a).total_seconds() * 1000))


def _changed_keys(before: dict, after: dict) -> list[str]:
    keys: list[str] = []
    for key, value in after.items():
        if key in _IGNORED_CHANGE_KEYS:
            continue
        if before.get(key) != value:
            keys.append(key)
    return keys


def _count(value: Any) -> int:
    return len(value) if isinstance(value, (list, dict, tuple)) else 0


def node_summary(node: str, values: dict) -> str:
    """给单个节点生成一句话结论，用于时间线折叠展示。"""
    v = values or {}
    if node == "ingest":
        return f"{v.get('repo', '')}#{v.get('pr_number', '')} · {v.get('pr_title', '')}"[:120]
    if node == "preflight":
        risk = v.get("risk_tier") or {}
        files = _count(v.get("changed_files"))
        profile = v.get("profile") or "-"
        return f"tier={risk.get('tier', '-')} · profile={profile} · 文件 {files}"
    if node == "hybrid_scan":
        findings = v.get("findings") or []
        blocking = sum(1 for item in findings if item.get("blocking"))
        return f"findings {len(findings)} · blocking {blocking}"
    if node == "verify_issue":
        issue = v.get("issue_verification") or {}
        intent = v.get("intent_verification") or {}
        if issue:
            return f"复现验证 {issue.get('status', '-')} · confidence={issue.get('confidence', 0)}"
        if intent:
            return f"意图验证 {intent.get('status', '-')}（advisory）"
        return "无验证结论"
    if node == "validate":
        report = v.get("validation_report") or {}
        return (
            f"沙箱 {report.get('status', '-')} · repro={report.get('repro_passed')} "
            f"· regression={report.get('regression_passed')}"
        )
    if node == "review":
        decision = v.get("review_decision") or {}
        return f"verdict={decision.get('verdict', '-')} · round={v.get('review_round', 0)}"
    if node == "fix":
        files = v.get("fix_files") or []
        return f"round={v.get('fix_round', 0)} · 文件 {len(files)}"
    if node == "merge_gate":
        decision = v.get("merge_decision") or {}
        return f"outcome={decision.get('outcome', '-')} · {decision.get('reason', '')}"[:120]
    if node == "human_approval":
        decision = v.get("human_decision") or {}
        if not decision:
            return "等待人工介入"
        state = "通过" if decision.get("approved") else "驳回"
        return f"{state} · approver={decision.get('approver', '-')}"
    if node == "merge":
        return f"sha={(v.get('merge_commit_sha') or '-')[:10]}"
    if node == "finalize":
        return f"outcome={v.get('outcome') or v.get('status') or '-'}"
    return ""


def _step_tool_events(before: dict, after: dict) -> list[dict]:
    before_events = list(before.get("tool_events") or [])
    after_events = list(after.get("tool_events") or [])
    if len(after_events) >= len(before_events) and after_events[:len(before_events)] == before_events:
        return after_events[len(before_events):]
    diff = len(after_events) - len(before_events)
    if diff > 0:
        return after_events[-diff:]
    return []


def build_trace(states: list[dict]) -> list[dict]:
    """states 需按 step 升序；返回按执行顺序排列的节点步骤。"""
    ordered = sorted(
        [s for s in states if isinstance(s, dict)],
        key=lambda s: (s.get("step") if isinstance(s.get("step"), int) else 0),
    )
    steps: list[dict] = []
    for index in range(len(ordered) - 1):
        current, following = ordered[index], ordered[index + 1]
        pending = [item for item in (current.get("next") or []) if not str(item).startswith("__")]
        if not pending:
            continue
        node = str(pending[0])
        before = current.get("values") or {}
        after = following.get("values") or {}
        step_events = _step_tool_events(before, after)
        steps.append({
            "index": len(steps) + 1,
            "node": node,
            "label": NODE_LABELS.get(node, node),
            "description": NODE_DESCRIPTIONS.get(node, ""),
            "started_at": current.get("created_at", ""),
            "finished_at": following.get("created_at", ""),
            "duration_ms": _duration_ms(current.get("created_at"), following.get("created_at")),
            "summary": node_summary(node, after),
            "changed_keys": _changed_keys(before, after),
            "next": list(following.get("next") or []),
            "tool_events": step_events,
            "tool_calls_count": len(step_events),
            "mcp_calls_count": sum(1 for ev in step_events if ev.get("source") == "mcp"),
        })

    if ordered:
        last = ordered[-1]
        pending = [item for item in (last.get("next") or []) if not str(item).startswith("__")]
        if pending:
            # 图已跑到某个节点但还没有产出快照：视为「正在执行」。
            node = str(pending[0])
            steps.append({
                "index": len(steps) + 1,
                "node": node,
                "label": NODE_LABELS.get(node, node),
                "description": NODE_DESCRIPTIONS.get(node, ""),
                "started_at": last.get("created_at", ""),
                "finished_at": "",
                "duration_ms": None,
                "summary": "",
                "changed_keys": [],
                "next": pending,
                "running": True,
                "tool_events": [],
                "tool_calls_count": 0,
                "mcp_calls_count": 0,
            })
    return steps


def current_node(states: list[dict]) -> str:
    if not states:
        return ""
    last = max(states, key=lambda s: s.get("step") if isinstance(s.get("step"), int) else 0)
    pending = [item for item in (last.get("next") or []) if not str(item).startswith("__")]
    return str(pending[0]) if pending else ""


__all__ = [
    "NODE_ORDER", "NODE_LABELS", "NODE_DESCRIPTIONS",
    "build_trace", "current_node", "node_summary", "parse_ts",
]
