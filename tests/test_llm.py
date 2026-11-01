"""llm 工厂辅助函数测试（不调真实 API）。"""
import pytest

from manga_drama.errors import NonRetryableError
from manga_drama.llm import (
    CostEvent,
    estimate_llm_cost,
    parse_json_loose,
    role_model,
    track_usage,
)


class _FakeAIMessage:
    """模拟 LangChain AIMessage，仅含 content 与 usage_metadata。"""

    def __init__(self, content: str, usage: dict | None = None):
        self.content = content
        self.usage_metadata = usage


def test_role_model_known():
    assert role_model("write") == ("qwen", "qwen-max")
    assert role_model("review") == ("deepseek", "deepseek-chat")


def test_role_model_unknown():
    with pytest.raises(ValueError):
        role_model("nope")


def test_parse_json_plain():
    assert parse_json_loose('{"a": 1}') == {"a": 1}


def test_parse_json_fence():
    s = "noise\n```json\n{\"b\": 2, \"c\": [1, 2]}\n```\ntail"
    assert parse_json_loose(s) == {"b": 2, "c": [1, 2]}


def test_parse_json_plain_fence_no_lang():
    s = "```\n{\"x\": 1}\n```"
    assert parse_json_loose(s) == {"x": 1}


def test_parse_json_embedded():
    assert parse_json_loose('prefix {"c": 3} suffix') == {"c": 3}


def test_parse_json_nested():
    assert parse_json_loose('{"a": {"b": {"c": 1}}}') == {"a": {"b": {"c": 1}}}


def test_parse_json_empty_raises():
    with pytest.raises(NonRetryableError):
        parse_json_loose("")


def test_parse_json_garbage_raises():
    with pytest.raises(NonRetryableError):
        parse_json_loose("not json at all")


def test_track_usage_extracts():
    resp = _FakeAIMessage("ok", usage={"input_tokens": 100, "output_tokens": 50})
    ev = track_usage(resp, "write")
    assert isinstance(ev, CostEvent)
    assert ev.type == "llm"
    assert ev.input_tokens == 100
    assert ev.output_tokens == 50
    # qwen-max: 0.040/1k in, 0.120/1k out
    expected = 0.040 * 100 / 1000 + 0.120 * 50 / 1000
    assert abs(ev.cost_cny - expected) < 1e-9


def test_track_usage_none_when_no_metadata():
    resp = _FakeAIMessage("ok", usage=None)
    assert track_usage(resp, "write") is None


def test_estimate_llm_cost():
    # deepseek-chat: 0.001 in, 0.002 out
    c = estimate_llm_cost("deepseek-chat", 1000, 500)
    expected = 0.001 * 1000 / 1000 + 0.002 * 500 / 1000
    assert abs(c - expected) < 1e-9


def test_cost_event_to_dict():
    ev = CostEvent(type="t2i", model="wanx", cost_cny=0.04, detail={"n": 1})
    d = ev.to_dict()
    assert d["type"] == "t2i"
    assert d["model"] == "wanx"
    assert d["cost_cny"] == 0.04
    assert d["detail"] == {"n": 1}
