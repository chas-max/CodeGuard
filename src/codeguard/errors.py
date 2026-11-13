"""统一异常层次。"""

from __future__ import annotations


class CodeGuardError(RuntimeError):
    """所有 CodeGuard 系统异常的基类。"""


class RetryableError(CodeGuardError):
    """可重试的瞬时错误（网络抖动、限流、临时 API 失败）。"""


class NonRetryableError(CodeGuardError):
    """不可重试的错误（参数错误、解析失败已兜底无果、沙箱环境不可用）。"""


class ConfigError(CodeGuardError):
    """配置缺失或非法。"""


class MissingConfigError(ConfigError):
    """必需的配置项未设置。"""


class PolicyError(ConfigError):
    """策略文件非法或不可用。"""


class SandboxError(NonRetryableError):
    """沙箱环境异常（Docker 不可用、容器启动失败等）。"""


class GitHubError(NonRetryableError):
    """GitHub API 或 webhook 异常。"""


class GitHubAuthError(GitHubError):
    """GitHub App 凭据、签名或 installation token 不可用。"""


class AgentError(CodeGuardError):
    """Agent 输出不合法或调用失败。"""


class CoordinatorError(CodeGuardError):
    """协调器调度失败（依赖环、单写者冲突等）。"""


class BudgetExceededError(CodeGuardError):
    """预算熔断。"""


__all__ = [
    "CodeGuardError", "RetryableError", "NonRetryableError", "ConfigError",
    "MissingConfigError", "PolicyError", "SandboxError", "GitHubError",
    "GitHubAuthError", "AgentError", "CoordinatorError", "BudgetExceededError",
]
