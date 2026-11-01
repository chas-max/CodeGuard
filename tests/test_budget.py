"""budget 累计与熔断测试。"""
from dataclasses import replace

from manga_drama.budget import refresh_budget_exceeded, total_cost_cny
from manga_drama.llm import CostEvent
from manga_drama.state import initial_state


def test_total_cost_empty():
    st = initial_state("x", "j")
    assert total_cost_cny(st) == 0.0


def test_total_cost_sums():
    st = initial_state("x", "j")
    st["budget_events"] = [
        CostEvent(type="llm", model="deepseek-chat", cost_cny=0.01).to_dict(),
        CostEvent(type="t2i", model="wanx", cost_cny=0.04).to_dict(),
    ]
    assert total_cost_cny(st) == 0.05


def test_refresh_budget_exceeded(mock_cfg):
    cfg = replace(mock_cfg, budget_max_cny=0.10)
    st = initial_state("x", "j")
    assert refresh_budget_exceeded(st, cfg) is False
    st["budget_events"] = [
        CostEvent(type="t2i", model="wanx", cost_cny=0.12).to_dict(),
    ]
    assert refresh_budget_exceeded(st, cfg) is True
