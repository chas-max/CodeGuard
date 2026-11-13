"""测试用 fake 沙箱后端：脚本化返回结果，记录规格。"""
from __future__ import annotations

from collections import deque
from typing import Callable, Iterable

from ..models import SandboxStatus
from .backend import SandboxBackend
from .models import SandboxResult, SandboxSpec


class FakeSandboxBackend(SandboxBackend):
    def __init__(
        self,
        results: Iterable[SandboxResult] | None = None,
        handler: Callable[[SandboxSpec], SandboxResult] | None = None,
    ):
        self._queue = deque(results or [])
        self._handler = handler
        self.specs: list[SandboxSpec] = []

    def run(self, spec: SandboxSpec) -> SandboxResult:
        self.specs.append(spec)
        if self._handler is not None:
            return self._handler(spec)
        if not self._queue:
            return SandboxResult(
                status=SandboxStatus.infra_error, exit_code=-1,
                infra_error="FakeSandboxBackend 结果队列已耗尽",
            )
        return self._queue.popleft()


__all__ = ["FakeSandboxBackend"]
