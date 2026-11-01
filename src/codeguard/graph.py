"""LangGraph 图装配：注册 12 节点 + 边 + 7 条件路由，编译带 checkpointer。

拓扑与原漫剧图同构（produce_video → sandbox_execute 改名），保证：
- 3 处 HITL interrupt（clarify_ask / human_review / confirm_anchors）
- 每个审核环节的打回回路 + 封顶 escalate
- director_resolve 异常旁路与出口路由
"""
from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .nodes import CodeGuardNodes
from .state import CodeGuardState


def build_graph(nodes: CodeGuardNodes, checkpointer=None):
    """构建并编译 CodeGuard 修复图。interrupt 节点要求 compile 带 checkpointer。"""
    g = StateGraph(CodeGuardState)

    # ── 节点注册 ──
    g.add_node("understand", nodes.understand)
    g.add_node("clarify_ask", nodes.clarify_ask)
    g.add_node("director_plan", nodes.director_plan)
    g.add_node("director_resolve", nodes.director_resolve)
    g.add_node("write_draft", nodes.write_draft)
    g.add_node("machine_review", nodes.machine_review)
    g.add_node("director_supervise", nodes.director_supervise)
    g.add_node("human_review", nodes.human_review)
    g.add_node("build_anchors", nodes.build_anchors)
    g.add_node("confirm_anchors", nodes.confirm_anchors)
    g.add_node("sandbox_execute", nodes.sandbox_execute)
    g.add_node("escalate", nodes.escalate)

    # ── 边 ──
    g.add_edge(START, "understand")

    # 理解 Agent：parsed/clarify/revision 三态路由
    g.add_conditional_edges(
        "understand", nodes.route_after_understand,
        {
            "director_plan": "director_plan",
            "clarify_ask": "clarify_ask",
            "write_draft": "write_draft",       # 用户补充信息直改补丁
            "director_resolve": "director_resolve",
        },
    )
    g.add_edge("clarify_ask", "understand")

    g.add_edge("director_plan", "write_draft")
    g.add_edge("write_draft", "machine_review")

    g.add_conditional_edges(
        "machine_review", nodes.route_after_machine,
        {
            "write_draft": "write_draft",
            "director_supervise": "director_supervise",
            "director_resolve": "director_resolve",
            "escalate": "escalate",
        },
    )
    g.add_conditional_edges(
        "director_supervise", nodes.route_after_supervise,
        {
            "write_draft": "write_draft",
            "human_review": "human_review",
            "director_resolve": "director_resolve",
            "escalate": "escalate",
        },
    )
    g.add_conditional_edges(
        "human_review", nodes.route_after_human,
        {"build_anchors": "build_anchors", "write_draft": "write_draft", "escalate": "escalate"},
    )
    g.add_edge("build_anchors", "confirm_anchors")
    g.add_conditional_edges(
        "confirm_anchors", nodes.route_after_anchors,
        {
            "sandbox_execute": "sandbox_execute",
            "build_anchors": "build_anchors",
            "director_resolve": "director_resolve",
            "escalate": "escalate",
        },
    )
    g.add_conditional_edges(
        "sandbox_execute", nodes.route_after_execute,
        {
            END: END,
            "sandbox_execute": "sandbox_execute",
            "director_resolve": "director_resolve",
            "escalate": "escalate",
        },
    )

    # 异常统筹节点的出口路由
    g.add_conditional_edges(
        "director_resolve", nodes.route_after_resolve,
        {
            "write_draft": "write_draft",
            "director_plan": "director_plan",
            "machine_review": "machine_review",
            "director_supervise": "director_supervise",
            "sandbox_execute": "sandbox_execute",
            "build_anchors": "build_anchors",
            "escalate": "escalate",
        },
    )

    g.add_edge("escalate", END)

    return g.compile(checkpointer=checkpointer)


def make_checkpointer(path):
    """构造 SqliteSaver（持久化 interrupt 状态，跨进程可恢复）。"""
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn)
