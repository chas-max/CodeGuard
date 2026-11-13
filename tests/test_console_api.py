"""控制台接口：仓库切换 / PR 列表 / 任务列表 / 定时轮询配置。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient


def _prepare(monkeypatch, tmp_path):
    monkeypatch.setenv("SANDBOX_BACKEND", "fake")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("CHECKPOINT_PATH", str(tmp_path / "ck.db"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("CODEGUARD_REPOS", "acme/one,acme/two")


def _fake_github():
    from tests.fakes import FakeGitHub

    fake = FakeGitHub()
    fake.repos = [{
        "full_name": "acme/demo", "private": False, "default_branch": "main",
        "description": "demo repo", "html_url": "https://github.com/acme/demo",
        "language": "Python", "pushed_at": "2026-01-01T00:00:00Z", "open_issues_count": 2,
    }]
    fake.pulls = [{
        "number": 12, "title": "fix: divide by zero", "user": {"login": "dev"},
        "state": "open", "draft": False, "head": {"ref": "fix/zero", "sha": "c" * 40},
        "base": {"ref": "main"}, "html_url": "https://github.com/acme/demo/pull/12",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
    }]
    return fake


def test_console_config_repos_and_pulls(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    from codeguard import server

    with TestClient(server.app) as client:
        server.rt.nodes.github = _fake_github()

        config = client.get("/api/config").json()
        assert config["default_repos"] == ["acme/one", "acme/two"]
        assert config["github_login"] == "tester"
        assert config["github_configured"] is True

        repos = client.get("/api/repos").json()
        assert repos["source"] == "github"
        assert repos["repos"][0]["full_name"] == "acme/demo"

        filtered = client.get("/api/repos", params={"q": "nomatch"}).json()
        assert filtered["repos"] == []

        pulls = client.get("/api/pulls", params={"repo": "acme/demo"}).json()
        assert pulls["pulls"][0]["number"] == 12
        assert pulls["pulls"][0]["job"] is None

        assert client.get("/api/pulls", params={"repo": "acme/demo", "state": "bad"}).status_code == 400


def test_watch_lifecycle(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    from codeguard import server

    with TestClient(server.app) as client:
        server.rt.nodes.github = _fake_github()

        created = client.post("/api/watch", json={"repo": "acme/demo", "interval_sec": 60})
        assert created.status_code == 200
        watcher = created.json()
        assert watcher["polls"] == 1
        assert watcher["detected"][0]["changed"] is True

        # head 未变化时不应重复标记
        again = client.post(f"/api/watch/{watcher['id']}/poll").json()
        assert again["detected"][0]["changed"] is False
        assert again["polls"] == 2

        # 同一仓库不允许重复创建
        assert client.post("/api/watch", json={"repo": "acme/demo"}).status_code == 409
        assert client.post("/api/watch", json={"repo": "bad-name"}).status_code == 400

        patched = client.patch(f"/api/watch/{watcher['id']}", json={"auto_review": True}).json()
        assert patched["auto_review"] is True

        assert len(client.get("/api/watch").json()["watchers"]) == 1
        assert client.delete(f"/api/watch/{watcher['id']}").json()["ok"] is True
        assert client.get("/api/watch").json()["watchers"] == []
        assert client.delete(f"/api/watch/{watcher['id']}").status_code == 404


def test_jobs_index_merges_reports(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    from codeguard import server

    reports = tmp_path / "reports" / "jobs"
    reports.mkdir(parents=True)
    (reports / "job-old.json").write_text(json.dumps({
        "job_id": "job-old", "repo": "acme/demo", "pr_number": 3, "author": "dev",
        "tier": "T1", "outcome": "MERGED", "message": "已合并。",
        "started_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:10:00+00:00",
        "duration_sec": 600.0, "cost_cny": 0.42, "findings_total": 3, "findings_blocking": 1,
        "approvals": 1, "required_approvals": 1,
    }, ensure_ascii=False), encoding="utf-8")

    with TestClient(server.app) as client:
        body = client.get("/api/jobs").json()
        assert body["total"] == 1
        job = body["jobs"][0]
        assert job["job_id"] == "job-old"
        assert job["status"] == "done"
        assert job["live"] is False

        scoped = client.get("/api/jobs", params={"repo": "acme/demo"}).json()
        assert scoped["total"] == 1
        assert client.get("/api/jobs", params={"repo": "other/repo"}).json()["total"] == 0

        # 历史任务没有 checkpoint，追踪接口应明确 404
        assert client.get("/api/jobs/job-old/trace").status_code == 404
        assert client.get("/api/jobs/unknown-job").status_code == 404
