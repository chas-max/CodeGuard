from __future__ import annotations

import time

import pytest

from codeguard.errors import CoordinatorError
from codeguard.orchestration.coordinator import Coordinator
from codeguard.orchestration.tasks import Task, TaskStatus


def test_parallel_tasks_are_faster_than_serial():
    def sleepy():
        time.sleep(0.2)
        return 1

    tasks = [Task(f"t{i}", "s", sleepy, timeout_sec=2) for i in range(3)]
    outcome = Coordinator(max_parallel=3).run_stage(tasks)
    assert outcome.wall_ms < 450
    assert all(r.status == TaskStatus.ok for r in outcome.results.values())


def test_dependencies_order():
    order: list[str] = []

    def make(name):
        def fn():
            order.append(name)
            return name
        return fn

    tasks = [
        Task("a", "s", make("a")),
        Task("b", "s", make("b"), depends_on=("a",)),
        Task("c", "s", make("c"), depends_on=("b",)),
    ]
    outcome = Coordinator(max_parallel=3).run_stage(tasks)
    assert outcome.ok
    assert order == ["a", "b", "c"]


def test_duplicate_id_and_single_writer():
    with pytest.raises(CoordinatorError):
        Coordinator().run_stage([Task("x", "s", lambda: 1), Task("x", "s", lambda: 1)])
    with pytest.raises(CoordinatorError):
        Coordinator().run_stage([
            Task("x", "s", lambda: 1, writes_workspace=True),
            Task("y", "s", lambda: 1, writes_workspace=True),
        ])


def test_timeout_marked():
    def slow():
        time.sleep(0.3)
        return 1

    outcome = Coordinator(max_parallel=2).run_stage([Task("slow", "s", slow, timeout_sec=1)])
    # 正常完成；再用极小 timeout 验证标记路径
    assert outcome.results["slow"].status == TaskStatus.ok
    outcome2 = Coordinator(max_parallel=2).run_stage([Task("slow2", "s", slow, timeout_sec=0.05)])
    assert outcome2.results["slow2"].status == TaskStatus.timeout


def test_budget_cancels():
    tasks = [Task(f"t{i}", "s", lambda: 1) for i in range(5)]
    outcome = Coordinator(max_parallel=1).run_stage(tasks, budget_exceeded=lambda: True)
    assert outcome.errors and "budget_exceeded" in outcome.errors
