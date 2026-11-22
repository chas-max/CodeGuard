"""Agent 公共调用层：LLM 调用、流式看门狗、自动故障切换、成本采集、JSON/XML 解析与校验。"""
from __future__ import annotations

import inspect
import json
import queue
import threading
import time
from typing import Any, TypeVar

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..errors import AgentError, LLMTimeoutError, NonRetryableError, RetryableError
from ..llm import (
    ROLE_MODELS,
    parse_dsml_tool_action,
    parse_json_loose,
    parse_xml_tool_action,
    track_usage,
)
from ..tools.spec import ToolBox, ToolLimits, ToolSpec

T = TypeVar("T", bound=BaseModel)

_TRANSIENT_HINTS = (
    "timed out",
    "timeout",
    "超时",
    "ttft",
    "空闲",
    "超限",
    "connection",
    "502",
    "503",
    "504",
    "429",
    "402",
    "insufficient balance",
    "rate limit",
)


def _is_transient_transport_error(exc: BaseException) -> bool:
    cause = getattr(exc, "__cause__", None)
    if isinstance(exc, LLMTimeoutError) or isinstance(cause, LLMTimeoutError):
        return True
    msg = str(exc).lower()
    return any(hint in msg for hint in _TRANSIENT_HINTS)


def _is_transient_retryable(exc: BaseException) -> bool:
    return isinstance(exc, RetryableError) and not isinstance(exc, LLMTimeoutError)


def _timeouts_from_ctx(ctx: Any) -> tuple[float, float, float]:
    cfg = getattr(ctx, "cfg", None)
    if cfg is None:
        return 25.0, 15.0, 75.0
    return (
        float(getattr(cfg, "llm_ttft_timeout_sec", 25.0) or 25.0),
        float(getattr(cfg, "llm_chunk_idle_timeout_sec", 15.0) or 15.0),
        float(getattr(cfg, "llm_step_timeout_sec", 75.0) or 75.0),
    )


def _resolve_llm(ctx: Any, role: str, *, tier: str = "T1", phase: str = "default") -> Any:
    llm_fn = ctx.llm
    try:
        sig = inspect.signature(llm_fn)
        params = sig.parameters
        if "tier" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return llm_fn(role, tier=tier, phase=phase)
    except (ValueError, TypeError):
        pass
    return llm_fn(role)


def supports_native_tools(ctx: Any, role: str, protocol: str = "auto") -> bool:
    """判断该角色当前 LLM 是否可用原生 bind_tools（用于消除冗余文本 schema）。"""
    if protocol == "json":
        return False
    try:
        llm = _resolve_llm(ctx, role)
    except Exception:  # noqa: BLE001
        return False
    return callable(getattr(llm, "bind_tools", None))


def _resolve_fallback_llm(
    ctx: Any,
    role: str,
    primary: Any,
    *,
    tier: str = "T1",
    phase: str = "default",
) -> Any:
    """按 ROLE_MODELS 约定解析 `<role>_fallback` 备选模型；不可用时返回 None。"""
    fb_role = f"{role}_fallback"
    if role.endswith("_fallback") or fb_role not in ROLE_MODELS or not hasattr(ctx, "llm"):
        return None
    try:
        fb = _resolve_llm(ctx, fb_role, tier=tier, phase=phase)
    except Exception:  # noqa: BLE001
        return None
    if fb is None or fb is primary:
        return None
    if hasattr(fb, "_queue") and not getattr(fb, "_queue"):
        return None
    return fb


def _should_stream(llm: Any) -> bool:
    if not callable(getattr(llm, "stream", None)):
        return False
    if bool(getattr(llm, "streaming", False)):
        return True
    bound = getattr(llm, "bound", None)
    return bool(bound is not None and getattr(bound, "streaming", False))


