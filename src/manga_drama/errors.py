"""统一异常层次。

设计：可重试错误（瞬时网络/限流）走 tenacity 退避重试；不可重试错误（参数错误、
内容策略拒绝）立即抛出；预算熔断与配置缺失各自独立类型，便于路由与上层处理。
"""


class MangaDramaError(RuntimeError):
    """所有漫剧系统异常的基类。"""


class RetryableError(MangaDramaError):
    """可重试的瞬时错误（网络抖动、限流、临时 API 失败）。"""


class NonRetryableError(MangaDramaError):
    """不可重试的错误（参数错误、内容违规拒绝、解析失败已兜底无果）。"""


class ContentPolicyError(NonRetryableError):
    """内容被服务方策略拒绝（文生图安全策略、审核拦截）。"""


class BudgetExceededError(MangaDramaError):
    """预算熔断：累计成本超过 BUDGET_MAX_CNY。"""


class MissingConfigError(MangaDramaError):
    """必需的环境变量/配置未设置。"""
