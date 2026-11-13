from __future__ import annotations

from pathlib import Path

import pytest

from codeguard.config import load_settings
from codeguard.graph import make_checkpointer
from codeguard.models import PullRequestContext
from codeguard.nodes import ReviewNodes
from codeguard.runtime import CodeGuardRuntime
from tests.fakes import FakeGitHub, FakeProvider, FakeSandbox


def _runtime(tmp_path: Path) -> CodeGuardRuntime:
    cfg = load_settings(data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ck.db",
                        sandbox_backend="fake")
    nodes = ReviewNodes(cfg, llm_provider=FakeProvider({}), github=FakeGitHub(), sandbox=FakeSandbox())
    return CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ck.db"))


def test_runtime_crash_is_visible(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.graph.invoke = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated crash"))
    # graph.invoke 是编译对象的属性；直接替换实例属性在 LangGraph 上可能不可写，改用包装。
    try:
        with pytest.raises(RuntimeError):
            rt.start(PullRequestContext(repo="a/b", number=1), "job-crash")
    except TypeError:
        pytest.skip("当前 LangGraph 版本不支持替换 compiled graph.invoke")
    progress = rt.get_progress("job-crash")
    assert "执行异常" in progress["status_text"]
    assert progress["percentage"] == 100