def stream_llm_response(
    llm: Any,
    role: str,
    messages: list,
    *,
    ttft_timeout: float = 25.0,
    idle_timeout: float = 15.0,
    hard_timeout: float = 75.0,
) -> Any:
    """消费流式响应并施加 TTFT / Chunk-Idle / 单步硬上限三重看门狗。"""
    q: queue.Queue[tuple[str, Any]] = queue.Queue()
    cancel = threading.Event()

    def _worker() -> None:
        try:
            for chunk in llm.stream(messages):
                if cancel.is_set():
                    break
                q.put(("chunk", chunk))
            q.put(("done", None))
        except Exception as exc:  # noqa: BLE001
            q.put(("err", exc))

    threading.Thread(target=_worker, daemon=True, name=f"llm-stream-{role}").start()
    started = time.monotonic()
    first_chunk_at: float | None = None
    last_chunk_at = started
    acc: Any = None
    chunk_count = 0

    while True:
        now = time.monotonic()
        remaining_hard = hard_timeout - (now - started)
        if remaining_hard <= 0:
            cancel.set()
            raise LLMTimeoutError(f"{role} 单步流式总耗时超限 (>{hard_timeout:.1f}s)")
        stage_budget = ttft_timeout if first_chunk_at is None else idle_timeout
        stage_base = started if first_chunk_at is None else last_chunk_at
        wait_sec = min(remaining_hard, max(0.01, stage_budget - (now - stage_base)))
        try:
            kind, payload = q.get(timeout=wait_sec)
        except queue.Empty as exc:
            cancel.set()
            kind_label = "TTFT" if first_chunk_at is None else "Chunk空闲"
            raise LLMTimeoutError(
                f"{role} 流式 {kind_label} 超时 (ttft={ttft_timeout}s, idle={idle_timeout}s, chunks={chunk_count})"
            ) from exc
        if kind == "err":
            raise payload
        if kind == "done":
            break
        chunk = payload
        chunk_count += 1
        tick = time.monotonic()
        if first_chunk_at is None:
            first_chunk_at = tick
        last_chunk_at = tick
        acc = chunk if acc is None else (acc + chunk)

    if acc is None:
        acc = AIMessage(content="")
    wall_ms = max(0, int((time.monotonic() - started) * 1000))
    ttft_ms = max(0, int(((first_chunk_at or started) - started) * 1000))
    additional = getattr(acc, "additional_kwargs", None) or {}
    reasoning_chars = len(str(additional.get("reasoning_content") or "")) if isinstance(additional, dict) else 0
    resp_meta = getattr(acc, "response_metadata", None)
    if isinstance(resp_meta, dict):
        resp_meta["codeguard_latency"] = {
            "ttft_ms": ttft_ms,
            "wall_ms": wall_ms,
            "chunks": chunk_count,
            "reasoning_chars": reasoning_chars,
        }
    return acc


