"""服务层与运行时测试：404 语义、入参校验、崩溃不再静默卡死。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from codeguard.config import load_config
from codeguard.nodes import CodeGuardNodes
from codeguard.runtime import CodeGuardRuntime
from tests._codeguard_fakes import FakeProvider, make_buggy_repo


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    from codeguard import server

    with TestClient(server.app) as c:
        yield c


def test_get_unknown_job_returns_404(client):
    """修复原系统缺陷：对未知 job 返回 404（原实现 get_state 空态导致 404 分支不可达）。"""
    resp = client.get("/api/jobs/does-not-exist")
    assert resp.status_code == 404


def test_start_job_rejects_missing_repo(client, tmp_path):
    resp = client.post("/api/jobs", json={"issue": "bug", "repo_dir": str(tmp_path / "nope")})
    assert resp.status_code == 400


def test_start_job_rejects_empty_issue(client, tmp_path):
    resp = client.post("/api/jobs", json={"issue": "  ", "repo_dir": str(tmp_path)})
    assert resp.status_code == 400


def test_runtime_crash_is_visible_not_silent(tmp_path):
    """修复原系统 P0：invoke 异常必须写入进度并发射 error 事件（进度可见、可诊断）。

    注：全 LLM 失败会走优雅的 escalate 终态（见 test_graph）；此处模拟真正的
    运行时崩溃（如 GraphRecursionError、节点未捕获异常）验证崩溃兜底。
    """
    cfg = load_config(output_dir=tmp_path / "out", checkpoint_path=tmp_path / "ck.db")
    nodes = CodeGuardNodes(cfg, llm_provider=FakeProvider({}))
    rt = CodeGuardRuntime(cfg, nodes=nodes)

    def _boom(_state, _config):
        raise RuntimeError("simulated graph crash")

    rt.graph.invoke = _boom
    with pytest.raises(RuntimeError):
        rt.start("issue", "job-x", str(tmp_path))

    prog = rt.get_progress("job-x")
    assert "执行异常" in prog["status_text"]
    assert prog["percentage"] == 100
