"""WanxAdapter 文生图适配器测试（mock dashscope，无真实 API）。"""
import pytest

import dashscope
import httpx

from manga_drama.errors import ContentPolicyError, NonRetryableError, RetryableError
from manga_drama.tools.t2i import WanxAdapter, get_t2i


class _Resp:
    def __init__(self, status_code=200, message="", output=None):
        self.status_code = status_code
        self.message = message
        self.output = output


class _FakeHTTP:
    content = b"\x89PNG fake bytes"

    def raise_for_status(self):
        pass


def test_wanx_text_to_image(mock_cfg, monkeypatch, tmp_path):
    captured = {}

    def fake_async_call(**kw):
        from types import SimpleNamespace
        captured.update(kw)
        return _Resp(200, "", SimpleNamespace(task_id="fake-task-123"))

    def fake_wait(task_id):
        return _Resp(200, "", {"results": [{"url": "http://x/a.png"}], "task_status": "SUCCEEDED"})

    monkeypatch.setattr(dashscope.ImageSynthesis, "async_call", fake_async_call)
    monkeypatch.setattr(dashscope.ImageSynthesis, "wait", fake_wait)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeHTTP())

    ad = WanxAdapter(mock_cfg, model="wanx2.1-t2i-turbo")
    out = tmp_path / "a.png"
    p = ad.text_to_image("古风少女", "变形,模糊", 42, "720*1280", out)

    assert p == str(out)
    assert out.read_bytes() == b"\x89PNG fake bytes"
    assert captured["model"] == "wanx2.1-t2i-turbo"
    assert captured["prompt"] == "古风少女"
    assert captured["negative_prompt"] == "变形,模糊"
    assert captured["n"] == 1
    assert captured["size"] == "720*1280"
    assert captured["seed"] == 42


def test_extract_url_content_policy():
    rsp = _Resp(400, "content policy risk detected", None)
    with pytest.raises(ContentPolicyError):
        WanxAdapter._extract_url(rsp)


def test_extract_url_rate_limit_retryable():
    rsp = _Resp(429, "rate limit", None)
    with pytest.raises(RetryableError):
        WanxAdapter._extract_url(rsp)


def test_extract_url_no_results_failed():
    rsp = _Resp(200, "", {"results": [], "task_status": "FAILED"})
    with pytest.raises(NonRetryableError):
        WanxAdapter._extract_url(rsp)


def test_extract_url_no_results_running_retryable():
    rsp = _Resp(200, "", {"results": [], "task_status": "PENDING"})
    with pytest.raises(RetryableError):
        WanxAdapter._extract_url(rsp)


def test_wanx_no_ref_no_edit_support(mock_cfg):
    ad = WanxAdapter(mock_cfg)
    assert ad.supports_reference() is False
    assert ad.supports_edit() is False
    with pytest.raises(NotImplementedError):
        ad.image_to_image("p", None, "ref", 0.6, 1, "s", "out")
    with pytest.raises(NotImplementedError):
        ad.edit_image("p", "base", None, 1, "out")


def test_get_t2i_factory(mock_cfg):
    assert isinstance(get_t2i(mock_cfg), WanxAdapter)
    from dataclasses import replace

    cfg = replace(mock_cfg, t2i_provider="unknown")
    with pytest.raises(NonRetryableError):
        get_t2i(cfg)
