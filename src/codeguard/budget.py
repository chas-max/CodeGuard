"""预算管理：累计 budget_events 成本，超阈值熔断。"""
from __future__ import annotations

from .config import CodeGuardConfig
from .state import CodeGuardState


def total_cost_cny(state: CodeGuardState) -> float:
    """累计已记录成本（元）。"""
    return round(
        sum(float(e.get("cost_cny", 0.0)) for e in state.get("budget_events", [])), 4
    )


def refresh_budget_exceeded(state: CodeGuardState, cfg: CodeGuardConfig) -> bool:
    """根据当前 budget_events 重新判定是否超预算。节点 append 成本后调用并写回 state。"""
    return total_cost_cny(state) >= cfg.budget_max_cny
