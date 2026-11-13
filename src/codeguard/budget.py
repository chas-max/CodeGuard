"""预算管理：累计 budget_events 成本，超阈值熔断。"""
from __future__ import annotations

from .config import Settings


def total_cost_cny(state: dict) -> float:
    return round(sum(float(e.get("cost_cny", 0.0)) for e in state.get("budget_events", [])), 4)


def refresh_budget_exceeded(state: dict, cfg: Settings) -> bool:
    return total_cost_cny(state) >= cfg.budget_max_cny


__all__ = ["total_cost_cny", "refresh_budget_exceeded"]
