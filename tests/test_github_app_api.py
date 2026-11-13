"""GitHub App 接入接口：manifest、回调、刷新、断开与 webhook 分派（不触网）。"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from tests.fakes import FakeAppAuth


def _prepare(monkeypatch, tmp_path, public_url: str = "https://demo.example.com"):
    monkeypatch.setenv("SANDBOX_BACKEND", "fake")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("CODEGUARD_PUBLIC_URL", public_url)
    monkeypatch.setenv("CHECKPOINT_PATH", str(tmp_path / "ck.db"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("GITHUB_APP_STORE", str(tmp_path / "data" / "github-app.json"))


def _install_fake(server, **kwargs) -> FakeAppAuth:
    fake = FakeAppAuth(**kwargs)
    server.rt.app_auth = fake
    server.rt.refresh_github()
    return fake


def test_status_and_manifest_flow(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    from codeguard import server

    with TestClient(server.app) as client:
        _install_fake(server, configured=False)

        status = client.get("/api/github/app").json()["status"]
        assert status["configured"] is False
        assert status["auth"] == "none"

        created = client.post("/api/github/app/manifest", json={"name": "CodeGuard", "public_url": ""})
        assert created.status_code == 200
        body = created.json()
        assert body["action"].startswith("https://github.com/settings/apps/new?state=")
        assert body["manifest"]["hook_attributes"]["url"] == "https://demo.example.com/webhooks/github"

        assert client.get("/api/github/app/callback", params={"code": "x", "state": "bad"}).status_code == 403

        ok = client.get(
            "/api/github/app/callback",
            params={"code": "good", "state": body["state"], "format": "json"},
        )
        assert ok.status_code == 200
        assert ok.json()["status"]["configured"] is True

        # state 一次性
        replay = client.get(
            "/api/github/app/callback",
            params={"code": "good", "state": body["state"], "format": "json"},
        )
        assert replay.status_code == 403


def test_manifest_requires_public_url(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path, public_url="")
    from codeguard import server

    with TestClient(server.app) as client:
        _install_fake(server, configured=False)
        resp = client.post("/api/github/app/manifest", json={"name": "CodeGuard", "public_url": ""})
        assert resp.status_code == 400
        assert "公网地址" in resp.json()["detail"]


def test_callback_exchange_failure_is_502(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    from codeguard import server

    with TestClient(server.app) as client:
        _install_fake(server, configured=False)
        state = client.post("/api/github/app/manifest", json={"name": "x", "public_url": ""}).json()["state"]
        resp = client.get(
            "/api/github/app/callback",
            params={"code": "used", "state": state, "format": "json"},
        )
        assert resp.status_code == 502


def test_refresh_and_disconnect(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    from codeguard import server

    with TestClient(server.app) as client:
        fake = _install_fake(server, configured=True)

        refreshed = client.post("/api/github/app/refresh")
        assert refreshed.status_code == 200
        assert refreshed.json()["status"]["slug"] == "codeguard"
        assert fake.refreshed >= 1

        wrong = client.request("DELETE", "/api/github/app", json={"confirm": "nope"})
        assert wrong.status_code == 400

        gone = client.request("DELETE", "/api/github/app", json={"confirm": "codeguard"})
        assert gone.status_code == 200
        assert gone.json()["status"]["configured"] is False
        assert fake.cleared == 1


def test_console_repos_and_config_use_app_source(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    from codeguard import server

    with TestClient(server.app) as client:
        _install_fake(server, configured=True)

        config = client.get("/api/config").json()
        assert config["github_auth"] == "app"
        assert config["github_login"] == "codeguard"
        assert config["github_app"]["installations"] == 1

        repos = client.get("/api/repos").json()
        assert repos["source"] == "app"
        assert repos["app_slug"] == "codeguard"
        assert repos["repos"][0]["full_name"] == "acme/demo"

        filtered = client.get("/api/repos", params={"q": "nomatch"}).json()
        assert filtered["repos"] == []


def test_health_reports_app_auth(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    from codeguard import server

    with TestClient(server.app) as client:
        _install_fake(server, configured=True)
        health = client.get("/api/health").json()
        assert health["github_auth"] == "app"
        assert health["github_configured"] is True
        assert health["github_app"]["slug"] == "codeguard"


def test_installation_webhook_uses_app_secret(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    from codeguard import server

    with TestClient(server.app) as client:
        fake = _install_fake(server, configured=True)
        body = json.dumps({"action": "created", "installation": {"id": 11}}).encode("utf-8")
        signature = "sha256=" + hmac.new(b"s" * 32, body, hashlib.sha256).hexdigest()

        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "installation",
                "X-Hub-Signature-256": signature,
                "X-GitHub-Delivery": "delivery-1",
                "X-GitHub-Hook-Installation-Target-Type": "integration",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["event"] == "installation"

        duplicate = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "installation",
                "X-Hub-Signature-256": signature,
                "X-GitHub-Delivery": "delivery-1",
            },
        )
        assert duplicate.json().get("duplicate") is True

        bad = client.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "installation", "X-Hub-Signature-256": "sha256=bad"},
        )
        assert bad.status_code == 401
        assert fake.configured is True


def test_guard_rejects_non_local_without_admin_token(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    from fastapi import HTTPException
    from codeguard import server

    with TestClient(server.app):
        scope = {
            "type": "http", "method": "POST", "path": "/api/github/app/manifest",
            "headers": [], "query_string": b"", "client": ("10.0.0.9", 1234),
        }
        with pytest.raises(HTTPException) as exc:
            server.app_api._guard(Request(scope))
        assert exc.value.status_code == 403

        local = dict(scope)
        local["client"] = ("127.0.0.1", 1234)
        server.app_api._guard(Request(local))
