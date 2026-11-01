"""LLM 工厂：按角色构造 OpenAI 兼容 ChatOpenAI + 成本采集 + JSON 解析兜底。

设计要点（与原漫剧系统一致）：
- 三家厂商（DeepSeek / Qwen / MiMo）均为 OpenAI 兼容，统一走 ChatOpenAI + 按 provider
  切 base_url。max_retries=0，重试交由 retry.with_retry。
- 修复（原系统 P0 缺陷）：增加 request_timeout，防止单请求无限挂起导致任务静默卡死。
- 成本采集：从 AIMessage.usage_metadata 抽 token 用量，按粗略单价估算，回填 state.budget_events。
- JSON 解析：宽松解析（直接 loads -> ```json 围栏 -> 花括号配平），失败返回由调用方兜底。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_openai import ChatOpenAI

from .config import CodeGuardConfig
from .errors import NonRetryableError

# 角色 -> (provider, model)。fallback 角色用于主模型不可用/降级。
ROLE_MODELS: dict[str, tuple[str, str]] = {
    # 四能力①前置：故障解析与澄清
    "understand": ("deepseek", "deepseek-chat"),
    "understand_summary": ("deepseek", "deepseek-chat"),  # 对话历史摘要压缩
    # 四能力②：架构规划（推理模型）与异常统筹
    "director_plan": ("deepseek", "deepseek-reasoner"),
    "director_plan_fallback": ("mimo", "mimo-7b-rl"),
    "director_resolve": ("deepseek", "deepseek-chat"),
    # 四能力③：四阶段补丁编写
    "write": ("qwen", "qwen-max"),
    "write_fallback": ("deepseek", "deepseek-chat"),
    "write_outline": ("qwen", "qwen-max"),
    "write_reflect": ("deepseek", "deepseek-chat"),
    # Tech Lead 监督（两阶段自省）
    "review": ("deepseek", "deepseek-chat"),
    "review_verify": ("deepseek", "deepseek-chat"),
    # 四能力④：F2P 复现测试生成
    "f2p_writer": ("qwen", "qwen-plus"),
    "f2p_writer_fallback": ("deepseek", "deepseek-chat"),
    # 沙箱 ReAct 自愈（Traceback 驱动定向重写）
    "produce_react": ("qwen", "qwen-plus"),
    "produce_react_fallback": ("qwen", "qwen-turbo"),
}

# 粗略单价（元 / 1K tokens），仅用于预算估算。model -> (input, output)。
PRICE_PER_1K_CNY: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.001, 0.002),
    "deepseek-reasoner": (0.004, 0.016),
    "qwen-max": (0.040, 0.120),
    "qwen-plus": (0.0008, 0.002),
    "qwen-turbo": (0.0003, 0.0006),
    "mimo-7b-rl": (0.0, 0.0),  # 占位，按实际填写
}

# 单次 LLM 请求超时（秒）。原系统缺失此配置导致请求可无限挂起、job 静默卡死。
DEFAULT_REQUEST_TIMEOUT_SEC = 120


def role_model(role: str) -> tuple[str, str]:
    """返回角色对应的 (provider, model)。"""
    if role not in ROLE_MODELS:
        raise ValueError(f"未知 LLM 角色: {role}")
    return ROLE_MODELS[role]


def make_llm(
    role: str,
    cfg: CodeGuardConfig,
    *,
    temperature: float = 0.7,
    streaming: bool = False,
) -> ChatOpenAI:
    """按角色构造 ChatOpenAI。

    Args:
        role: ROLE_MODELS 中的角色键。
        cfg: 全局配置（取 provider 的 key 与 base_url）。
        temperature: 采样温度。
        streaming: 是否流式。

    Returns:
        ChatOpenAI 实例。
    """
    provider, model = role_model(role)
    if provider == "deepseek":
        api_key, base_url = cfg.require_deepseek(), cfg.deepseek_base_url
    elif provider == "qwen":
        api_key, base_url = cfg.require_dashscope(), cfg.dashscope_base_url
    elif provider == "mimo":
        api_key, base_url = cfg.require_mimo(), cfg.mimo_base_url
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
    """一次外部调用的成本事件，回填 state.budget_events。"""

    type: str  # "llm"
    model: str
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
    """按粗略单价估算 LLM 成本（元）。"""
    pin, pout = PRICE_PER_1K_CNY.get(model, (0.0, 0.0))
    return pin * input_tokens / 1000.0 + pout * output_tokens / 1000.0


def track_usage(response: Any, role: str) -> CostEvent | None:
    """从 LangChain AIMessage 抽 usage_metadata 生成 CostEvent；无则返回 None。"""
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return None
    _, model = role_model(role)
    inp = int(usage.get("input_tokens", 0))
    out = int(usage.get("output_tokens", 0))
    return CostEvent(
        type="llm",
        model=model,
        input_tokens=inp,
        output_tokens=out,
        cost_cny=estimate_llm_cost(model, inp, out),
    )


def parse_json_loose(text: str) -> dict:
    """宽松解析 LLM 输出中的 JSON 对象。

    依次尝试：直接 json.loads -> 抽取首个 ```json``` 围栏 -> 抽取首个 {...}。
    全部失败抛 NonRetryableError（调用方决定是否兜底为空结构）。
    """
    if not text:
        raise NonRetryableError("空响应，无法解析 JSON")
    s = text.strip()
    # 1. 直接解析
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 2. ```json ... ``` 围栏
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 3. 首个 {...}（花括号配平到最外层闭合）
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
