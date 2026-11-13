"""LLM 工厂、角色模型表、成本采集与宽松 JSON 解析。

所有 provider 走 OpenAI 兼容 ChatOpenAI；测试通过注入 fake provider 绕过 API key。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_openai import ChatOpenAI

from .config import Settings
from .errors import NonRetryableError

# role -> (provider, default model)。可用 MODEL_<ROLE> 环境变量覆盖模型。
ROLE_MODELS: dict[str, tuple[str, str]] = {
    "issue_verifier": ("deepseek", "deepseek-flash"),
    "issue_verifier_fallback": ("qwen", "qwen3.8-flash"),
    "semantic_detector": ("deepseek", "deepseek-flash"),
    "semantic_detector_contract": ("deepseek", "deepseek-flash"),
    "semantic_detector_dataflow": ("deepseek", "deepseek-flash"),
    "semantic_detector_architecture": ("deepseek", "deepseek-flash"),
    "reviewer": ("deepseek", "deepseek-flash"),
    "reviewer_fallback": ("qwen", "qwen3.8-flash"),
    # finding 复核：刻意用两个不同 provider，降低同源误差相关性
    "finding_verifier_a": ("deepseek", "deepseek-flash"),
    "finding_verifier_b": ("qwen", "qwen3.8-flash"),
    "fixer": ("qwen", "qwen3.8-flash"),
    "fixer_fallback": ("deepseek", "deepseek-flash"),
    # 兼容旧调用点，重构完成前保留
    "understand": ("deepseek", "deepseek-flash"),
    "review": ("deepseek", "deepseek-flash"),
}

# 新模型先按同档位旧价格估算预算；以实际账单为准调整。
PRICE_PER_1K_CNY: dict[str, tuple[float, float]] = {
    "deepseek-flash": (0.001, 0.002),
    "deepseek-chat": (0.001, 0.002),
    "deepseek-reasoner": (0.004, 0.016),
    "qwen3.8-flash": (0.0008, 0.002),
    "qwen-max": (0.040, 0.120),
    "qwen-plus": (0.0008, 0.002),
    "qwen-turbo": (0.0003, 0.0006),
}


def role_model(role: str) -> tuple[str, str]:
    if role not in ROLE_MODELS:
        raise ValueError(f"未知 LLM 角色: {role}")
    provider, model = ROLE_MODELS[role]
    override = (os.environ.get(f"MODEL_{role.upper()}") or "").strip()
    return provider, override or model


def make_llm(role: str, cfg: Settings, *, temperature: float = 0.2, streaming: bool = False) -> ChatOpenAI:
    provider, model = role_model(role)
    if provider == "deepseek":
        api_key, base_url = cfg.require_deepseek(), cfg.deepseek_base_url
    elif provider == "qwen":
        api_key, base_url = cfg.require_dashscope(), cfg.dashscope_base_url
    else:
        raise ValueError(f"未知 provider: {provider}")
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_retries=0,
        streaming=streaming,
        request_timeout=cfg.request_timeout_sec,
    )


@dataclass
class CostEvent:
    type: str = "llm"
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cny: float = 0.0
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_cny": self.cost_cny,
            "detail": self.detail,
        }


def estimate_llm_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICE_PER_1K_CNY.get(model, (0.0, 0.0))
    return pin * input_tokens / 1000.0 + pout * output_tokens / 1000.0


def track_usage(response: Any, role: str) -> CostEvent | None:
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return None
    _, model = role_model(role)
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    return CostEvent(
        model=model,
        input_tokens=inp,
        output_tokens=out,
        cost_cny=estimate_llm_cost(model, inp, out),
    )


_DSML_MARKER = "DSML"
_DSML_INVOKE_RE = re.compile(
    r'invoke\s+name="([^"]+)"[^>]*>(.*?)(?:</[^>]*invoke>|\Z)', re.DOTALL,
)
_DSML_PARAM_RE = re.compile(
    r'parameter\s+name="([^"]+)"(?:\s+string="(true|false)")?[^>]*>(.*?)(?:</[^>]*parameter>|\Z)',
    re.DOTALL,
)


def parse_dsml_tool_action(text: str) -> dict | None:
    """解析 DeepSeek 原生 DSML 工具调用，归一化为项目的动作 JSON。

    部分模型（如 deepseek-flash）在工具场景下会输出 `<｜｜DSML｜｜ invoke ...>`，
    而不是约定的 `{"tool": ..., "args": ...}`。这里只做格式归一化，不执行任何内容；
    一次响应只取第一个 invoke，后续步骤会重新请求。
    """
    if not text or _DSML_MARKER not in text:
        return None
    match = _DSML_INVOKE_RE.search(text)
    if not match:
        return None
    name = match.group(1).strip()
    if not name:
        return None
    args: dict[str, Any] = {}
    for param in _DSML_PARAM_RE.finditer(match.group(2)):
        key = param.group(1)
        raw = (param.group(3) or "").strip()
        if param.group(2) == "false":
            try:
                args[key] = json.loads(raw)
            except json.JSONDecodeError:
                args[key] = raw
        else:
            args[key] = raw
    return {"tool": name, "args": args}


def parse_json_loose(text: str) -> dict:
    """宽松解析 LLM 输出中的 JSON 对象。"""
    if not text:
        raise NonRetryableError("空响应，无法解析 JSON")
    s = text.strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = s.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise NonRetryableError(f"无法从响应解析 JSON：{s[:200]}")


__all__ = [
    "ROLE_MODELS", "role_model", "make_llm", "CostEvent", "track_usage",
    "estimate_llm_cost", "parse_json_loose", "parse_dsml_tool_action",
]
