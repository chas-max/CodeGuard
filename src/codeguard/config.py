"""配置加载与校验（CodeGuard-Agent 版）。

从环境变量（.env）加载。API key 缺失不在此抛错，由 require_* / validate_for_pipeline
按实际使用按需校验——这样测试可注入空 key 跑 mock，真实入口才 fail-fast。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .errors import MissingConfigError

# 默认 base URL（均为 OpenAI 兼容）
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MIMO_BASE_URL = "https://api.mimo.xiaomi.com/v1"

# 防死循环封顶（各计数器独立，沿用原系统默认值）
DEFAULT_MAX_CLARIFY = 5
DEFAULT_MAX_MACHINE = 3
DEFAULT_MAX_HUMAN = 3
DEFAULT_MAX_ANCHOR = 2
DEFAULT_MAX_PROD = 2
DEFAULT_MAX_RESOLVE = 3  # 异常统筹最大尝试次数（路由层硬编码强制检查）
DEFAULT_BUDGET_MAX_CNY = 20.0

# CodeGuard 新增：沙箱与门控参数
DEFAULT_SANDBOX_TIMEOUT_SEC = 30  # 沙箱内单轮 pytest 强制超时（防死循环）
DEFAULT_PER_UNIT_ATTEMPTS = 2  # 沙箱 ReAct 每槽位最大自愈重试
DEFAULT_TOPK_FILES = 3  # AST 故障文件定位 Top-K
DEFAULT_COVERAGE_MIN_PCT = 0.0  # 分支覆盖率门槛（0 = 不做覆盖率门控）
DEFAULT_REQUEST_TIMEOUT_SEC = 120  # 单次 LLM 请求超时


@dataclass(frozen=True)
class CodeGuardConfig:
    """全局配置。frozen 以便在图执行期间不可变。"""

    # API keys（按 provider；缺失为 None，实际调用时校验）
    deepseek_api_key: str | None
    dashscope_api_key: str | None
    mimo_api_key: str | None
    # base URLs
    deepseek_base_url: str
    dashscope_base_url: str
    mimo_base_url: str
    # 防死循环封顶
    max_clarify: int
    max_machine: int
    max_human: int
    max_anchor: int
    max_prod: int
    max_resolve: int
    budget_max_cny: float
    # 沙箱与门控
    sandbox_timeout_sec: int
    per_unit_attempts: int
    topk_files: int
    coverage_min_pct: float
    request_timeout_sec: int
    # 路径
    output_dir: Path
    checkpoint_path: Path

    # ---- 按 provider 校验（实际调用时用）----
    def require_deepseek(self) -> str:
        if not self.deepseek_api_key:
            raise MissingConfigError(
                "未设置 DEEPSEEK_API_KEY（故障理解 / 规划 / 异常统筹 / 监督需要 DeepSeek）"
            )
        return self.deepseek_api_key

    def require_dashscope(self) -> str:
        if not self.dashscope_api_key:
            raise MissingConfigError(
                "未设置 DASHSCOPE_API_KEY（补丁编写 qwen-max / F2P 生成与沙箱自愈 qwen-plus 共用此 key）"
            )
        return self.dashscope_api_key

    def require_mimo(self) -> str:
        if not self.mimo_api_key:
            raise MissingConfigError("未设置 MIMO_API_KEY（规划推理备选）")
        return self.mimo_api_key

    def validate_for_pipeline(self) -> None:
        """完整管线所需 provider 启动校验。仅应用入口调用，测试不调。"""
        self.require_deepseek()
        self.require_dashscope()


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    try:
        return int(v) if v and v.strip() else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    try:
        return float(v) if v and v.strip() else default
    except ValueError:
        return default


def load_config(
    output_dir: Path | None = None,
    checkpoint_path: Path | None = None,
    dotenv_path: Path | None = None,
) -> CodeGuardConfig:
    """从环境变量加载配置。

    Args:
        output_dir: 修复产物输出目录（patch/覆盖率报告），默认 data/codeguard_output。
        checkpoint_path: LangGraph checkpoint sqlite 路径，默认 checkpoints.db。
        dotenv_path: .env 路径，默认自动查找。

    Returns:
        CodeGuardConfig。
    """
    if dotenv_path is not None:
        load_dotenv(dotenv_path)
    else:
        load_dotenv()

    out = output_dir or Path(os.environ.get("OUTPUT_DIR", "data/codeguard_output"))
    ckpt = checkpoint_path or Path(os.environ.get("CHECKPOINT_PATH", "checkpoints.db"))

    return CodeGuardConfig(
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
        dashscope_api_key=os.environ.get("DASHSCOPE_API_KEY") or None,
        mimo_api_key=os.environ.get("MIMO_API_KEY") or None,
        deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL),
        dashscope_base_url=os.environ.get("DASHSCOPE_BASE_URL", DEFAULT_DASHSCOPE_BASE_URL),
        mimo_base_url=os.environ.get("MIMO_BASE_URL", DEFAULT_MIMO_BASE_URL),
        max_clarify=_env_int("MAX_CLARIFY", DEFAULT_MAX_CLARIFY),
        max_machine=_env_int("MAX_MACHINE", DEFAULT_MAX_MACHINE),
        max_human=_env_int("MAX_HUMAN", DEFAULT_MAX_HUMAN),
        max_anchor=_env_int("MAX_ANCHOR", DEFAULT_MAX_ANCHOR),
        max_prod=_env_int("MAX_PROD", DEFAULT_MAX_PROD),
        max_resolve=_env_int("MAX_RESOLVE", DEFAULT_MAX_RESOLVE),
        budget_max_cny=_env_float("BUDGET_MAX_CNY", DEFAULT_BUDGET_MAX_CNY),
        sandbox_timeout_sec=_env_int("SANDBOX_TIMEOUT_SEC", DEFAULT_SANDBOX_TIMEOUT_SEC),
        per_unit_attempts=_env_int("PER_UNIT_ATTEMPTS", DEFAULT_PER_UNIT_ATTEMPTS),
        topk_files=_env_int("TOPK_FILES", DEFAULT_TOPK_FILES),
        coverage_min_pct=_env_float("COVERAGE_MIN_PCT", DEFAULT_COVERAGE_MIN_PCT),
        request_timeout_sec=_env_int("REQUEST_TIMEOUT_SEC", DEFAULT_REQUEST_TIMEOUT_SEC),
        output_dir=out,
        checkpoint_path=ckpt,
    )
