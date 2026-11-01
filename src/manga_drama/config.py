"""配置加载与校验。

从环境变量（.env）加载。API key 缺失不在此抛错，由 require_* / validate_for_pipeline
按实际使用按需校验--这样测试可注入空 key 跑 mock，真实入口才 fail-fast。
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

# 防死循环封顶（各计数器独立）
DEFAULT_MAX_CLARIFY = 5
DEFAULT_MAX_MACHINE = 3
DEFAULT_MAX_HUMAN = 3
DEFAULT_MAX_ANCHOR = 2
DEFAULT_MAX_PROD = 2
DEFAULT_MAX_RESOLVE = 3  # 导演解决异常最大尝试次数
DEFAULT_BUDGET_MAX_CNY = 20.0

DEFAULT_FACE_THRESHOLD = 0.78
DEFAULT_BODY_THRESHOLD = 0.70


@dataclass(frozen=True)
class MangaDramaConfig:
    """全局配置。frozen 以便在图执行期间不可变。"""

    # API keys（按 provider；缺失为 None，实际调用时校验）
    deepseek_api_key: str | None
    dashscope_api_key: str | None
    mimo_api_key: str | None
    volc_access_key: str | None
    volc_secret_key: str | None
    tavily_api_key: str | None   # Tavily 搜索（文本创作参考资料）
    # base URLs
    deepseek_base_url: str
    dashscope_base_url: str
    mimo_base_url: str
    # provider 选择
    t2i_provider: str  # "wanx" | ...
    tts_provider: str  # "cosyvoice" | "volcano"
    # Tavily
    tavily_enabled: bool  # 是否启用 Tavily 搜索（需有 key）
    # 防死循环封顶
    max_clarify: int
    max_machine: int
    max_human: int
    max_anchor: int
    max_prod: int
    max_resolve: int   # 导演解决异常最大尝试次数
    budget_max_cny: float
    # 一致性
    consistency_enabled: bool
    consistency_face_threshold: float
    consistency_body_threshold: float
    # 路径
    ffmpeg_path: str
    output_dir: Path
    checkpoint_path: Path

    # ---- 按 provider 校验（实际调用时用）----
    def require_deepseek(self) -> str:
        if not self.deepseek_api_key:
            raise MissingConfigError(
                "未设置 DEEPSEEK_API_KEY（需求理解 / 导演 / 机器审核 需要 DeepSeek）"
            )
        return self.deepseek_api_key

    def require_dashscope(self) -> str:
        if not self.dashscope_api_key:
            raise MissingConfigError(
                "未设置 DASHSCOPE_API_KEY（创作 qwen-max / 制作 qwen-plus / "
                "通义万相文生图 / CosyVoice TTS 共用此 key）"
            )
        return self.dashscope_api_key

    def require_mimo(self) -> str:
        if not self.mimo_api_key:
            raise MissingConfigError("未设置 MIMO_API_KEY（导演推理备选）")
        return self.mimo_api_key

    def require_tavily(self) -> str:
        if not self.tavily_api_key:
            raise MissingConfigError(
                "未设置 TAVILY_API_KEY（文本创作参考资料搜索需要 Tavily）"
            )
        return self.tavily_api_key

    def validate_for_pipeline(self) -> None:
        """完整管线所需 provider 启动校验。仅应用入口调用，测试不调。"""
        self.require_deepseek()
        self.require_dashscope()
        if self.tts_provider == "volcano" and not (
            self.volc_access_key and self.volc_secret_key
        ):
            raise MissingConfigError(
                "TTS_PROVIDER=volcano 但未设置 VOLC_ACCESS_KEY / VOLC_SECRET_KEY"
            )


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


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if not v or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def load_config(
    output_dir: Path | None = None,
    checkpoint_path: Path | None = None,
    dotenv_path: Path | None = None,
) -> MangaDramaConfig:
    """从环境变量加载配置。

    Args:
        output_dir: 最终视频输出目录，默认 data/drama_output。
        checkpoint_path: LangGraph checkpoint sqlite 路径，默认 checkpoints.db。
        dotenv_path: .env 路径，默认自动查找。

    Returns:
        MangaDramaConfig。
    """
    if dotenv_path is not None:
        load_dotenv(dotenv_path)
    else:
        load_dotenv()

    out = output_dir or Path(os.environ.get("OUTPUT_DIR", "data/drama_output"))
    ckpt = checkpoint_path or Path(os.environ.get("CHECKPOINT_PATH", "checkpoints.db"))

    tavily_key = os.environ.get("TAVILY_API_KEY") or None
    tavily_enabled = _env_bool("TAVILY_ENABLED", bool(tavily_key))  # 有 key 则默认开启

    return MangaDramaConfig(
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
        dashscope_api_key=os.environ.get("DASHSCOPE_API_KEY") or None,
        mimo_api_key=os.environ.get("MIMO_API_KEY") or None,
        volc_access_key=os.environ.get("VOLC_ACCESS_KEY") or None,
        volc_secret_key=os.environ.get("VOLC_SECRET_KEY") or None,
        tavily_api_key=tavily_key,
        deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL),
        dashscope_base_url=os.environ.get("DASHSCOPE_BASE_URL", DEFAULT_DASHSCOPE_BASE_URL),
        mimo_base_url=os.environ.get("MIMO_BASE_URL", DEFAULT_MIMO_BASE_URL),
        t2i_provider=os.environ.get("T2I_PROVIDER", "wanx").lower(),
        tts_provider=os.environ.get("TTS_PROVIDER", "cosyvoice").lower(),
        tavily_enabled=tavily_enabled,
        max_clarify=_env_int("MAX_CLARIFY", DEFAULT_MAX_CLARIFY),
        max_machine=_env_int("MAX_MACHINE", DEFAULT_MAX_MACHINE),
        max_human=_env_int("MAX_HUMAN", DEFAULT_MAX_HUMAN),
        max_anchor=_env_int("MAX_ANCHOR", DEFAULT_MAX_ANCHOR),
        max_prod=_env_int("MAX_PROD", DEFAULT_MAX_PROD),
        max_resolve=_env_int("MAX_RESOLVE", DEFAULT_MAX_RESOLVE),
        budget_max_cny=_env_float("BUDGET_MAX_CNY", DEFAULT_BUDGET_MAX_CNY),
        consistency_enabled=_env_bool("CONSISTENCY_ENABLED", True),
        consistency_face_threshold=_env_float("CONSISTENCY_FACE_THRESHOLD", DEFAULT_FACE_THRESHOLD),
        consistency_body_threshold=_env_float("CONSISTENCY_BODY_THRESHOLD", DEFAULT_BODY_THRESHOLD),
        ffmpeg_path=os.environ.get("FFMPEG_PATH", "ffmpeg"),
        output_dir=out,
        checkpoint_path=ckpt,
    )
