"""pytest 共享夹具。"""
from __future__ import annotations

from pathlib import Path

import pytest

from manga_drama.config import MangaDramaConfig


@pytest.fixture
def mock_cfg(tmp_path: Path) -> MangaDramaConfig:
    """带假 key 的配置，供不需要真实 API 的测试使用。"""
    return MangaDramaConfig(
        deepseek_api_key="fake-deepseek",
        dashscope_api_key="fake-dashscope",
        mimo_api_key="fake-mimo",
        volc_access_key="fake-volc-ak",
        volc_secret_key="fake-volc-sk",
        tavily_api_key=None,          # 测试环境不启用 Tavily
        deepseek_base_url="https://api.deepseek.com",
        dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        mimo_base_url="https://api.mimo.xiaomi.com/v1",
        t2i_provider="wanx",
        tts_provider="cosyvoice",
        tavily_enabled=False,         # 测试环境禁用搜索
        max_clarify=5,
        max_machine=3,
        max_human=3,
        max_anchor=2,
        max_prod=2,
        max_resolve=3,                # 新增
        budget_max_cny=20.0,
        consistency_enabled=True,
        consistency_face_threshold=0.78,
        consistency_body_threshold=0.70,
        ffmpeg_path="ffmpeg",
        output_dir=tmp_path / "drama_output",
        checkpoint_path=tmp_path / "checkpoints.db",
    )


class FakeAIMessage:
    """模拟 LangChain AIMessage，仅含 content 与 usage_metadata。"""

    def __init__(self, content: str, usage: dict | None = None):
        self.content = content
        self.usage_metadata = usage


class FakeLLM:
    """可编程的假 LLM：按调用顺序返回预设响应。

    用法：fake = FakeLLM([resp1, resp2]); fake.invoke(msgs) -> resp1
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def invoke(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if not self._responses:
            raise AssertionError("FakeLLM 预设响应已耗尽")
        return self._responses.pop(0)


@pytest.fixture
def fake_llm_factory():
    """返回 (factory, registry)：factory(role) -> FakeLLM，并记录角色到假 LLM 的映射。"""
    registry: dict[str, FakeLLM] = []

    def _make(role, responses):
        flm = FakeLLM(responses)
        registry.append((role, flm))
        return flm

    return _make, registry
