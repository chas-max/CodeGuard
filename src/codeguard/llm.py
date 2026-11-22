"""LLM 工厂、角色模型表、分级 Thinking 策略、成本采集与宽松 JSON 解析。

所有 provider 走 OpenAI 兼容 ChatOpenAI；测试通过注入 fake provider 绕过 API key。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict

from .config import Settings
from .errors import NonRetryableError

# role -> (provider, default model)。可用 MODEL_<ROLE> / PROVIDER_<ROLE> / CODEGUARD_LLM_PROVIDER 覆盖。
ROLE_MODELS: dict[str, tuple[str, str]] = {
    "pr_reader": ("deepseek", "deepseek-flash"),
    "pr_reader_fallback": ("qwen", "qwen3.8-flash"),
    "review_logic": ("deepseek", "deepseek-flash"),
    "review_logic_fallback": ("qwen", "qwen3.8-flash"),
    "review_security": ("deepseek", "deepseek-flash"),
    "review_security_fallback": ("qwen", "qwen3.8-flash"),
    "review_architecture": ("deepseek", "deepseek-flash"),
    "review_architecture_fallback": ("qwen", "qwen3.8-flash"),
    "review_tests": ("deepseek", "deepseek-flash"),
    "review_tests_fallback": ("qwen", "qwen3.8-flash"),
    "coder": ("qwen", "qwen3.8-flash"),
    "coder_fallback": ("deepseek", "deepseek-flash"),
    "qa_checker": ("deepseek", "deepseek-flash"),
    "qa_checker_fallback": ("qwen", "qwen3.8-flash"),
    "summary": ("deepseek", "deepseek-flash"),
    "summary_fallback": ("qwen", "qwen3.8-flash"),
    # 兼容保留旧角色
    "issue_verifier": ("deepseek", "deepseek-flash"),
    "issue_verifier_fallback": ("qwen", "qwen3.8-flash"),
    "semantic_detector": ("deepseek", "deepseek-flash"),
    "semantic_detector_fallback": ("qwen", "qwen3.8-flash"),
    "semantic_detector_contract": ("deepseek", "deepseek-flash"),
    "semantic_detector_contract_fallback": ("qwen", "qwen3.8-flash"),
    "semantic_detector_dataflow": ("deepseek", "deepseek-flash"),
    "semantic_detector_dataflow_fallback": ("qwen", "qwen3.8-flash"),
    "semantic_detector_architecture": ("deepseek", "deepseek-flash"),
    "semantic_detector_architecture_fallback": ("qwen", "qwen3.8-flash"),
    "reviewer": ("deepseek", "deepseek-flash"),
    "reviewer_fallback": ("qwen", "qwen3.8-flash"),
    # finding 复核：刻意用两个不同 provider，降低同源误差相关性
    "finding_verifier_a": ("deepseek", "deepseek-flash"),
    "finding_verifier_a_fallback": ("qwen", "qwen3.8-flash"),
    "finding_verifier_b": ("qwen", "qwen3.8-flash"),
    "fixer": ("qwen", "qwen3.8-flash"),
    "fixer_fallback": ("deepseek", "deepseek-flash"),
    # 兼容旧调用点，重构完成前保留
    "understand": ("deepseek", "deepseek-flash"),
    "understand_fallback": ("qwen", "qwen3.8-flash"),
    "review": ("deepseek", "deepseek-flash"),
    "review_fallback": ("qwen", "qwen3.8-flash"),
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
    preferred = (
        os.environ.get(f"PROVIDER_{role.upper()}")
        or os.environ.get("CODEGUARD_LLM_PROVIDER")
        or ""
    ).strip().lower()
    if preferred in ("qwen", "deepseek") and preferred != provider:
        fb = ROLE_MODELS.get(f"{role}_fallback")
        if fb and fb[0] == preferred:
            provider, model = fb
        else:
            provider = preferred
            model = "qwen3.8-flash" if preferred == "qwen" else "deepseek-flash"
    override = (os.environ.get(f"MODEL_{role.upper()}") or "").strip()
    return provider, override or model


class ThinkingProfile(BaseModel):
    """按角色与风险等级约束 Thinking 开关、推理预算及输出上限。"""

    model_config = ConfigDict(frozen=True)
    enable_thinking: bool = False
    thinking_budget: int = 0
    max_tokens: int = 1024
    reasoning_effort: str = "low"
    label: str = "off"


_ROLE_ALIAS: dict[str, str] = {
    "review_logic": "reviewer",
    "review_security": "reviewer",
    "review_architecture": "reviewer",
    "review_tests": "reviewer",
    "review": "reviewer",
    "coder": "fixer",
    "semantic_detector_contract": "semantic_detector",
    "semantic_detector_dataflow": "semantic_detector",
    "semantic_detector_architecture": "semantic_detector",
    "understand": "semantic_detector",
}


def _t(enable: bool, budget: int, max_tokens: int, effort: str, label: str) -> ThinkingProfile:
    return ThinkingProfile(
        enable_thinking=enable,
        thinking_budget=budget,
        max_tokens=max_tokens,
        reasoning_effort=effort,
        label=label,
    )


ROLE_THINKING_PROFILES: dict[str, dict[str, ThinkingProfile]] = {
    "pr_reader": {
        "T0": _t(False, 0, 1024, "low", "pr_reader:t0_off"),
        "T1": _t(False, 0, 1536, "low", "pr_reader:t1_off"),
        "T2": _t(False, 0, 2048, "low", "pr_reader:t2_off"),
    },
    "qa_checker": {
        "T0": _t(False, 0, 1024, "low", "qa_checker:t0_off"),
        "T1": _t(False, 0, 1536, "low", "qa_checker:t1_off"),
        "T2": _t(True, 512, 2048, "low", "qa_checker:t2"),
    },
    "summary": {
        "T0": _t(False, 0, 1536, "low", "summary:t0_off"),
        "T1": _t(False, 0, 2048, "low", "summary:t1_off"),
        "T2": _t(False, 0, 2560, "low", "summary:t2_off"),
    },
    "issue_verifier": {
        "T0": _t(False, 0, 512, "low", "issue_verifier:off"),
        "T1": _t(False, 0, 512, "low", "issue_verifier:off"),
        "T2": _t(False, 0, 512, "low", "issue_verifier:off"),
    },
    "finding_verifier_a": {
        "T0": _t(False, 0, 512, "low", "verifier:off"),
        "T1": _t(False, 0, 512, "low", "verifier:off"),
        "T2": _t(True, 256, 1024, "low", "verifier:t2_low"),
    },
    "finding_verifier_b": {
        "T0": _t(False, 0, 512, "low", "verifier:off"),
        "T1": _t(False, 0, 512, "low", "verifier:off"),
        "T2": _t(True, 256, 1024, "low", "verifier:t2_low"),
    },
    "semantic_detector": {
        "T0": _t(False, 0, 1024, "low", "semantic:t0_off"),
        "T1": _t(False, 0, 2048, "low", "semantic:t1_off"),
        "T2": _t(True, 2048, 4096, "high", "semantic:t2"),
    },
    "reviewer": {
        "T0": _t(False, 0, 1536, "low", "reviewer:t0_off"),
        "T1": _t(True, 1024, 3072, "medium", "reviewer:t1"),
        "T2": _t(True, 2048, 4096, "high", "reviewer:t2"),
    },
    "fixer": {
        "T0": _t(True, 512, 2048, "low", "fixer:t0"),
        "T1": _t(True, 1536, 4096, "low", "fixer:t1"),
        "T2": _t(True, 2560, 6144, "high", "fixer:t2"),
    },
}


def resolve_thinking_profile(
    role: str,
    *,
    tier: str = "T1",
    phase: str = "default",
    mode: str = "tiered",
) -> ThinkingProfile | None:
    """按角色、风险等级、执行阶段（tool_step/default）及模式解析 Thinking 配置。"""
    norm_mode = (mode or "tiered").strip().lower()
    if norm_mode == "legacy":
        return None
    base = role[:-9] if role.endswith("_fallback") else role
    base = _ROLE_ALIAS.get(base, base)
    norm_tier = tier if tier in ("T0", "T1", "T2") else "T1"
    by_tier = ROLE_THINKING_PROFILES.get(base)
    profile = by_tier.get(norm_tier, by_tier["T1"]) if by_tier else _t(False, 0, 2048, "low", "default:off")
    if norm_mode == "off":
        return profile.model_copy(update={"enable_thinking": False, "thinking_budget": 0, "label": f"{base}:off"})
    if phase == "tool_step" and profile.enable_thinking and profile.thinking_budget > 512:
        max_tok = min(profile.max_tokens, 3072 if base == "fixer" else 2048)
        return profile.model_copy(update={
            "thinking_budget": 512,
            "max_tokens": max_tok,
            "reasoning_effort": "low",
            "label": f"{profile.label}:tool_step",
        })
    return profile


def _provider_thinking_kwargs(provider: str, profile: ThinkingProfile) -> dict[str, Any]:
    if provider == "qwen":
        extra: dict[str, Any] = {"enable_thinking": profile.enable_thinking}
        if profile.enable_thinking and profile.thinking_budget > 0:
            extra["thinking_budget"] = profile.thinking_budget
        return {"max_tokens": profile.max_tokens, "extra_body": extra}
    if provider == "deepseek":
        extra = {"thinking": {"type": "enabled" if profile.enable_thinking else "disabled"}}
        kwargs: dict[str, Any] = {"max_tokens": profile.max_tokens, "extra_body": extra}
        if profile.enable_thinking:
            kwargs["reasoning_effort"] = profile.reasoning_effort
        return kwargs
    return {"max_tokens": profile.max_tokens}


def _extract_choice_delta_reasoning(chunk: dict[str, Any]) -> str:
    choices = chunk.get("choices") or (chunk.get("chunk") or {}).get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return ""
    rc = delta.get("reasoning_content")
    return rc if isinstance(rc, str) else ""


def _extract_response_reasoning(response: Any, index: int) -> str:
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if index < len(choices) and isinstance(choices[index], dict):
            msg = choices[index].get("message")
            if isinstance(msg, dict) and isinstance(msg.get("reasoning_content"), str):
                return msg["reasoning_content"]
        return ""
    choices = getattr(response, "choices", None) or []
    if index < len(choices):
        msg = getattr(choices[index], "message", None)
        rc = getattr(msg, "reasoning_content", None)
        if not isinstance(rc, str):
            extra = getattr(msg, "model_extra", None)
            rc = extra.get("reasoning_content") if isinstance(extra, dict) else None
        if isinstance(rc, str):
            return rc
    return ""


class _ReasoningChatOpenAI(ChatOpenAI):
    """兼容 DeepSeek/Qwen thinking 模式：保留并在多轮工具调用时回传 reasoning_content。"""

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> Any:
        gen_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if gen_chunk is not None and isinstance(chunk, dict):
            rc = _extract_choice_delta_reasoning(chunk)
            if rc:
                gen_chunk.message.additional_kwargs["reasoning_content"] = rc
        return gen_chunk

    def _create_chat_result(
        self,
        response: Any,
        generation_info: dict | None = None,
    ) -> Any:
        result = super()._create_chat_result(response, generation_info=generation_info)
        for idx, gen in enumerate(result.generations):
            rc = _extract_response_reasoning(response, idx)
            if rc and isinstance(gen.message, AIMessage):
                gen.message.additional_kwargs["reasoning_content"] = rc
        return result

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if self._use_responses_api(payload) or not isinstance(payload.get("messages"), list):
            return payload
        extra_body = payload.get("extra_body")
        thinking_cfg = extra_body.get("thinking") if isinstance(extra_body, dict) else None
        if not (isinstance(thinking_cfg, dict) and thinking_cfg.get("type") == "enabled"):
            return payload
        messages = self._convert_input(input_).to_messages()
        if len(messages) != len(payload["messages"]):
            return payload
        for msg, msg_dict in zip(messages, payload["messages"]):
            if (
                isinstance(msg, AIMessage)
                and isinstance(msg_dict, dict)
                and msg_dict.get("role") == "assistant"
            ):
                rc = (getattr(msg, "additional_kwargs", None) or {}).get("reasoning_content")
                msg_dict["reasoning_content"] = rc if isinstance(rc, str) else ""
        return payload


def make_llm(
    role: str,
    cfg: Settings,
    *,
    temperature: float = 0.2,
    streaming: bool | None = None,
    tier: str = "T1",
    phase: str = "default",
) -> ChatOpenAI:
    provider, model = role_model(role)
    if provider == "deepseek":
        api_key, base_url = cfg.require_deepseek(), cfg.deepseek_base_url
    elif provider == "qwen":
        api_key, base_url = cfg.require_dashscope(), cfg.dashscope_base_url
    else:
        raise ValueError(f"未知 provider: {provider}")
    mode = (getattr(cfg, "llm_thinking_mode", "tiered") or "tiered").strip().lower()
    use_streaming = streaming if streaming is not None else (mode != "legacy")
    step_timeout = (
        float(cfg.request_timeout_sec)
        if mode == "legacy"
        else min(float(cfg.request_timeout_sec), float(getattr(cfg, "llm_step_timeout_sec", 75.0)))
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "temperature": temperature,
        "max_retries": 0,
        "streaming": use_streaming,
        "request_timeout": step_timeout,
    }
    if use_streaming:
        kwargs["stream_usage"] = True
    profile = resolve_thinking_profile(role, tier=tier, phase=phase, mode=mode)
    if profile is not None:
        kwargs.update(_provider_thinking_kwargs(provider, profile))
    return _ReasoningChatOpenAI(**kwargs)


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


def _extract_reasoning_tokens(response: Any, usage: dict) -> int:
    details = usage.get("output_token_details") or {}
    val = int(details.get("reasoning", 0) or 0)
    if val:
        return val
    resp_meta = getattr(response, "response_metadata", None) or {}
    if isinstance(resp_meta, dict):
        token_usage = resp_meta.get("token_usage") or {}
        comp = token_usage.get("completion_tokens_details") or {}
        return int(comp.get("reasoning_tokens", 0) or 0)
    return 0


def track_usage(response: Any, role: str, *, detail: dict | None = None) -> CostEvent | None:
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return None
    _, model = role_model(role)
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    merged_detail: dict[str, Any] = {}
    resp_meta = getattr(response, "response_metadata", None) or {}
    if isinstance(resp_meta, dict) and isinstance(resp_meta.get("codeguard_latency"), dict):
        merged_detail.update(resp_meta["codeguard_latency"])
    reasoning_tokens = _extract_reasoning_tokens(response, usage)
    if reasoning_tokens:
        merged_detail["reasoning_tokens"] = reasoning_tokens
    if detail:
        merged_detail.update(detail)
    return CostEvent(
        model=model,
        input_tokens=inp,
        output_tokens=out,
        cost_cny=estimate_llm_cost(model, inp, out),
        detail=merged_detail,
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


_XML_FUNC_RE = re.compile(
    r"<function=([^>\s]+)\s*>(.*?)(?:</function>|</tool_call>|\Z)",
    re.DOTALL,
)
_XML_PARAM_RE = re.compile(
    r"<parameter=([^>\s]+)\s*>(.*?)(?:</parameter>|(?=<parameter=)|</function>|\Z)",
    re.DOTALL,
)
_XML_JSON_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def _coerce_xml_param(raw: str) -> Any:
    stripped = (raw or "").strip()
    if (
        stripped.startswith(("{", "["))
        or stripped in ("true", "false", "null")
        or re.fullmatch(r"-?\d+", stripped)
    ):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    # 仅剥离紧邻 XML 标签的首尾换行，保留代码参数内部的缩进空白
    trimmed = (raw or "").strip("\r\n")
    return trimmed if "\n" in trimmed else stripped


def parse_xml_tool_action(text: str) -> dict | None:
    """解析 Qwen/Hermes `<tool_call>` XML 工具调用，归一化为 `{"tool": ..., "args": ...}`。"""
    if not text or ("<tool_call" not in text and "<function=" not in text):
        return None
    fn_match = _XML_FUNC_RE.search(text)
    if fn_match:
        name = fn_match.group(1).strip()
        if name:
            args: dict[str, Any] = {}
            for param in _XML_PARAM_RE.finditer(fn_match.group(2)):
                key = param.group(1).strip()
                if key:
                    args[key] = _coerce_xml_param(param.group(2) or "")
            return {"tool": name, "args": args}

    json_match = _XML_JSON_CALL_RE.search(text)
    if json_match:
        try:
            obj = json.loads(json_match.group(1))
        except json.JSONDecodeError:
            return None
        if isinstance(obj, dict):
            name = str(obj.get("name") or obj.get("tool") or "").strip()
            args = obj.get("arguments") if "arguments" in obj else obj.get("args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if name:
                return {"tool": name, "args": args if isinstance(args, dict) else {}}
    return None


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
    "ROLE_MODELS", "ROLE_THINKING_PROFILES", "ThinkingProfile", "resolve_thinking_profile",
    "role_model", "make_llm", "CostEvent", "track_usage",
    "estimate_llm_cost", "parse_json_loose", "parse_dsml_tool_action",
    "parse_xml_tool_action",
]
