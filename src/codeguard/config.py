"""配置加载与启动校验。

所有配置从环境变量加载；测试通过 `load_settings()` 后注入 fake 依赖，不要求
API key / Docker。真实入口调用 `Settings.validate()` 做 fail-fast，避免半可用状态。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigError, MissingConfigError
from .models import MergeMode

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """不可变的全局配置。测试使用 dataclasses.replace 派生。"""

    # GitHub
    github_token: str | None = None
    github_webhook_secret: str | None = None
    github_api_url: str = "https://api.github.com"
    github_repos: tuple[str, ...] = ()  # 无 token 时前端可选仓库（CODEGUARD_REPOS）
    github_auth_mode: str = "auto"  # auto | pat | app
    github_app_store: Path = field(default_factory=lambda: Path("data/github-app.json"))
    public_url: str = ""  # GitHub App manifest / webhook 依赖的公网 HTTPS 地址
    console_admin_token: str = ""  # 非本机访问 App 写接口时的管理令牌
    target_branch: str | None = None
    merge_mode: MergeMode = MergeMode.auto
    merge_method: str = "squash"
    push_fixes: bool = True
    close_on_no_issue: bool = False
    review_mode: str = ""  # 空 = 由仓库策略 mode 决定；internal/external 强制覆盖
    post_review: bool = True
    post_merge_watch_hours: int = 72

    # LLM
    deepseek_api_key: str | None = None
    dashscope_api_key: str | None = None
    deepseek_base_url: str = DEFAULT_DEEPSEEK_BASE_URL
    dashscope_base_url: str = DEFAULT_DASHSCOPE_BASE_URL
    budget_max_cny: float = 10.0
    request_timeout_sec: int = 120

    # limits
    max_pr_files: int = 50
    max_changed_lines: int = 3000
    max_diff_bytes: int = 2_000_000
    max_fix_rounds: int = 2
    max_review_rounds: int = 2
    max_job_seconds: int = 1200
    reject_confidence: float = 0.85

    # coordinator
    coordinator_max_parallel: int = 6
    llm_max_concurrency: int = 4
    sandbox_max_concurrency: int = 2
    job_max_active: int = 2

    # sandbox
    sandbox_backend: str = "docker"  # docker | fake (fake only for tests/dev)
    sandbox_image: str = "codeguard-sandbox-python:3.12"
    sandbox_timeout_sec: int = 300
    sandbox_memory: str = "1g"
    sandbox_cpus: float = 1.0
    sandbox_pids: int = 256
    sandbox_allow_prepare_network: bool = False

    # agent tools（策略默认开启；环境变量只能收紧）
    agent_tools_disabled: bool = False
    agent_tools_max_capability: str = ""  # 空 = 不收紧；none/read/exec/write
    mcp_disabled: bool = False
    mcp_max_capability: str = ""  # 空 = 不收紧；none/read/exec/write

    # paths
    data_dir: Path = field(default_factory=lambda: Path("data/jobs"))
    checkpoint_path: Path = field(default_factory=lambda: Path("checkpoints.db"))
    reports_dir: Path = field(default_factory=lambda: Path("reports"))
    policy_path: str = ".codeguard/policy.yml"

    # ---- provider checks ----
    def require_deepseek(self) -> str:
        if not self.deepseek_api_key:
            raise MissingConfigError("未设置 DEEPSEEK_API_KEY")
        return self.deepseek_api_key

    def require_dashscope(self) -> str:
        if not self.dashscope_api_key:
            raise MissingConfigError("未设置 DASHSCOPE_API_KEY")
        return self.dashscope_api_key

    def validate(self, *, require_docker: bool = False, require_github: bool = True) -> None:
        """收集配置错误后一次性抛出，避免逐个字段排障。"""
        errors: list[str] = []
        if self.github_auth_mode not in ("auto", "pat", "app"):
            errors.append(f"非法 GITHUB_AUTH_MODE: {self.github_auth_mode}")
        # App 模式下凭据运行期从 data/github-app.json 加载，不能再要求 PAT。
        if require_github and self.github_auth_mode == "pat" and not self.github_token:
            errors.append("未设置 GITHUB_TOKEN")
        if self.public_url and not self.public_url.startswith("https://"):
            errors.append("CODEGUARD_PUBLIC_URL 必须以 https:// 开头")
        if self.github_webhook_secret and len(self.github_webhook_secret) < 16:
            errors.append("GITHUB_WEBHOOK_SECRET 至少 16 字符")
        if self.merge_mode not in (MergeMode.auto, MergeMode.human):
            errors.append(f"非法 MERGE_MODE: {self.merge_mode}")
        if self.review_mode not in ("", "internal", "external"):
            errors.append(f"非法 REVIEW_MODE: {self.review_mode}")
        if self.merge_method not in ("squash", "merge", "rebase"):
            errors.append(f"非法 MERGE_METHOD: {self.merge_method}")
        if not (0 <= self.reject_confidence <= 1):
            errors.append("REJECT_CONFIDENCE 必须在 [0,1]")
        for name in (
            "max_pr_files", "max_changed_lines", "max_fix_rounds", "max_review_rounds",
            "coordinator_max_parallel", "llm_max_concurrency", "sandbox_max_concurrency",
        ):
            if getattr(self, name) <= 0:
                errors.append(f"{name} 必须为正整数")
        if self.sandbox_backend not in ("docker", "fake"):
            errors.append("SANDBOX_BACKEND 只允许 docker/fake")
        if self.agent_tools_max_capability not in ("", "none", "read", "exec", "write"):
            errors.append("AGENT_TOOLS_MAX_CAPABILITY 只允许 none/read/exec/write")
        if self.mcp_max_capability not in ("", "none", "read", "exec", "write"):
            errors.append("CODEGUARD_MCP_MAX_CAPABILITY 只允许 none/read/exec/write")
        if not self.deepseek_api_key and not self.dashscope_api_key:
            errors.append("至少配置一个 LLM provider 的 API key")
        if require_docker and self.sandbox_backend == "docker":
            from .sandbox.health import check_docker

            ok, message = check_docker()
            if not ok:
                errors.append(f"Docker 不可用: {message}")
        if errors:
            raise ConfigError("；".join(errors))


def load_settings(
    *,
    dotenv_path: Path | None = None,
    data_dir: Path | None = None,
    checkpoint_path: Path | None = None,
    **overrides,
) -> Settings:
    """从 .env / 环境变量加载；overrides 仅供测试与 CLI 注入。"""
    if dotenv_path is not None:
        load_dotenv(dotenv_path)
    else:
        load_dotenv()

    merge_raw = _env_str("MERGE_MODE", "auto").lower()
    merge_mode = MergeMode.human if merge_raw == "human" else MergeMode.auto

    values = dict(
        github_token=_env_str("GITHUB_TOKEN") or None,
        github_webhook_secret=_env_str("GITHUB_WEBHOOK_SECRET") or None,
        github_api_url=_env_str("GITHUB_API_URL", "https://api.github.com"),
        github_repos=tuple(
            item.strip()
            for item in _env_str("CODEGUARD_REPOS").split(",")
            if item.strip()
        ),
        github_auth_mode=_env_str("GITHUB_AUTH_MODE", "auto").lower(),
        github_app_store=Path(_env_str("GITHUB_APP_STORE", "data/github-app.json")),
        public_url=_env_str("CODEGUARD_PUBLIC_URL", "").rstrip("/"),
        console_admin_token=_env_str("CODEGUARD_ADMIN_TOKEN"),
        target_branch=_env_str("TARGET_BRANCH") or None,
        merge_mode=merge_mode,
        merge_method=_env_str("MERGE_METHOD", "squash"),
        push_fixes=_env_bool("PUSH_FIXES", True),
        close_on_no_issue=_env_bool("CLOSE_ON_NO_ISSUE", False),
        review_mode=_env_str("REVIEW_MODE", ""),
        post_review=_env_bool("POST_REVIEW", True),
        post_merge_watch_hours=_env_int("POST_MERGE_WATCH_HOURS", 72),
        deepseek_api_key=_env_str("DEEPSEEK_API_KEY") or None,
        dashscope_api_key=_env_str("DASHSCOPE_API_KEY") or None,
        deepseek_base_url=_env_str("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL),
        dashscope_base_url=_env_str("DASHSCOPE_BASE_URL", DEFAULT_DASHSCOPE_BASE_URL),
        budget_max_cny=_env_float("BUDGET_MAX_CNY", 10.0),
        request_timeout_sec=_env_int("REQUEST_TIMEOUT_SEC", 120),
        max_pr_files=_env_int("MAX_PR_FILES", 50),
        max_changed_lines=_env_int("MAX_CHANGED_LINES", 3000),
        max_diff_bytes=_env_int("MAX_DIFF_BYTES", 2_000_000),
        max_fix_rounds=_env_int("MAX_FIX_ROUNDS", 2),
        max_review_rounds=_env_int("MAX_REVIEW_ROUNDS", 2),
        max_job_seconds=_env_int("MAX_JOB_SECONDS", 1200),
        reject_confidence=_env_float("REJECT_CONFIDENCE", 0.85),
        coordinator_max_parallel=_env_int("COORDINATOR_MAX_PARALLEL", 6),
        llm_max_concurrency=_env_int("LLM_MAX_CONCURRENCY", 4),
        sandbox_max_concurrency=_env_int("SANDBOX_MAX_CONCURRENCY", 2),
        job_max_active=_env_int("JOB_MAX_ACTIVE", 2),
        sandbox_backend=_env_str("SANDBOX_BACKEND", "docker"),
        sandbox_image=_env_str("SANDBOX_IMAGE", "codeguard-sandbox-python:3.12"),
        sandbox_timeout_sec=_env_int("SANDBOX_TIMEOUT_SEC", 300),
        sandbox_memory=_env_str("SANDBOX_MEMORY", "1g"),
        sandbox_cpus=_env_float("SANDBOX_CPUS", 1.0),
        sandbox_pids=_env_int("SANDBOX_PIDS", 256),
        sandbox_allow_prepare_network=_env_bool("SANDBOX_ALLOW_PREPARE_NETWORK", False),
        agent_tools_disabled=_env_bool("AGENT_TOOLS_DISABLED", False),
        agent_tools_max_capability=_env_str("AGENT_TOOLS_MAX_CAPABILITY", "").lower(),
        mcp_disabled=_env_bool("CODEGUARD_MCP_DISABLED", False),
        mcp_max_capability=_env_str("CODEGUARD_MCP_MAX_CAPABILITY", "").lower(),
        data_dir=data_dir or Path(_env_str("DATA_DIR", "data/jobs")),
        checkpoint_path=checkpoint_path or Path(_env_str("CHECKPOINT_PATH", "checkpoints.db")),
        reports_dir=Path(_env_str("REPORTS_DIR", "reports")),
        policy_path=_env_str("POLICY_PATH", ".codeguard/policy.yml"),
    )
    values.update(overrides)
    return Settings(**values)


def with_overrides(settings: Settings, **overrides) -> Settings:
    return replace(settings, **overrides)


__all__ = ["Settings", "load_settings", "with_overrides"]
