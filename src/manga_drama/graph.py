"""LangGraph 图装配：注册 12 节点 + 边 + 7 条件路由，编译带 checkpointer。

改造说明（聊天式）：
- 新增 director_resolve 节点：统筹各 Agent 异常
- route_after_understand：可直接路由到 write_draft 或 director_plan（用户修改请求）
- 各 route_after_* 增加 director_resolve 分支
- route_after_resolve：根据解决方案路由到目标节点或 escalate
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .nodes import GraphNodes
from .state import WorkflowState


def build_graph(nodes: GraphNodes, checkpointer=None):
    """构建并编译漫剧制作图。interrupt 节点要求 compile 带 checkpointer。"""
    g = StateGraph(WorkflowState)

    # ── 节点注册 ──
    g.add_node("understand", nodes.understand)
    g.add_node("clarify_ask", nodes.clarify_ask)
    g.add_node("director_plan", nodes.director_plan)
    g.add_node("director_resolve", nodes.director_resolve)   # 新增：异常统筹
    g.add_node("write_draft", nodes.write_draft)
    g.add_node("machine_review", nodes.machine_review)
    g.add_node("director_supervise", nodes.director_supervise)
    g.add_node("human_review", nodes.human_review)
    g.add_node("build_anchors", nodes.build_anchors)
    g.add_node("confirm_anchors", nodes.confirm_anchors)
    g.add_node("produce_video", nodes.produce_video)
    g.add_node("escalate", nodes.escalate)

    # ── 边 ──
    g.add_edge(START, "understand")

    # 理解 Agent：briefing/revision 双模式路由
    g.add_conditional_edges(
        "understand", nodes.route_after_understand,
        {
            "director_plan": "director_plan",
            "clarify_ask": "clarify_ask",
            "write_draft": "write_draft",    # 用户直接要求修改剧本
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
            "director_resolve": "director_resolve",   # 新增
            "escalate": "escalate",
        },
    )
    g.add_conditional_edges(
        "director_supervise", nodes.route_after_supervise,
        {
            "write_draft": "write_draft",
            "human_review": "human_review",
            "director_resolve": "director_resolve",   # 新增
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
            "produce_video": "produce_video",
            "build_anchors": "build_anchors",
            "director_resolve": "director_resolve",   # 新增
            "escalate": "escalate",
        },
    )
    g.add_conditional_edges(
        "produce_video", nodes.route_after_produce,
        {
            END: END,
            "produce_video": "produce_video",
            "director_resolve": "director_resolve",   # 新增
            "escalate": "escalate",
        },
    )

    # 导演解决节点的出口路由
    g.add_conditional_edges(
        "director_resolve", nodes.route_after_resolve,
        {
            "write_draft": "write_draft",
            "director_plan": "director_plan",
            "machine_review": "machine_review",
            "director_supervise": "director_supervise",
            "produce_video": "produce_video",
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
