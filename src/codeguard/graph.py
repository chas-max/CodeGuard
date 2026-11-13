"""LangGraph 图装配：11 节点 PR 审查流程 + 条件路由。"""
from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .nodes import ReviewNodes
from .state import ReviewState


def build_graph(nodes: ReviewNodes, checkpointer=None):
    g = StateGraph(ReviewState)

    g.add_node("ingest", nodes.ingest)
    g.add_node("preflight", nodes.preflight)
    g.add_node("hybrid_scan", nodes.hybrid_scan)
    g.add_node("verify_issue", nodes.verify_issue)
    g.add_node("validate", nodes.validate)
    g.add_node("review", nodes.review)
    g.add_node("fix", nodes.fix)
    g.add_node("merge_gate", nodes.merge_gate)
    g.add_node("human_approval", nodes.human_approval)
    g.add_node("merge", nodes.merge)
    g.add_node("finalize", nodes.finalize)

    g.add_edge(START, "ingest")
    g.add_edge("ingest", "preflight")
    g.add_conditional_edges(
        "preflight", nodes.route_after_preflight,
        {"hybrid_scan": "hybrid_scan", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "hybrid_scan", nodes.route_after_hybrid,
        {"verify_issue": "verify_issue", "validate": "validate", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "verify_issue", nodes.route_after_verify,
        {"validate": "validate", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "validate", nodes.route_after_validate,
        {"review": "review", "fix": "fix", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "review", nodes.route_after_review,
        {"merge_gate": "merge_gate", "fix": "fix", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "fix", nodes.route_after_fix,
        {"hybrid_scan": "hybrid_scan", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "merge_gate", nodes.route_after_merge_gate,
        {"merge": "merge", "human_approval": "human_approval", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "human_approval", nodes.route_after_human,
        {"merge_gate": "merge_gate", "human_approval": "human_approval", "finalize": "finalize"},
    )
    g.add_edge("merge", END)
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer)


def make_checkpointer(path):
    path.parent.mkdir(parents=True, exist_ok=True) if hasattr(path, "parent") else None
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn)


__all__ = ["build_graph", "make_checkpointer"]
