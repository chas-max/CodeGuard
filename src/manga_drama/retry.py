"""带指数退避的重试装饰器（tenacity）。

沿用项目既有模式：LLM 客户端 max_retries=0，重试统一交由本装饰器，仅对
RetryableError 重试，NonRetryableError 立即抛出，避免无谓消耗。
"""
from __future__ import annotations

from typing import Callable, TypeVar

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .errors import RetryableError

T = TypeVar("T")


def with_retry(
    max_attempts: int = 3,
    base_wait: float = 1.0,
    max_wait: float = 16.0,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """对 RetryableError 重试，指数退避；其它异常立即抛出。

    Args:
        max_attempts: 最大尝试次数（含首次）。
        base_wait: 指数退避基数（秒）。
        max_wait: 单次最长等待（秒）。

    Returns:
        装饰器。
    """
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=base_wait, max=max_wait),
        retry=retry_if_exception_type(RetryableError),
        reraise=True,
    )
