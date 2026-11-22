"""LangGraph 状态机拓扑构建（CodeGuard v2：并行 4 路审查 + QA/Verify/Reviewer 5 轮双层闭环）。"""
from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .nodes import ReviewNodes
from .state import ReviewState

REVIEWER_BRANCH_NODES = (
    "review_logic",
    "review_security",
    "review_architecture",
    "review_tests",
)


def build_graph(nodes: ReviewNodes, checkpointer=None):
    """构建并编译 CodeGuard v2 状态图。"""
    g = StateGraph(ReviewState)

    # Node 1 & Node 2
    g.add_node("prepare_context", nodes.prepare_context)
    g.add_node("pr_reader", nodes.pr_reader)

    # Node 3 (并行扇出) & Node 4 (扇入综合)
    g.add_node("dispatch_reviewers", nodes.dispatch_reviewers)
    g.add_node("review_logic", nodes.review_logic)
    g.add_node("review_security", nodes.review_security)
    g.add_node("review_architecture", nodes.review_architecture)
    g.add_node("review_tests", nodes.review_tests)
    g.add_node("synthesize_findings", nodes.synthesize_findings)

    # Node 5 & Node 6 & Node 9 (前置验证门禁)
    g.add_node("coder_agent", nodes.coder_agent)
    g.add_node("qa_checker", nodes.qa_checker)
    g.add_node("verify_gate", nodes.verify_gate)

    # Node 7 & Node 8 & 终态程序化记忆持久化
    g.add_node("summary_and_submit", nodes.summary_and_submit)
    g.add_node("merge_gate", nodes.merge_gate)
    g.add_node("human_approval", nodes.human_approval)
    g.add_node("merge", nodes.merge)
    g.add_node("persist_memory_and_finalize", nodes.persist_memory_and_finalize)

    # 主流程边与条件路由
    g.add_edge(START, "prepare_context")
    g.add_conditional_edges(
        "prepare_context",
        nodes.route_after_prepare,
        {
            "pr_reader": "pr_reader",
            "persist_memory_and_finalize": "persist_memory_and_finalize",
        },
    )
    g.add_conditional_edges(
        "pr_reader",
        nodes.route_after_pr_reader,
        {
            "dispatch_reviewers": "dispatch_reviewers",
            "persist_memory_and_finalize": "persist_memory_and_finalize",
        },
    )

    # 并行扇出（dispatch_reviewers -> 4 路审查）与扇入（4 路审查 -> synthesize_findings）
    for branch in REVIEWER_BRANCH_NODES:
        g.add_edge("dispatch_reviewers", branch)
        g.add_edge(branch, "synthesize_findings")

    g.add_conditional_edges(
        "synthesize_findings",
        nodes.route_after_synthesize,
        {
            "coder_agent": "coder_agent",
            "verify_gate": "verify_gate",
            "summary_and_submit": "summary_and_submit",
            "persist_memory_and_finalize": "persist_memory_and_finalize",
        },
    )
    g.add_conditional_edges(
        "coder_agent",
        nodes.route_after_coder,
        {
            "qa_checker": "qa_checker",
            "summary_and_submit": "summary_and_submit",
            "persist_memory_and_finalize": "persist_memory_and_finalize",
        },
    )
    g.add_conditional_edges(
        "qa_checker",
        nodes.route_after_qa,
        {
            "verify_gate": "verify_gate",
            "coder_agent": "coder_agent",
            "summary_and_submit": "summary_and_submit",
            "persist_memory_and_finalize": "persist_memory_and_finalize",
        },
    )
    g.add_conditional_edges(
        "verify_gate",
        nodes.route_after_verify_gate,
        {
            "dispatch_reviewers": "dispatch_reviewers",
            "coder_agent": "coder_agent",
            "summary_and_submit": "summary_and_submit",
            "persist_memory_and_finalize": "persist_memory_and_finalize",
        },
    )
    g.add_conditional_edges(
        "summary_and_submit",
        nodes.route_after_summary,
        {
            "merge_gate": "merge_gate",
            "persist_memory_and_finalize": "persist_memory_and_finalize",
        },
    )
    g.add_conditional_edges(
        "merge_gate",
        nodes.route_after_merge_gate,
        {
            "merge": "merge",
            "human_approval": "human_approval",
            "persist_memory_and_finalize": "persist_memory_and_finalize",
        },
    )
    g.add_conditional_edges(
        "human_approval",
        nodes.route_after_human,
        {
            "merge_gate": "merge_gate",
            "human_approval": "human_approval",
            "persist_memory_and_finalize": "persist_memory_and_finalize",
        },
    )
    g.add_edge("merge", "persist_memory_and_finalize")
    g.add_edge("persist_memory_and_finalize", END)

    return g.compile(checkpointer=checkpointer)


def make_checkpointer(path):
    """创建 SQLite LangGraph 检查点存储器。"""
    if hasattr(path, "parent"):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn)


__all__ = ["REVIEWER_BRANCH_NODES", "build_graph", "make_checkpointer"]
