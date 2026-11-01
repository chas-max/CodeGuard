"""节点共享工具：LLM 调用 + 成本结算 + 产物目录（复用原系统模式）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from ..budget import total_cost_cny
from ..errors import NonRetryableError, RetryableError
from ..llm import parse_json_loose, track_usage
from ..retry import with_retry
from ..state import CodeGuardState


def call_llm_json(
    ctx,
    role: str,
    prompt: str,
    temperature: float = 0.7,
) -> tuple[dict, list[dict]]:
    """调用 LLM 并解析 JSON，返回 (parsed_dict, cost_events)。

    解析失败返回 ({}, costs)，由调用方决定兜底。重试交由 with_retry。
    """
    llm = ctx.llm(role)

    @with_retry()
    def _do():
        try:
            return llm.invoke([HumanMessage(content=prompt)])
        except Exception as e:
            raise RetryableError(f"{role} LLM 调用失败: {e}") from e

    resp = _do()
    costs: list[dict] = []
    ev = track_usage(resp, role)
    if ev:
        costs.append(ev.to_dict())
    content = getattr(resp, "content", "") or ""
    try:
        obj = parse_json_loose(content)
    except NonRetryableError:
        obj = {}
    return obj, costs


def settle_costs(state: CodeGuardState, ctx, costs: list[dict]) -> dict:
    """把成本事件 append 到 budget_events，并重算 budget_exceeded。"""
    prev_total = total_cost_cny(state)
    added = sum(float(c.get("cost_cny", 0.0)) for c in costs)
    return {
        "budget_events": costs,
        "budget_exceeded": (prev_total + added) >= ctx.cfg.budget_max_cny,
    }


def artifacts_dir_for(state: CodeGuardState, ctx) -> Path:
    """任务产物目录：output_dir/<job_id>/（fix.patch、覆盖率报告、运行日志）。"""
    job_id = state.get("job_id") or "default"
    d = Path(ctx.cfg.output_dir) / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d
