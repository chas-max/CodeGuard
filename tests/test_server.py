from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient


def test_health_and_webhook_signature(monkeypatch, tmp_path):
    secret = "s" * 32
    monkeypatch.setenv("SANDBOX_BACKEND", "fake")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("CHECKPOINT_PATH", str(tmp_path / "ck.db"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    from codeguard import server

    with TestClient(server.app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["sandbox_backend"] == "fake"

        body = json.dumps({"action": "opened"}).encode()
        bad = client.post("/webhooks/github", content=body,
                          headers={"X-GitHub-Event": "pull_request",
                                   "X-Hub-Signature-256": "sha256=bad"})
        assert bad.status_code == 401

        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        ignored = client.post("/webhooks/github", content=body,
                              headers={"X-GitHub-Event": "issues",
                                       "X-Hub-Signature-256": sig})
        assert ignored.status_code == 200
        assert ignored.json()["ignored"]
