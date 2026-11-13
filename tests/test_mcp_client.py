"""MCP Client 隔离、动态建模、4 个真实 stdio MCP Server 握手与端点测试。"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from codeguard.engine.policy import McpServerPolicy, Policy, default_mcp_servers
from codeguard.mcp import McpClientManager, build_stdio_env, schema_to_args_model


def test_build_stdio_env_isolates_llm_secrets(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-deepseek")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-secret-dashscope")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-allowed-token")

    srv = McpServerPolicy(
        name="github_context",
        transport="stdio",
        command="python",
        args=["-m", "codeguard.mcp.servers.github_server"],
        env_vars=["GITHUB_TOKEN"],
    )
    env = build_stdio_env(srv, context_env={"CODEGUARD_REPO_ROOT": "/tmp/repo"})
    assert "DEEPSEEK_API_KEY" not in env
    assert "DASHSCOPE_API_KEY" not in env
    assert env["GITHUB_TOKEN"] == "ghp-allowed-token"
    assert env["CODEGUARD_REPO_ROOT"] == "/tmp/repo"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_schema_to_args_model_validation():
    Model = schema_to_args_model(
        "query_osv",
        {
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "包名"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["package"],
        },
    )
    obj = Model.model_validate({"package": "pyyaml"})
    assert obj.package == "pyyaml"
    assert obj.limit == 5


def test_real_stdio_probe_all_four_builtin_servers():
    mgr = McpClientManager()
    policy = Policy()
    statuses = mgr.probe_all(policy.agents.mcp, force_stdio=True)
    by_name = {s.name: s for s in statuses}
    assert set(by_name) == {"osv_security", "git_history", "knowledge", "github_context"}
    for name, st in by_name.items():
        assert st.ok, f"Server {name} probe failed: {st.error}"
        assert st.tools_count >= 2
    assert "query_osv_vulnerability" in by_name["osv_security"].tools
    assert "git_blame" in by_name["git_history"].tools
    assert "inspect_package_api" in by_name["knowledge"].tools
    assert "get_issue" in by_name["github_context"].tools


def test_real_stdio_call_tools_and_security_redlines(tmp_path: Path):
    mgr = McpClientManager()
    servers = {s.name: s for s in default_mcp_servers()}

    # 1. osv_security: scan_manifest_dependencies
    req_file = tmp_path / "requirements.txt"
    req_file.write_text("pyyaml==5.3\nrequests==2.32.3\n", encoding="utf-8")
    osv_res = mgr.call_tool(
        servers["osv_security"],
        "scan_manifest_dependencies",
        {"manifest_path": "requirements.txt"},
        context_env={"CODEGUARD_REPO_ROOT": str(tmp_path)},
    )
    assert osv_res.ok, osv_res.error
    assert isinstance(osv_res.content, dict)
    assert osv_res.content["flagged_count"] >= 1
    assert osv_res.content["findings"][0]["package"] == "pyyaml"

    # 2. git_history: git_file_log against this workspace README.md
    repo_root = Path(__file__).resolve().parents[1]
    git_res = mgr.call_tool(
        servers["git_history"],
        "git_file_log",
        {"path": "README.md", "limit": 3},
        context_env={"CODEGUARD_REPO_ROOT": str(repo_root)},
    )
    assert git_res.ok, git_res.error
    assert isinstance(git_res.content, dict)

    # 3. knowledge: safe stdlib inspect + PR-repo import redline + incidents search
    know_ok = mgr.call_tool(
        servers["knowledge"],
        "inspect_package_api",
        {"module_name": "json", "symbol": "loads"},
        context_env={"CODEGUARD_REPO_ROOT": str(tmp_path)},
    )
    assert know_ok.ok, know_ok.error
    assert know_ok.content["ok"] is True

    (tmp_path / "evil_pr_mod.py").write_text("raise RuntimeError('pwn')\n", encoding="utf-8")
    know_blocked = mgr.call_tool(
        servers["knowledge"],
        "inspect_package_api",
        {"module_name": "evil_pr_mod"},
        context_env={"CODEGUARD_REPO_ROOT": str(tmp_path)},
    )
    assert know_blocked.ok
    assert know_blocked.content["ok"] is False
    assert "安全红线" in know_blocked.content["error"]

    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "incident-1.md").write_text("Auth token signature bypass fixed by HMAC check.", encoding="utf-8")
    search_res = mgr.call_tool(
        servers["knowledge"],
        "search_incidents",
        {"query": "HMAC"},
        context_env={"CODEGUARD_REPORTS_DIR": str(reports_dir)},
    )
    assert search_res.ok
    assert len(search_res.content["matches"]) == 1

    # 4. github_context: missing repo returns structured error instead of crashing
    gh_res = mgr.call_tool(servers["github_context"], "get_issue", {"issue_number": 1})
    assert gh_res.ok
    assert gh_res.content["ok"] is False


def test_console_api_mcp_status_endpoint(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SANDBOX_BACKEND", "fake")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("CHECKPOINT_PATH", str(tmp_path / "ck.db"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))

    from codeguard import server

    with TestClient(server.app) as client:
        cfg_resp = client.get("/api/config")
        assert cfg_resp.status_code == 200
        assert cfg_resp.json()["mcp_enabled"] is True

        resp = client.get("/api/mcp/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert len(body["servers"]) == 4
        assert all(srv["ok"] is True for srv in body["servers"])
