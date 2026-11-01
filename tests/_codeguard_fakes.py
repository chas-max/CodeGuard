"""CodeGuard 测试基建：FakeLLM（按角色响应队列）与通用构造器。

无需任何 API key：所有节点/图测试通过 FakeProvider 注入预置 JSON 响应；
沙箱相关测试使用 tmp_path 构造的真实微型仓库（真实跑 pytest 子进程）。
"""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from codeguard.config import load_config
from codeguard.nodes import CodeGuardNodes

# 预置响应片段（各测试按需取用/改写）
PARSE_CLEAR = (
    '{"title": "divide_safe 除零崩溃", "symptom": "divide_safe(10, 0) 抛 ZeroDivisionError，'
    '期望返回 0.0", "stack_trace": "ZeroDivisionError: division by zero at calc.py:2", '
    '"suspected_modules": ["divide_safe"], "repro_steps": ["调用 divide_safe(10, 0)"], '
    '"env_hints": "", "missing_info": [], "completeness": "clear"}'
)
PARSE_PARTIAL = (
    '{"title": "计算崩溃", "symptom": "有时抛 ZeroDivisionError", "stack_trace": "", '
    '"suspected_modules": [], "repro_steps": [], "env_hints": "", '
    '"missing_info": ["哪个函数在什么入参下崩溃？"], "completeness": "partial"}'
)
PLAN_OK = (
    '{"plan_summary": "calc.py 的 divide_safe 未处理 b=0", '
    '"repair_strategy": [{"target_file": "calc.py", "test_file": "test_repro_issue.py", '
    '"hypothesis": "divide_safe 缺少除零保护", "change_sketch": "b==0 时返回 0.0"}]}'
)
FIXED_CALC = (
    "def divide_safe(a: float, b: float) -> float:\n"
    "    if b == 0:\n"
    "        return 0.0\n"
    "    return a / b\n"
)
WRITE_OK = json.dumps({"patched_code": FIXED_CALC, "change_note": "补除零保护"}, ensure_ascii=False)
WRITE_REFLECT_OK = '{"consistent": true, "issues": []}'
SUPERVISE_APPROVE = '{"decision": "approve", "feedback": "边界处理正确", "boundary_risks": []}'
SUPERVISE_VERIFY_OK = '{"confirmed": true, "revised_decision": "approve", "revised_feedback": "", "reason": "复核通过"}'
F2P_OK = (
    '{"test_file": "test_repro_issue.py", '
    '"test_code": "from calc import divide_safe\\n\\n\\n'
    'def test_bug_repro():\\n    assert divide_safe(10, 0) == 0.0\\n", '
    '"node_ids": ["test_repro_issue.py::test_bug_repro"]}'
)


class FakeResponse:
    """模拟 LangChain AIMessage 的最小接口。"""

    def __init__(self, content: str):
        self.content = content
        self.usage_metadata = None


class FakeLLM:
    """按队列依次弹出预置响应；耗尽即抛错（暴露测试映射缺失）。"""

    def __init__(self, responses: list[str]):
        self._queue = deque(responses)

    def invoke(self, messages) -> FakeResponse:
        if not self._queue:
            raise RuntimeError(f"FakeLLM 响应队列已耗尽（prompt 开头: {messages[0].content[:60]!r}）")
        return FakeResponse(self._queue.popleft())


class FakeProvider:
    """role -> 响应列表 的映射式 LLM 提供方。"""

    def __init__(self, mapping: dict[str, list[str]]):
        self._mapping = mapping

    def __call__(self, role: str) -> FakeLLM:
        return FakeLLM(list(self._mapping.get(role, [])))


def make_nodes(tmp_path: Path, mapping: dict[str, list[str]]) -> CodeGuardNodes:
    """构造带 FakeLLM 的节点容器（无 API key、无 SSE runtime）。"""
    cfg = load_config(output_dir=tmp_path / "out", checkpoint_path=tmp_path / "ckpt.db")
    return CodeGuardNodes(cfg, llm_provider=FakeProvider(mapping))


def make_buggy_repo(tmp_path: Path) -> Path:
    """构造含除零 Bug 的微型仓库（含既有 P2P 测试）。"""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "calc.py").write_text(
        "def divide_safe(a: float, b: float) -> float:\n"
        "    return a / b\n",
        encoding="utf-8",
    )
    (repo / "test_calc.py").write_text(
        "from calc import divide_safe\n\n"
        "def test_normal_divide():\n"
        "    assert divide_safe(10, 2) == 5.0\n",
        encoding="utf-8",
    )
    return repo


HAPPY_MAPPING = {
    "understand": [PARSE_CLEAR],
    "director_plan": [PLAN_OK],
    "write_outline": ['{"outline": "1. 为 divide_safe 增加 b==0 保护"}'],
    "write": [WRITE_OK],
    "write_reflect": [WRITE_REFLECT_OK],
    "review": [SUPERVISE_APPROVE],
    "review_verify": [SUPERVISE_VERIFY_OK],
    "f2p_writer": [F2P_OK],
}
