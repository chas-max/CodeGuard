"""作业级调度：限制同时活跃的 PR 审查 job 数。"""
from __future__ import annotations

import threading


class JobScheduler:
    def __init__(self, max_active: int = 2):
        if max_active <= 0:
            raise ValueError("max_active 必须为正")
        self._sem = threading.BoundedSemaphore(max_active)

    def acquire(self, timeout: float | None = None) -> bool:
        return self._sem.acquire(timeout=timeout)

    def release(self) -> None:
        try:
            self._sem.release()
        except ValueError:
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


__all__ = ["JobScheduler"]
