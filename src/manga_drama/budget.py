"""预算管理：累计 budget_events 成本，超阈值熔断。

cost_collector 把每次 LLM / 文生图 / TTS 调用的 CostEvent.to_dict() append 到
state.budget_events；节点 append 后调用 refresh_budget_exceeded 重算 budget_exceeded；
路由函数仅读 state.budget_exceeded 决定是否走 escalate。check_budget 本身是确定性
求和，不调 LLM。
"""
from __future__ import annotations

from .config import MangaDramaConfig
from .state import WorkflowState


def total_cost_cny(state: WorkflowState) -> float:
    """累计已记录成本（元）。"""
    return round(
        sum(float(e.get("cost_cny", 0.0)) for e in state.get("budget_events", [])), 4
    )


def refresh_budget_exceeded(state: WorkflowState, cfg: MangaDramaConfig) -> bool:
    """根据当前 budget_events 重新判定是否超预算。节点 append 成本后调用并写回 state。"""
    return total_cost_cny(state) >= cfg.budget_max_cny