def _call_candidate(
    llm: Any,
    role: str,
    messages: list,
    *,
    ttft_timeout: float,
    idle_timeout: float,
    hard_timeout: float,
) -> Any:
    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=0.5, max=2.0),
        retry=retry_if_exception(_is_transient_retryable),
        reraise=True,
    )
    def _attempt():
        try:
            if _should_stream(llm):
                return stream_llm_response(
                    llm,
                    role,
                    messages,
                    ttft_timeout=ttft_timeout,
                    idle_timeout=idle_timeout,
                    hard_timeout=hard_timeout,
                )
            return llm.invoke(messages)
        except (LLMTimeoutError, NonRetryableError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise RetryableError(f"{role} LLM 调用失败: {exc}") from exc

    return _attempt()


def _invoke_llm(
    llm: Any,
    role: str,
    messages: list,
    *,
    ttft_timeout: float = 25.0,
    idle_timeout: float = 15.0,
    hard_timeout: float = 75.0,
) -> Any:
    """调用单个（或带回退链的）LLM 实例；超时直接快速失败切换，不再原地重复等待。"""
    candidates = llm.iter_candidates() if hasattr(llm, "iter_candidates") else [llm]
    last_exc: Exception | None = None
    for candidate in candidates:
        try:
            return _call_candidate(
                candidate,
                role,
                messages,
                ttft_timeout=ttft_timeout,
                idle_timeout=idle_timeout,
                hard_timeout=hard_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
    raise AgentError(f"{role} 调用失败: {last_exc}") from last_exc


def _invoke(
    ctx,
    role: str,
    messages: list,
    *,
    tier: str = "T1",
    phase: str = "default",
) -> Any:
    """调用一次 LLM；失败或流式超时后自动切换到 `<role>_fallback`。"""
    ttft, idle, hard = _timeouts_from_ctx(ctx)
    primary = _resolve_llm(ctx, role, tier=tier, phase=phase)
    try:
        return _invoke_llm(
            primary, role, messages, ttft_timeout=ttft, idle_timeout=idle, hard_timeout=hard,
        )
    except AgentError:
        fallback = _resolve_fallback_llm(ctx, role, primary, tier=tier, phase=phase)
        if fallback is None:
            raise
        return _invoke_llm(
            fallback,
            f"{role}_fallback",
            messages,
            ttft_timeout=ttft,
            idle_timeout=idle,
            hard_timeout=hard,
        )


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


def _invoke_single_with_tools(
    llm: Any,
    role: str,
    messages: list,
    tool_specs: list[ToolSpec] | None = None,
    *,
    protocol: str = "auto",
    ttft_timeout: float = 25.0,
    idle_timeout: float = 15.0,
    hard_timeout: float = 75.0,
) -> tuple[Any, str]:
    if protocol != "json" and tool_specs and callable(getattr(llm, "bind_tools", None)):
        openai_tools = [spec.to_openai_tool() for spec in tool_specs]
        try:
            bound = llm.bind_tools(openai_tools)
            return _invoke_llm(
                bound,
                role,
                messages,
                ttft_timeout=ttft_timeout,
                idle_timeout=idle_timeout,
                hard_timeout=hard_timeout,
            ), "native"
        except (NotImplementedError, AttributeError):
            if protocol == "native":
                raise AgentError(f"{role} 当前 LLM 不支持原生 bind_tools")
        except Exception as exc:
            if protocol == "auto" and not _is_transient_transport_error(exc):
                # 仅在非网络超时/余额不足/限流异常（如模型代理不支持 tools 参数）时同模型降级文本协议
                return _invoke_llm(
                    llm,
                    role,
                    messages,
                    ttft_timeout=ttft_timeout,
                    idle_timeout=idle_timeout,
                    hard_timeout=hard_timeout,
                ), "json"
            raise exc
    return _invoke_llm(
        llm,
        role,
        messages,
        ttft_timeout=ttft_timeout,
        idle_timeout=idle_timeout,
        hard_timeout=hard_timeout,
    ), "json"


def _invoke_with_tools(
    ctx,
    role: str,
    messages: list,
    tool_specs: list[ToolSpec] | None = None,
    *,
    protocol: str = "auto",
    tier: str = "T1",
    phase: str = "default",
) -> tuple[Any, str]:
    """在支持 `bind_tools` 时优先走原生 Tool Calling；失败时自动切换到 `<role>_fallback`。"""
    ttft, idle, hard = _timeouts_from_ctx(ctx)
    primary = _resolve_llm(ctx, role, tier=tier, phase=phase)
    try:
        return _invoke_single_with_tools(
            primary,
            role,
            messages,
            tool_specs,
            protocol=protocol,
            ttft_timeout=ttft,
            idle_timeout=idle,
            hard_timeout=hard,
        )
    except AgentError:
        fallback = _resolve_fallback_llm(ctx, role, primary, tier=tier, phase=phase)
        if fallback is None:
            raise
        return _invoke_single_with_tools(
            fallback,
            f"{role}_fallback",
            messages,
            tool_specs,
            protocol=protocol,
            ttft_timeout=ttft,
            idle_timeout=idle,
            hard_timeout=hard,
        )


def _collect_usage(response: Any, role: str, costs: list[dict]) -> None:
    event = track_usage(response, role)
    if event:
        costs.append(event.to_dict())


def _parse_payload(role: str, content: str) -> dict:
    raw = content or ""
    # 原生 DSML 或 XML `<tool_call>` 标签优先解析，避免被参数体内部 JSON 片段截胡
    action = parse_dsml_tool_action(raw) or parse_xml_tool_action(raw)
    if action:
        return action
    try:
        return parse_json_loose(raw)
    except Exception as exc:  # noqa: BLE001
        raise AgentError(f"{role} 输出不是合法 JSON: {exc}") from exc


def _validate_payload(role: str, payload: dict, model_cls: type[T]) -> T:
    try:
        return model_cls.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        raise AgentError(f"{role} 输出不满足 schema: {exc}") from exc


def call_json(
    ctx,
    role: str,
    prompt: str | list,
    model_cls: type[T],
    *,
    temperature: float = 0.2,
    tier: str = "T1",
    phase: str = "default",
) -> tuple[T, list[dict]]:
    """调用 LLM 并把 JSON 响应校验为 model_cls；返回 (model, cost_events)。"""
    del temperature  # 温度由 role/profile 配置统一管理
    messages = list(prompt) if isinstance(prompt, list) else [HumanMessage(content=prompt)]
    response = _invoke(ctx, role, messages, tier=tier, phase=phase)
    costs: list[dict] = []
    _collect_usage(response, role, costs)
    payload = _parse_payload(role, getattr(response, "content", "") or "")
    return _validate_payload(role, payload, model_cls), costs


def call_json_with_tools(
    ctx,
    role: str,
    prompt: str | list,
    model_cls: type[T],
    *,
    tools: ToolBox | None = None,
    limits: ToolLimits | None = None,
    temperature: float = 0.2,
    max_cost_cny: float = 0.0,
    protocol: str | None = None,
    tier: str = "T1",
) -> tuple[T, list[dict], list[dict]]:
    """带工具的多轮调用；工具不可用时等价于 call_json（行为与改造前一致）。

    返回 `(最终 model, LLM 成本事件, 工具审计事件)`。
    """
    usable = tools is not None and bool(tools.specs())
    if not usable:
        model, costs = call_json(ctx, role, prompt, model_cls, temperature=temperature, tier=tier)
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
        tier=tier,
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
    "stream_llm_response", "supports_native_tools", "_invoke_llm", "_resolve_llm",
    "_invoke", "_invoke_with_tools", "_extract_native_tool_calls",
    "_collect_usage", "_parse_payload", "_validate_payload",
]
