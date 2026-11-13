"""Agent 公共调用层：LLM 调用、成本采集、JSON 解析、Pydantic 校验。"""
from __future__ import annotations

import json
from typing import Any, TypeVar

from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from ..errors import AgentError, RetryableError
from ..llm import parse_dsml_tool_action, parse_json_loose, track_usage
from ..retry import with_retry
from ..tools.spec import ToolBox, ToolLimits, ToolSpec

T = TypeVar("T", bound=BaseModel)


def _invoke_llm(llm: Any, role: str, messages: list) -> Any:
    """带瞬时重试地调用单个 Runnable/LLM 实例。"""

    @with_retry(max_attempts=2)
    def _call():
        try:
            return llm.invoke(messages)
        except Exception as exc:  # noqa: BLE001
            raise RetryableError(f"{role} LLM 调用失败: {exc}") from exc

    try:
        return _call()
    except Exception as exc:  # noqa: BLE001
        raise AgentError(f"{role} 调用失败: {exc}") from exc


def _invoke(ctx, role: str, messages: list) -> Any:
    """调用一次 LLM；仅对瞬时错误重试，最终失败统一转 AgentError。"""
    return _invoke_llm(ctx.llm(role), role, messages)


def _extract_native_tool_calls(response: Any) -> list[dict[str, Any]]:
    """提取 LangChain / OpenAI 原生 `tool_calls` 列表并归一化为 `{id, name, args}`。"""
    raw_calls = getattr(response, "tool_calls", None) or []
    normalized: list[dict[str, Any]] = []
    if isinstance(raw_calls, list) and raw_calls:
        for idx, item in enumerate(raw_calls):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            args = item.get("args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            normalized.append({
                "id": str(item.get("id") or f"call_{idx}"),
                "name": name,
                "args": args if isinstance(args, dict) else {},
            })
        return normalized

    additional = getattr(response, "additional_kwargs", None) or {}
    legacy_calls = additional.get("tool_calls") if isinstance(additional, dict) else None
    if isinstance(legacy_calls, list):
        for idx, item in enumerate(legacy_calls):
            if not isinstance(item, dict):
                continue
            fn = item.get("function") if isinstance(item.get("function"), dict) else {}
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            raw_args = fn.get("arguments", "{}")
            args: Any = raw_args
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
            normalized.append({
                "id": str(item.get("id") or f"call_{idx}"),
                "name": name,
                "args": args if isinstance(args, dict) else {},
            })
    return normalized


def _invoke_with_tools(
    ctx,
    role: str,
    messages: list,
    tool_specs: list[ToolSpec] | None = None,
    *,
    protocol: str = "auto",
) -> tuple[Any, str]:
    """在支持 `bind_tools` 时优先走原生 Tool Calling，否则无缝回退文本协议。"""
    llm = ctx.llm(role)
    if protocol != "json" and tool_specs and callable(getattr(llm, "bind_tools", None)):
        openai_tools = [spec.to_openai_tool() for spec in tool_specs]
        try:
            bound = llm.bind_tools(openai_tools)
            return _invoke_llm(bound, role, messages), "native"
        except (NotImplementedError, AttributeError):
            if protocol == "native":
                raise AgentError(f"{role} 当前 LLM 不支持原生 bind_tools")
        except Exception as exc:
            if protocol == "auto":
                # 部分模型或代理对 tools 参数兼容性不佳，auto 模式下回退文本 JSON/DSML 协议
                return _invoke_llm(llm, role, messages), "json"
            raise exc
    return _invoke_llm(llm, role, messages), "json"


def _collect_usage(response: Any, role: str, costs: list[dict]) -> None:
    event = track_usage(response, role)
    if event:
        costs.append(event.to_dict())


def _parse_payload(role: str, content: str) -> dict:
    try:
        return parse_json_loose(content or "")
    except Exception as exc:  # noqa: BLE001
        # 部分模型会输出原生 DSML 工具调用；归一化后仍走同一套工具协议。
        action = parse_dsml_tool_action(content or "")
        if action:
            return action
        raise AgentError(f"{role} 输出不是合法 JSON: {exc}") from exc


def _validate_payload(role: str, payload: dict, model_cls: type[T]) -> T:
    try:
        return model_cls.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        raise AgentError(f"{role} 输出不满足 schema: {exc}") from exc


def call_json(
    ctx,
    role: str,
    prompt: str,
    model_cls: type[T],
    *,
    temperature: float = 0.2,
) -> tuple[T, list[dict]]:
    """调用 LLM 并把 JSON 响应校验为 model_cls；返回 (model, cost_events)。"""
    response = _invoke(ctx, role, [HumanMessage(content=prompt)])
    costs: list[dict] = []
    _collect_usage(response, role, costs)
    payload = _parse_payload(role, getattr(response, "content", "") or "")
    return _validate_payload(role, payload, model_cls), costs


def call_json_with_tools(
    ctx,
    role: str,
    prompt: str,
    model_cls: type[T],
    *,
    tools: ToolBox | None = None,
    limits: ToolLimits | None = None,
    temperature: float = 0.2,
    max_cost_cny: float = 0.0,
    protocol: str | None = None,
) -> tuple[T, list[dict], list[dict]]:
    """带工具的多轮调用；工具不可用时等价于 call_json（行为与改造前一致）。

    返回 `(最终 model, LLM 成本事件, 工具审计事件)`。
    """
    usable = tools is not None and bool(tools.specs())
    if not usable:
        model, costs = call_json(ctx, role, prompt, model_cls, temperature=temperature)
        return model, costs, []

    from .tool_loop import run_tool_loop

    eff_limits = limits or ToolLimits()
    eff_protocol = protocol or eff_limits.protocol or "auto"
    return run_tool_loop(
        ctx,
        role=role,
        prompt=prompt,
        model_cls=model_cls,
        tools=tools,
        limits=eff_limits,
        temperature=temperature,
        max_cost_cny=max_cost_cny,
        protocol=eff_protocol,
    )


def record_costs(state: dict, costs: list[dict]) -> dict:
    total = sum(float(c.get("cost_cny", 0.0)) for c in costs)
    return {
        "budget_events": costs,
        "llm_calls": int(state.get("llm_calls", 0)) + len(costs),
        "last_cost_cny": round(total, 4),
    }


__all__ = [
    "call_json", "call_json_with_tools", "record_costs",
    "_invoke", "_invoke_with_tools", "_extract_native_tool_calls",
    "_collect_usage", "_parse_payload", "_validate_payload",
]
