"""分级 Thinking 策略、Provider 覆盖、流式 TTFT/Idle 看门狗与 XML 工具解析测试。"""
from __future__ import annotations

import json
import time

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from pydantic import BaseModel

from codeguard.agents.base import _invoke_llm, call_json_with_tools
from codeguard.config import Settings, load_settings
from codeguard.errors import AgentError, ConfigError, LLMTimeoutError
from codeguard.llm import (
    ROLE_MODELS,
    make_llm,
    parse_xml_tool_action,
    resolve_thinking_profile,
    role_model,
    track_usage,
)
from codeguard.tools.spec import ToolLimits, ToolSpec


def test_settings_thinking_and_timeout_defaults_env_and_validation(tmp_path, monkeypatch):
    cfg = load_settings(dotenv_path=tmp_path / "missing.env")
    assert cfg.llm_thinking_mode == "tiered"
    assert cfg.llm_ttft_timeout_sec == 25.0
    assert cfg.llm_chunk_idle_timeout_sec == 15.0
    assert cfg.llm_step_timeout_sec == 75.0

    monkeypatch.setenv("CODEGUARD_THINKING_MODE", "off")
    monkeypatch.setenv("LLM_TTFT_TIMEOUT_SEC", "12.5")
    monkeypatch.setenv("LLM_CHUNK_IDLE_TIMEOUT_SEC", "8.0")
    monkeypatch.setenv("LLM_STEP_TIMEOUT_SEC", "45.0")
    cfg_env = load_settings(dotenv_path=tmp_path / "missing.env")
    assert cfg_env.llm_thinking_mode == "off"
    assert cfg_env.llm_ttft_timeout_sec == 12.5
    assert cfg_env.llm_chunk_idle_timeout_sec == 8.0
    assert cfg_env.llm_step_timeout_sec == 45.0

    bad_mode = Settings(deepseek_api_key="k", llm_thinking_mode="invalid")
    with pytest.raises(ConfigError, match="CODEGUARD_THINKING_MODE"):
        bad_mode.validate(require_github=False)

    for field in ("llm_ttft_timeout_sec", "llm_chunk_idle_timeout_sec", "llm_step_timeout_sec"):
        bad = Settings(**{"deepseek_api_key": "k", field: 0})
        with pytest.raises(ConfigError, match=field):
            bad.validate(require_github=False)


def test_v2_roles_provider_override_and_fallbacks(monkeypatch):
    v2_roles = (
        "pr_reader",
        "review_logic",
        "review_security",
        "review_architecture",
        "review_tests",
        "coder",
        "qa_checker",
        "summary",
    )
    for role in v2_roles:
        assert f"{role}_fallback" in ROLE_MODELS

    monkeypatch.setenv("CODEGUARD_LLM_PROVIDER", "qwen")
    for role in v2_roles:
        assert role_model(role) == ("qwen", "qwen3.8-flash")
    # coder_fallback 在全局切换为 qwen 时也会随统一 provider 切换，避免 402/不可用 provider 拖垮主路
    assert role_model("coder_fallback") == ("qwen", "qwen3.8-flash")

    monkeypatch.setenv("PROVIDER_CODER_FALLBACK", "deepseek")
    assert role_model("coder_fallback") == ("deepseek", "deepseek-flash")
    monkeypatch.setenv("PROVIDER_QA_CHECKER", "deepseek")
    assert role_model("qa_checker") == ("deepseek", "deepseek-flash")
    assert role_model("coder") == ("qwen", "qwen3.8-flash")


def test_resolve_thinking_profile_v2_roles_tiers_and_tool_step():
    pr_t0 = resolve_thinking_profile("pr_reader", tier="T0", mode="tiered")
    pr_t2 = resolve_thinking_profile("pr_reader", tier="T2", mode="tiered")
    assert pr_t0 is not None and not pr_t0.enable_thinking and pr_t0.max_tokens == 1024
    assert pr_t2 is not None and not pr_t2.enable_thinking and pr_t2.max_tokens == 2048

    qa_t1 = resolve_thinking_profile("qa_checker", tier="T1", mode="tiered")
    qa_t2 = resolve_thinking_profile("qa_checker", tier="T2", mode="tiered")
    assert qa_t1 is not None and not qa_t1.enable_thinking
    assert qa_t2 is not None and qa_t2.enable_thinking and qa_t2.thinking_budget == 512

    sum_t2 = resolve_thinking_profile("summary", tier="T2", mode="tiered")
    assert sum_t2 is not None and not sum_t2.enable_thinking and sum_t2.max_tokens == 2560

    rev_t0 = resolve_thinking_profile("review_logic", tier="T0", mode="tiered")
    rev_t1 = resolve_thinking_profile("review_security", tier="T1", mode="tiered")
    rev_step = resolve_thinking_profile("review_architecture", tier="T2", phase="tool_step", mode="tiered")
    assert rev_t0 is not None and not rev_t0.enable_thinking
    assert rev_t1 is not None and rev_t1.enable_thinking and rev_t1.thinking_budget == 1024
    assert rev_step is not None and rev_step.thinking_budget <= 512 and rev_step.max_tokens <= 2048

    coder_t2 = resolve_thinking_profile("coder", tier="T2", mode="tiered")
    coder_step = resolve_thinking_profile("coder", tier="T2", phase="tool_step", mode="tiered")
    assert coder_t2 is not None and coder_t2.thinking_budget == 2560 and coder_t2.max_tokens == 6144
    assert coder_step is not None and coder_step.thinking_budget <= 512 and coder_step.max_tokens <= 3072

    off_mode = resolve_thinking_profile("coder", tier="T2", mode="off")
    assert off_mode is not None and not off_mode.enable_thinking and off_mode.thinking_budget == 0
    assert resolve_thinking_profile("review_tests", tier="T1", mode="legacy") is None


