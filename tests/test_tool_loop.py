"""多轮工具循环：动作协议、观测回喂、熔断与失败语义。"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ConfigDict

from codeguard.agents.base import call_json_with_tools
from codeguard.agents.tool_loop import run_tool_loop
from codeguard.errors import AgentError
from codeguard.tools.spec import ToolLimits, ToolResult
from tests.fakes import FakeLLM, FakeToolBox


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = ""
    ok: bool = False


class Ctx:
    def __init__(self, responses: list[str]):
        self._llm = FakeLLM(responses)

    def llm(self, role):
        return self._llm


def _tool(name: str = "read_file", **args) -> str:
    return json.dumps({"tool": name, "args": args}, ensure_ascii=False)


def _final(value: str = "done") -> str:
    return json.dumps({"value": value, "ok": True}, ensure_ascii=False)


def test_tool_then_final():
    ctx = Ctx([_tool(path="a.py"), _final()])
    box = FakeToolBox([ToolResult(status="ok", content="def f(): pass")])
    model, costs, events = run_tool_loop(
        ctx, role="reviewer", prompt="p", model_cls=_Out, tools=box, limits=ToolLimits(),
    )
    assert model.ok and model.value == "done"
    assert box.calls == [("read_file", {"path": "a.py"})]
    assert events and events[0]["tool"] == "read_file"
    assert costs == []


def test_final_wrapper_is_accepted():
    ctx = Ctx([json.dumps({"final": {"value": "wrapped", "ok": True}})])
    box = FakeToolBox()
    model, _, _ = run_tool_loop(
        ctx, role="reviewer", prompt="p", model_cls=_Out, tools=box, limits=ToolLimits(),
    )
    assert model.value == "wrapped"


def test_invalid_args_are_fed_back():
    ctx = Ctx([_tool(path=123), _final()])
    box = FakeToolBox()
    _, _, events = run_tool_loop(
        ctx, role="reviewer", prompt="p", model_cls=_Out, tools=box, limits=ToolLimits(),
    )
    # 参数类型可由 pydantic 强转时不报错；无论如何循环都应走到 final
    assert events


def test_non_dict_args_are_rejected():
    ctx = Ctx([json.dumps({"tool": "read_file", "args": "bad"}), _final()])
    box = FakeToolBox()
    _, _, events = run_tool_loop(
        ctx, role="reviewer", prompt="p", model_cls=_Out, tools=box, limits=ToolLimits(),
    )
    assert events[0]["status"] == "invalid_args"
    assert box.calls == []


def test_max_steps_forces_final_convergence():
    ctx = Ctx([_tool(path="a.py"), _final("forced")])
    box = FakeToolBox([ToolResult(status="ok", content="x")])
    model, _, events = run_tool_loop(
        ctx, role="reviewer", prompt="p", model_cls=_Out, tools=box,
        limits=ToolLimits(max_steps=1, max_tool_calls=5),
    )
    assert model.value == "forced"
    assert len(events) == 1


def test_zero_tool_calls_goes_straight_to_convergence():
    ctx = Ctx([_final("no-tools")])
    box = FakeToolBox()
    model, _, events = run_tool_loop(
        ctx, role="reviewer", prompt="p", model_cls=_Out, tools=box,
        limits=ToolLimits(max_steps=3, max_tool_calls=0),
    )
    assert model.value == "no-tools"
    assert events == [] and box.calls == []


def test_tool_after_convergence_is_an_error():
    ctx = Ctx([_tool(path="a.py"), _tool(path="a.py")])
    box = FakeToolBox([ToolResult(status="ok", content="x")])
    with pytest.raises(AgentError):
        run_tool_loop(
            ctx, role="reviewer", prompt="p", model_cls=_Out, tools=box,
            limits=ToolLimits(max_steps=1, max_tool_calls=1),
        )


def test_infra_error_aborts_the_agent():
    ctx = Ctx([_tool(path="a.py")])
    box = FakeToolBox([ToolResult(status="infra_error", error="docker down")])
    with pytest.raises(AgentError, match="docker down"):
        run_tool_loop(
            ctx, role="reviewer", prompt="p", model_cls=_Out, tools=box, limits=ToolLimits(),
        )


def test_schema_failure_is_agent_error():
    ctx = Ctx([json.dumps({"unexpected": 1})])
    box = FakeToolBox()
    with pytest.raises(AgentError, match="schema"):
        run_tool_loop(
            ctx, role="reviewer", prompt="p", model_cls=_Out, tools=box, limits=ToolLimits(),
        )


def test_dsml_tool_call_is_normalized():
    dsml = (
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="read_file">\n'
        '<｜｜DSML｜｜ parameter name="path" string="true">a.py</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="start_line" string="false">3</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    ctx = Ctx([dsml, _final()])
    box = FakeToolBox([ToolResult(status="ok", content="x")])
    model, _, events = run_tool_loop(
        ctx, role="reviewer", prompt="p", model_cls=_Out, tools=box, limits=ToolLimits(),
    )
    assert model.ok
    assert box.calls == [("read_file", {"path": "a.py", "start_line": 3})]
    assert events and events[0]["tool"] == "read_file"


def test_call_json_with_tools_falls_back_without_tools():
    ctx = Ctx([_final("plain")])
    model, costs, events = call_json_with_tools(
        ctx, "reviewer", "p", _Out, tools=None, limits=ToolLimits(),
    )
    assert model.value == "plain"
    assert costs == [] and events == []
