"""确定性协调器：阶段内任务 DAG 并行、超时、预算、取消与结果合并。

注意：LangGraph 节点是同步函数；这里用 ThreadPoolExecutor 并行执行同步 Agent/工具。
任务内部仍必须自带超时（LLM request_timeout / Docker wait timeout / httpx timeout），
因为 Python 线程不能被强制 kill；协调器负责标记超时并取消未开始任务。
"""
from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable

from ..errors import CoordinatorError
from .tasks import Task, TaskResult, TaskStatus


@dataclass
class StageOutcome:
    stage: str
    results: dict[str, TaskResult] = field(default_factory=dict)
    timed_out: bool = False
    cancelled: bool = False
    wall_ms: int = 0
    errors: list[str] = field(default_factory=list)

    def get(self, task_id: str) -> TaskResult | None:
        return self.results.get(task_id)

    @property
    def ok(self) -> bool:
        return all(r.status == TaskStatus.ok for r in self.results.values()) and not self.timed_out

    def values(self) -> dict:
        return {k: v.value for k, v in self.results.items() if v.status == TaskStatus.ok}


class Coordinator:
    def __init__(
        self,
        *,
        max_parallel: int = 6,
        default_timeout_sec: int = 120,
    ):
        if max_parallel <= 0:
            raise ValueError("max_parallel 必须为正")
        self.max_parallel = max_parallel
        self.default_timeout_sec = default_timeout_sec

    def run_stage(
        self,
        tasks: list[Task],
        *,
        deadline: float | None = None,
        budget_exceeded: Callable[[], bool] | None = None,
    ) -> StageOutcome:
        if not tasks:
            return StageOutcome(stage="")
        stage = tasks[0].stage
        by_id: dict[str, Task] = {}
        for task in tasks:
            if task.id in by_id:
                raise CoordinatorError(f"重复 task id: {task.id}")
            by_id[task.id] = task
        writers = [t.id for t in tasks if t.writes_workspace]
        if len(writers) > 1:
            raise CoordinatorError(f"同一 stage 只允许一个写任务: {writers}")
        dependents: dict[str, list[str]] = {tid: [] for tid in by_id}
        remaining_deps: dict[str, set[str]] = {}
        for task in tasks:
            deps = set(task.depends_on)
            missing = deps - set(by_id)
            if missing:
                raise CoordinatorError(f"task {task.id} 依赖不存在: {sorted(missing)}")
            remaining_deps[task.id] = deps
            for dep in deps:
                dependents[dep].append(task.id)
        ready = [tid for tid, deps in remaining_deps.items() if not deps]
        if not ready and tasks:
            raise CoordinatorError("任务依赖存在环")

        outcome = StageOutcome(stage=stage)
        started = time.monotonic()
        running: dict[Future, tuple[str, float]] = {}
        submitted: set[str] = set()
        completed: set[str] = set()

        def _run(task: Task) -> TaskResult:
            t0 = time.monotonic()
            attempt = 0
            last_error = ""
            while attempt <= task.retries:
                attempt += 1
                try:
                    value = task.fn()
                    return TaskResult(
                        task_id=task.id, status=TaskStatus.ok, value=value,
                        duration_ms=int((time.monotonic() - t0) * 1000), attempts=attempt,
                    )
                except Exception as exc:  # noqa: BLE001 - 任务失败由调用方决定
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt > task.retries:
                        break
            return TaskResult(
                task_id=task.id, status=TaskStatus.failed, error=last_error,
                duration_ms=int((time.monotonic() - t0) * 1000), attempts=attempt,
            )

        executor = ThreadPoolExecutor(max_workers=self.max_parallel, thread_name_prefix="cg-coord")
        try:
            while ready or running:
                if deadline is not None and time.monotonic() > deadline:
                    outcome.timed_out = True
                    break
                if budget_exceeded and budget_exceeded():
                    outcome.errors.append("budget_exceeded")
                    break

                while ready and len(running) < self.max_parallel:
                    tid = ready.pop(0)
                    if tid in submitted or tid in completed:
                        continue
                    submitted.add(tid)
                    fut = executor.submit(_run, by_id[tid])
                    running[fut] = (tid, time.monotonic())

                if not running:
                    if ready:
                        continue
                    break

                done, _ = wait(list(running), timeout=0.05, return_when=FIRST_COMPLETED)
                now = time.monotonic()
                # 任务级超时：仅标记；运行中的线程无法强杀，依赖任务内部超时。
                for fut, (tid, t_start) in list(running.items()):
                    timeout = by_id[tid].timeout_sec or self.default_timeout_sec
                    if now - t_start > timeout and fut not in done:
                        fut.cancel()
                        outcome.results[tid] = TaskResult(
                            task_id=tid, status=TaskStatus.timeout,
                            error=f"timeout after {timeout}s",
                            duration_ms=int((now - t_start) * 1000),
                        )
                        completed.add(tid)
                        running.pop(fut, None)
                        outcome.errors.append(f"{tid}: timeout")
                for fut in done:
                    tid, _ = running.pop(fut)
                    try:
                        result = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        result = TaskResult(task_id=tid, status=TaskStatus.failed, error=str(exc))
                    outcome.results[tid] = result
                    completed.add(tid)
                    if result.status != TaskStatus.ok:
                        outcome.errors.append(f"{tid}: {result.error or result.status}")
                    if result.status == TaskStatus.timeout:
                        outcome.timed_out = True
                for tid in list(completed):
                    for child in dependents.get(tid, []):
                        if child in submitted or child in completed:
                            continue
                        remaining_deps[child].discard(tid)
                        if not remaining_deps[child] and child not in ready:
                            ready.append(child)
        finally:
            for fut in list(running):
                fut.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
        outcome.wall_ms = int((time.monotonic() - started) * 1000)
        return outcome


__all__ = ["Coordinator", "StageOutcome"]
