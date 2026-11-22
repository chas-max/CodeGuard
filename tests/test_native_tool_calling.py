"""原生 Function/Tool Calling 与双模降级循环测试。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel

from codeguard.agents.base import call_json_with_tools
from codeguard.config import Settings
from codeguard.engine.policy import Policy
from codeguard.tools.registry import build_toolbox
from codeguard.tools.spec import ToolLimits
from tests.fakes import FakeLLM, FakeSandbox


class _Report(BaseModel):
    verdict: str
    evidence: list[str] = []


class _BoundRunnable:
    def __init__(self, parent: "_NativeToolLLM", bound_tools: list[dict]):
        self.parent = parent
        self.bound_tools = bound_tools

    def invoke(self, messages: list[Any]):
        self.parent.recorded_messages.append(messages)
        idx = self.parent.call_count
        self.parent.call_count += 1
        return self.parent.script[idx]


class _NativeToolLLM:
    """模拟支持 `.bind_tools()` 且返回 `AIMessage.tool_calls` 的现代模型。"""

    def __init__(self, script: list[AIMessage]):
        self.script = script
        self.call_count = 0
        self.recorded_tools: list[list[dict]] = []
        self.recorded_messages: list[list[Any]] = []

    def bind_tools(self, tools: list[dict]):
        self.recorded_tools.append(tools)
        return _BoundRunnable(self, tools)

    def invoke(self, messages: list[Any]):
        self.recorded_messages.append(messages)
        idx = self.call_count
        self.call_count += 1
        return self.script[idx]


class _Ctx:
    def __init__(self, llm):
        self.cfg = Settings(mcp_disabled=True)
        self.sandbox = FakeSandbox()
        self._llm = llm

    def llm(self, role: str):
        return self._llm


def _make_box(tmp_path: Path):
    (tmp_path / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def bar():\n    return 2\n", encoding="utf-8")
    return build_toolbox(
        role="semantic_detector",
        root=tmp_path,
        job_id="job-native",
        ctx=_Ctx(None),
        policy=Policy(),
    )


def test_tool_spec_to_openai_tool(tmp_path: Path):
    box = _make_box(tmp_path)
    assert box is not None
    spec = next(s for s in box.specs() if s.name == "ast_symbols")
    schema = spec.to_openai_tool()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "ast_symbols"
    assert "path" in schema["function"]["parameters"]["properties"]


def test_native_parallel_tool_calls_and_tool_messages(tmp_path: Path):
    box = _make_box(tmp_path)
    llm = _NativeToolLLM([
        AIMessage(
            content="",
            tool_calls=[
                {"id": "call_1", "name": "read_file", "args": {"path": "a.py", "start_line": 1, "end_line": 10}},
                {"id": "call_2", "name": "ast_symbols", "args": {"path": "b.py"}},
            ],
        ),
        AIMessage(content=json.dumps({"verdict": "ok", "evidence": ["def foo", "def bar"]})),
    ])
    out, _costs, events = call_json_with_tools(
        _Ctx(llm),
        "semantic_detector",
        "Check a.py and b.py",
        _Report,
        tools=box,
        limits=ToolLimits(max_steps=4, max_tool_calls=6, protocol="auto"),
    )
    assert out.verdict == "ok"
    assert len(events) == 2
    assert all(ev["protocol"] == "native" for ev in events)
    assert events[0]["tool"] == "read_file"
    assert events[1]["tool"] == "ast_symbols"

    # 第二轮请求必须包含：首条 HumanMessage + 上轮 AIMessage + 两条对应 tool_call_id 的 ToolMessage
    second_msgs = llm.recorded_messages[1]
    assert isinstance(second_msgs[0], HumanMessage)
    assert isinstance(second_msgs[1], AIMessage)
    tool_msgs = [m for m in second_msgs if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_msgs] == ["call_1", "call_2"]


def test_native_batch_budget_cutoff_keeps_valid_tool_messages(tmp_path: Path):
    box = _make_box(tmp_path)
    llm = _NativeToolLLM([
        AIMessage(
            content="",
            tool_calls=[
                {"id": "call_a", "name": "read_file", "args": {"path": "a.py"}},
                {"id": "call_b", "name": "read_file", "args": {"path": "b.py"}},
            ],
        ),
        AIMessage(content=json.dumps({"verdict": "limited", "evidence": ["a.py"]})),
    ])
    out, _costs, events = call_json_with_tools(
        _Ctx(llm),
        "semantic_detector",
        "Inspect",
        _Report,
        tools=box,
        limits=ToolLimits(max_steps=4, max_tool_calls=1, protocol="native"),
    )
    assert out.verdict == "limited"
    assert len(events) == 1
    # force-final 调用必须收到两条 ToolMessage，确保 call_b 不悬空
    final_msgs = llm.recorded_messages[1]
    tool_msgs = [m for m in final_msgs if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_msgs] == ["call_a", "call_b"]
    assert "上限" in tool_msgs[1].content


def test_protocol_json_skips_bind_tools_and_fakellm_fallback(tmp_path: Path):
    box = _make_box(tmp_path)
    native_llm = _NativeToolLLM([
        AIMessage(content=json.dumps({"tool": "ast_symbols", "args": {"path": "a.py"}})),
        AIMessage(content=json.dumps({"verdict": "json_mode", "evidence": ["foo"]})),
    ])
    out, _costs, events = call_json_with_tools(
        _Ctx(native_llm),
        "semantic_detector",
        "Inspect",
        _Report,
        tools=box,
        limits=ToolLimits(max_steps=4, max_tool_calls=4, protocol="json"),
    )
    assert out.verdict == "json_mode"
    assert native_llm.recorded_tools == []
    assert events[0]["protocol"] == "json"

    fake_llm = FakeLLM([
        json.dumps({"tool": "read_file", "args": {"path": "a.py"}}),
        json.dumps({"verdict": "fake_ok", "evidence": ["a.py"]}),
    ])
    out2, _costs2, events2 = call_json_with_tools(
        _Ctx(fake_llm),
        "semantic_detector",
        "Inspect",
        _Report,
        tools=box,
        limits=ToolLimits(max_steps=4, max_tool_calls=4, protocol="auto"),
    )
    assert out2.verdict == "fake_ok"
    assert events2[0]["protocol"] == "json"


def test_invoke_with_tools_fails_over_to_fallback_on_timeout(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)

    class _TimeoutLLM:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools: list[dict]):
            return self

        def invoke(self, messages: list[Any]):
            self.calls += 1
            raise TimeoutError("Request timed out.")

    box = _make_box(tmp_path)
    primary = _TimeoutLLM()
    fallback = _NativeToolLLM([
        AIMessage(content=json.dumps({"verdict": "fallback_ok", "evidence": ["recovered"]})),
    ])

    class _MultiRoleCtx:
        def __init__(self):
            self.cfg = Settings(mcp_disabled=True)
            self.sandbox = FakeSandbox()

        def llm(self, role: str):
            return fallback if role == "coder_fallback" else primary

    out, _costs, events = call_json_with_tools(
        _MultiRoleCtx(),
        "coder",
        "Fix bug",
        _Report,
        tools=box,
        limits=ToolLimits(max_steps=4, max_tool_calls=4, protocol="auto"),
    )
    assert out.verdict == "fallback_ok"
    assert events == []
    # 主模型只经历一次 with_retry(2)，绝不会再因 auto 降级重复在同一超时模型上卡死
    assert primary.calls == 2
    assert fallback.call_count == 1
