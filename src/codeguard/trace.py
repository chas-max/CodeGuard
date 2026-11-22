"""全链路追踪：把 LangGraph 检查点历史还原为「节点级时间线」。

约定：
- 输入是已按 step 升序排列的快照列表，每个快照包含 values / next / created_at。
- 某个快照的 `next` 指向「即将执行的节点」（在 4 路并行审查阶段可包含多个分支节点）。
- 输出只做展示用途的摘要，不参与任何门控判定。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

NODE_ORDER = [
    "prepare_context",
    "pr_reader",
    "dispatch_reviewers",
    "review_logic",
    "review_security",
    "review_architecture",
    "review_tests",
    "synthesize_findings",
    "coder_agent",
    "qa_checker",
    "verify_gate",
    "summary_and_submit",
    "merge_gate",
    "human_approval",
    "merge",
    "persist_memory_and_finalize",
]

NODE_LABELS = {
    "prepare_context": "前置准备",
    "pr_reader": "PR 理解",
    "dispatch_reviewers": "并行分发",
    "review_logic": "逻辑审查",
    "review_security": "安全审查",
    "review_architecture": "架构审查",
    "review_tests": "测试审查",
    "synthesize_findings": "规则综合",
    "coder_agent": "代码修复",
    "qa_checker": "QA 对齐",
    "verify_gate": "前置验证",
    "summary_and_submit": "总结提交",
    "merge_gate": "合并门控",
    "human_approval": "人工审批",
    "merge": "合并",
    "persist_memory_and_finalize": "记忆落库",
    # 兼容历史节点展示标签
    "ingest": "接入",
    "preflight": "预检",
    "hybrid_scan": "混合检测",
    "verify_issue": "意图/复现验证",
    "validate": "沙箱验证",
    "review": "审查",
    "fix": "修复",
    "finalize": "收尾",
}

NODE_DESCRIPTIONS = {
    "prepare_context": "并行搭建 RepoMap、base 分支策略、文件分区与静态/规则扫描",
    "pr_reader": "结构化解析 PR 意图与改动类型，结合下限确定性单调分级",
    "dispatch_reviewers": "分发 4 个并行审查视角并注入各自独立短期记忆",
    "review_logic": "审查逻辑错误、状态流转与边界处理",
    "review_security": "审查安全规范、鉴权漏洞与静态漏检",
    "review_architecture": "审查项目规范、模块职责与架构设计",
    "review_tests": "审查测试覆盖、断言质量与回归风险",
    "synthesize_findings": "确定性坐标对齐、去重，并签发 FixDirective 契约",
    "coder_agent": "结合程序化记忆 RAG 与 Docker 沙箱执行定向修复",
    "qa_checker": "核对 Coder 实际 diff 与 FixDirective 是否一致、未偏题",
    "verify_gate": "在提交与合并前跑 AST/lint/策略/单测并触发多轮增量复审",
    "summary_and_submit": "提交更新 PR（继承不降级的重要等级）并发布结构化摘要",
    "merge_gate": "确定性门控：tree hash/SHA 绑定、QA/Verify 全绿、分级审批",
    "human_approval": "按重要等级 (low=0, medium=1, critical=2) 中断收集人工审批",
    "merge": "通过 GitHub Merge API 完成合并",
    "persist_memory_and_finalize": "将已验证的审查修复经验写入本地 sqlite-vec 向量库并生成终态报告",
}

# 只用于展示的「变更键」，避免把大文本字段刷到时间线上。
_IGNORED_CHANGE_KEYS = {
    "diff_text", "pr_body", "pr_context", "run_record", "policy",
    "repo_map", "review_guidance", "repro_evidence", "events",
    "finding_history", "task_events", "tool_events", "errors", "budget_events",
    "fix_history", "qa_history", "agent_issues", "open_issues", "changed_files",
    "reviewer_memories",
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
    if node == "prepare_context":
        risk = v.get("risk_tier") or {}
        files = _count(v.get("changed_files"))
        det = len(v.get("findings") or [])
        return f"level={v.get('importance_level', '-')} · tier={risk.get('tier', '-')} · 文件 {files} · 静态问题 {det}"
    if node == "pr_reader":
        lvl = v.get("importance_level", "-")
        iv = v.get("issue_verification") or {}
        repro = f" · 复现={iv.get('status')}" if iv else ""
        return f"level={lvl} · profile={v.get('profile', '-')}{repro}"
    if node == "dispatch_reviewers":
        return f"第 {int(v.get('review_round', 0)) + 1} 轮 · {v.get('review_round_mode', 'full')}"
    if node in ("review_logic", "review_security", "review_architecture", "review_tests"):
        out = (v.get("reviewer_outputs") or {}).get(node) or {}
        issues = _count(out.get("findings"))
        flag = "需修改" if out.get("needs_modification") else "通过"
        return f"{flag} · issues={issues} · {out.get('summary', '')}"[:120]
    if node == "synthesize_findings":
        findings = v.get("findings") or []
        blocking = sum(1 for item in findings if item.get("blocking"))
        directive = v.get("fix_directive") or {}
        need = "需修复" if directive.get("needs_modification") else "干净"
        return f"{need} · findings {len(findings)} · blocking {blocking}"
    if node == "coder_agent":
        files = v.get("fix_files") or []
        return f"round={v.get('fix_round', 0)} · 修改 {len(files)} 个文件"
    if node == "qa_checker":
        report = v.get("qa_report") or {}
        return f"verdict={report.get('verdict', '-')} · {report.get('summary', '')}"[:120]
    if node == "verify_gate":
        report = v.get("validation_report") or {}
        return f"沙箱 {report.get('status', '-')} · regression={report.get('regression_passed')}"
    if node == "summary_and_submit":
        summary = v.get("review_summary") or {}
        mode = summary.get("submitted_mode", "comment")
        return f"level={v.get('importance_level', '-')} · mode={mode}"
    if node == "merge_gate":
        decision = v.get("merge_decision") or {}
        return f"outcome={decision.get('outcome', '-')} · {decision.get('reason', '')}"[:120]
    if node == "human_approval":
        decision = v.get("human_decision") or {}
        if not decision:
            return "等待人工介入"
        state_text = "通过" if decision.get("approved") else "驳回"
        return f"{state_text} · approver={decision.get('approver', '-')}"
    if node == "merge":
        return f"sha={(v.get('merge_commit_sha') or '-')[:10]}"
    if node == "persist_memory_and_finalize":
        mems = _count(v.get("memory_records_written"))
        return f"outcome={v.get('outcome') or v.get('status') or '-'} · 记忆入库 {mems} 条"

    # 兼容历史节点摘要
    if node == "ingest":
        return f"{v.get('repo', '')}#{v.get('pr_number', '')} · {v.get('pr_title', '')}"[:120]
    if node == "preflight":
        risk = v.get("risk_tier") or {}
        files = _count(v.get("changed_files"))
        return f"tier={risk.get('tier', '-')} · profile={v.get('profile') or '-'} · 文件 {files}"
    if node == "hybrid_scan":
        findings = v.get("findings") or []
        blocking = sum(1 for item in findings if item.get("blocking"))
        return f"findings {len(findings)} · blocking {blocking}"
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
    """states 需按 step 升序；返回按执行顺序排列的节点步骤（支持并行扇出拆步）。"""
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
        before = current.get("values") or {}
        after = following.get("values") or {}
        step_events = _step_tool_events(before, after)
        parallel = len(pending) > 1
        changed = _changed_keys(before, after)
        duration = _duration_ms(current.get("created_at"), following.get("created_at"))
        for raw_node in pending:
            node = str(raw_node)
            events_for_node = (
                [ev for ev in step_events if ev.get("role") == node]
                if parallel
                else step_events
            )
            steps.append({
                "index": len(steps) + 1,
                "node": node,
                "label": NODE_LABELS.get(node, node),
                "description": NODE_DESCRIPTIONS.get(node, ""),
                "started_at": current.get("created_at", ""),
                "finished_at": following.get("created_at", ""),
                "duration_ms": duration,
                "summary": node_summary(node, after),
                "changed_keys": changed,
                "next": list(following.get("next") or []),
                "tool_events": events_for_node,
                "tool_calls_count": len(events_for_node),
                "mcp_calls_count": sum(1 for ev in events_for_node if ev.get("source") == "mcp"),
                "parallel": parallel,
            })

    if ordered:
        last = ordered[-1]
        pending = [item for item in (last.get("next") or []) if not str(item).startswith("__")]
        if pending:
            for raw_node in pending:
                node = str(raw_node)
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
