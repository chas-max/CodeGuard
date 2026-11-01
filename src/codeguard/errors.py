"""统一异常层次。

设计：可重试错误（瞬时网络/限流）走 tenacity 退避重试；不可重试错误（参数错误、
沙箱环境损坏）立即抛出；预算熔断与配置缺失各自独立类型，便于路由与上层处理。
"""


class CodeGuardError(RuntimeError):
    """所有 CodeGuard 系统异常的基类。"""


class RetryableError(CodeGuardError):
    """可重试的瞬时错误（网络抖动、限流、临时 API 失败）。"""


class NonRetryableError(CodeGuardError):
    """不可重试的错误（参数错误、解析失败已兜底无果、沙箱环境不可用）。"""


class SandboxError(NonRetryableError):
    """沙箱执行环境异常（隔离目录创建失败、pytest 启动失败等）。"""


class BudgetExceededError(CodeGuardError):
    """预算熔断：累计成本超过 BUDGET_MAX_CNY。"""


class MissingConfigError(CodeGuardError):
    """必需的环境变量/配置未设置。"""