def test_make_llm_payload_and_reasoning_preservation():
    cfg = Settings(
        deepseek_api_key="ds-test",
        dashscope_api_key="qw-test",
        llm_thinking_mode="tiered",
    )
    coder_t1 = make_llm("coder", cfg, tier="T1")
    qw_payload = coder_t1._get_request_payload([HumanMessage(content="hi")], stop=None)
    assert qw_payload["stream"] is True
    assert qw_payload["extra_body"] == {"enable_thinking": True, "thinking_budget": 1536}
    assert qw_payload["max_completion_tokens"] == 4096

    pr_llm = make_llm("pr_reader_fallback", cfg, tier="T1")
    pr_payload = pr_llm._get_request_payload([HumanMessage(content="hi")], stop=None)
    assert pr_payload["extra_body"] == {"enable_thinking": False}
    assert pr_payload["max_completion_tokens"] == 1536

    rev_t2 = make_llm("review_logic", cfg, tier="T2")
    ds_payload = rev_t2._get_request_payload([HumanMessage(content="hi")], stop=None)
    assert ds_payload["stream"] is True
    assert ds_payload["extra_body"] == {"thinking": {"type": "enabled"}}
    assert ds_payload["max_completion_tokens"] == 4096

    # 流式 delta.reasoning_content 提取与多轮回传
    g1 = rev_t2._convert_chunk_to_generation_chunk(
        {"choices": [{"delta": {"reasoning_content": "step 1 "}, "finish_reason": None}]},
        AIMessageChunk,
        None,
    )
    g2 = rev_t2._convert_chunk_to_generation_chunk(
        {"choices": [{"delta": {"reasoning_content": "step 2", "content": "ok"}, "finish_reason": "stop"}]},
        AIMessageChunk,
        None,
    )
    assert g1 is not None and g2 is not None
    merged = g1.message + g2.message
    assert merged.additional_kwargs["reasoning_content"] == "step 1 step 2"

    history = [
        HumanMessage(content="check"),
        AIMessage(
            content="",
            tool_calls=[{"id": "call_1", "name": "read_file", "args": {"path": "a.py"}}],
            additional_kwargs={"reasoning_content": "step 1 step 2"},
        ),
        ToolMessage(content="code", tool_call_id="call_1", name="read_file"),
    ]
    p_t2 = rev_t2._get_request_payload(history, stop=None)
    assert p_t2["messages"][1]["reasoning_content"] == "step 1 step 2"

    rev_t0 = make_llm("review_logic", cfg, tier="T0")
    p_t0 = rev_t0._get_request_payload(history, stop=None)
    assert "reasoning_content" not in p_t0["messages"][1]

    p_qw = coder_t1._get_request_payload(history, stop=None)
    assert "reasoning_content" not in p_qw["messages"][1]

    legacy_cfg = Settings(deepseek_api_key="ds-test", dashscope_api_key="qw-test", llm_thinking_mode="legacy")
    legacy_llm = make_llm("coder", legacy_cfg)
    leg_payload = legacy_llm._get_request_payload([HumanMessage(content="hi")], stop=None)
    assert leg_payload.get("stream") is False
    assert "extra_body" not in leg_payload


def test_track_usage_and_parse_xml_tool_action():
    resp = AIMessageChunk(
        content='{"verdict": "pass"}',
        usage_metadata={
            "input_tokens": 120,
            "output_tokens": 300,
            "total_tokens": 420,
            "output_token_details": {"reasoning": 220},
        },
        response_metadata={"codeguard_latency": {"ttft_ms": 310, "wall_ms": 920}},
    )
    ev = track_usage(resp, "review_logic", detail={"thinking_profile": "reviewer:t1"})
    assert ev is not None
    assert ev.detail["reasoning_tokens"] == 220
    assert ev.detail["ttft_ms"] == 310
    assert ev.detail["thinking_profile"] == "reviewer:t1"

    xml_text = (
        "<tool_call>\n"
        "<function=edit_file>\n"
        "<parameter=path>src/a.py</parameter>\n"
        "<parameter=old_text>\ndef f():\n    return 1\n</parameter>\n"
        "<parameter=new_text>\ndef f():\n    return 2\n</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    parsed = parse_xml_tool_action(xml_text)
    assert parsed == {
        "tool": "edit_file",
        "args": {
            "path": "src/a.py",
            "old_text": "def f():\n    return 1",
            "new_text": "def f():\n    return 2",
        },
    }

    json_xml = '<tool_call>{"name": "read_file", "arguments": {"path": "b.py"}}</tool_call>'
    assert parse_xml_tool_action(json_xml) == {"tool": "read_file", "args": {"path": "b.py"}}


class _SlowStreamLLM:
    def __init__(self, chunks_fn):
        self.streaming = True
        self._chunks_fn = chunks_fn
        self.calls = 0

    def invoke(self, messages):
        raise AssertionError("streaming LLM 不应退回同步 invoke")

    def stream(self, messages):
        self.calls += 1
        yield from self._chunks_fn(messages)


class _FlakyInvokeLLM:
    def __init__(self, fail_times: int, response: str):
        self.fail_times = fail_times
        self.response = response
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("429 rate limit")
        return AIMessageChunk(content=self.response)


def test_streaming_watchdog_accumulates_ttft_and_fast_fails():
    def _gen(_msgs):
        yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": "step1"})
        yield AIMessageChunk(
            content='{"ok": true}',
            usage_metadata={"input_tokens": 10, "output_tokens": 8, "total_tokens": 18},
        )

    llm = _SlowStreamLLM(_gen)
    resp = _invoke_llm(llm, "review_logic", [HumanMessage(content="x")], ttft_timeout=2.0, idle_timeout=2.0, hard_timeout=5.0)
    assert resp.content == '{"ok": true}'
    assert resp.additional_kwargs["reasoning_content"] == "step1"
    assert (resp.response_metadata.get("codeguard_latency") or {}).get("ttft_ms") is not None

    def _hung(_msgs):
        time.sleep(0.5)
        yield AIMessageChunk(content="late")

    hung_llm = _SlowStreamLLM(_hung)
    with pytest.raises(AgentError) as exc_info:
        _invoke_llm(hung_llm, "coder", [HumanMessage(content="x")], ttft_timeout=0.06, idle_timeout=0.06, hard_timeout=1.0)
    assert isinstance(exc_info.value.__cause__, LLMTimeoutError)
    assert hung_llm.calls == 1

    def _stall(_msgs):
        yield AIMessageChunk(content="partial")
        time.sleep(0.5)
        yield AIMessageChunk(content="end")

    stall_llm = _SlowStreamLLM(_stall)
    with pytest.raises(AgentError) as stall_exc:
        _invoke_llm(stall_llm, "coder", [HumanMessage(content="x")], ttft_timeout=0.5, idle_timeout=0.06, hard_timeout=1.0)
    assert isinstance(stall_exc.value.__cause__, LLMTimeoutError)
    assert stall_llm.calls == 1


def test_transient_non_timeout_retries_and_stream_timeout_fails_over():
    flaky = _FlakyInvokeLLM(fail_times=1, response='{"ok": 1}')
    resp = _invoke_llm(flaky, "review_logic", [HumanMessage(content="x")])
    assert resp.content == '{"ok": 1}'
    assert flaky.calls == 2

    class _Out(BaseModel):
        verdict: str

    class _DummyArgs(BaseModel):
        path: str

    class _DummyBox:
        def specs(self):
            return [ToolSpec(name="read_file", description="read", args_model=_DummyArgs)]

    class _BindableStream(_SlowStreamLLM):
        def bind_tools(self, _tools):
            return self

    def _hung(_msgs):
        time.sleep(0.4)
        yield AIMessageChunk(content="never")

    primary = _BindableStream(_hung)
    fallback = _BindableStream(lambda _m: iter([AIMessageChunk(content=json.dumps({"verdict": "ok"}))]))

    class _Ctx:
        def __init__(self):
            self.cfg = Settings(
                mcp_disabled=True,
                llm_ttft_timeout_sec=0.06,
                llm_chunk_idle_timeout_sec=0.06,
                llm_step_timeout_sec=1.0,
            )

        def llm(self, role: str, *, tier: str = "T1", phase: str = "default"):
            del tier, phase
            return fallback if role == "coder_fallback" else primary

    out, _costs, _events = call_json_with_tools(
        _Ctx(),
        "coder",
        "fix",
        _Out,
        tools=_DummyBox(),
        limits=ToolLimits(max_steps=2, max_tool_calls=2, protocol="auto"),
        tier="T2",
    )
    assert out.verdict == "ok"
    assert primary.calls == 1
    assert fallback.calls == 1
