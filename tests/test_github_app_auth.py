"""GitHub App 鉴权：manifest、JWT、installation token 缓存与凭据存储（不触网）。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from codeguard.errors import GitHubAuthError
from codeguard.github.app_auth import AppCredentials, CredentialStore, GitHubAppAuth
from codeguard.github.client import GitHubClient


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


PEM = _pem()


class _Resp:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.content = b"{}" if payload is not None else b""
        self.text = json.dumps(payload or {}, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeHTTP:
    """最小 GitHub App API 替身：按路径返回，并记录 Authorization。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.token_seq = 0
        self.repo_calls = 0

    def request(self, method: str, url: str, *, headers=None, **kwargs):
        path = urlsplit(url).path
        headers = headers or {}
        self.calls.append({"method": method, "path": path, "headers": headers, "kwargs": kwargs})
        if path == "/app":
            return _Resp(200, {"slug": "codeguard", "name": "CodeGuard"})
        if path == "/app/installations":
            return _Resp(200, [{"id": 11, "account": {"login": "acme"}}])
        if re.fullmatch(r"/app/installations/\d+/access_tokens", path):
            self.token_seq += 1
            return _Resp(201, {
                "token": f"ghs_11_{self.token_seq}",
                "expires_at": "2099-01-01T00:00:00Z",
            })
        if path == "/installation/repositories":
            self.repo_calls += 1
            assert headers.get("Authorization", "").startswith("Bearer ghs_11_")
            return _Resp(200, {"repositories": [{
                "full_name": "acme/demo", "private": False, "default_branch": "main",
                "description": "demo", "html_url": "https://github.com/acme/demo",
                "language": "Python", "pushed_at": "", "open_issues_count": 1,
            }]})
        if path.startswith("/app-manifests/"):
            return _Resp(201, {
                "id": 123, "slug": "codeguard", "pem": PEM, "webhook_secret": "w" * 40,
                "client_id": "cid", "client_secret": "csecret",
                "html_url": "https://github.com/apps/codeguard",
                "name": "CodeGuard", "owner": {"login": "acme"},
            })
        return _Resp(404, {"message": "not found"})


def _auth(tmp_path: Path, http=None) -> GitHubAppAuth:
    store = CredentialStore(tmp_path / "data" / "github-app.json")
    return GitHubAppAuth(store, http=http or FakeHTTP())


def _configured(tmp_path: Path, http=None) -> GitHubAppAuth:
    auth = _auth(tmp_path, http)
    auth.save(AppCredentials(
        app_id=123, slug="codeguard", private_key=PEM, webhook_secret="w" * 40,
        html_url="https://github.com/apps/codeguard", name="CodeGuard", owner="acme",
    ))
    return auth


def test_manifest_requires_https_and_sets_webhook(tmp_path):
    auth = _auth(tmp_path)
    with pytest.raises(GitHubAuthError):
        auth.manifest("http://127.0.0.1:8002")

    manifest = auth.manifest("https://demo.example.com/", name="CodeGuard")
    assert manifest["hook_attributes"]["url"] == "https://demo.example.com/webhooks/github"
    assert manifest["redirect_url"] == "https://demo.example.com/api/github/app/callback"
    assert manifest["default_permissions"]["contents"] == "write"
    assert manifest["default_events"] == ["pull_request"]
    # installation 类事件由 GitHub 自动投递，写进 manifest 会被 GitHub 拒绝
    assert "installation" not in manifest["default_events"]


def test_exchange_manifest_persists_credentials(tmp_path):
    http = FakeHTTP()
    auth = _auth(tmp_path, http)
    creds = auth.exchange_manifest("good-code")

    path = tmp_path / "data" / "github-app.json"
    assert path.is_file()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["app_id"] == 123
    assert saved["private_key"] == PEM
    assert creds.slug == "codeguard"
    assert auth.configured is True
    # 落盘后新实例可直接加载
    again = GitHubAppAuth(CredentialStore(path))
    assert again.load().app_id == 123


def test_jwt_claims_and_private_key_is_never_exposed(tmp_path):
    import jwt

    auth = _configured(tmp_path)
    claims = jwt.decode(auth.app_jwt(), options={"verify_signature": False})
    assert claims["iss"] == "123"
    assert 0 < claims["exp"] - claims["iat"] <= 660

    status = auth.status()
    assert status["configured"] is True
    assert status["private_key_configured"] is True
    assert "private_key" not in status
    assert "webhook_secret" not in status


def test_installation_token_cached_then_invalidated(tmp_path):
    http = FakeHTTP()
    auth = _configured(tmp_path, http)

    first = auth.repos()
    assert first[0]["full_name"] == "acme/demo"
    assert first[0]["installation_id"] == 11
    assert http.token_seq == 1

    auth.repos()  # 命中 60s 缓存
    assert http.repo_calls == 1

    auth.invalidate()
    auth.installations(refresh=True)
    auth.repos(refresh=True)
    assert http.token_seq >= 1


def test_token_for_repo_and_unknown_repo(tmp_path):
    auth = _configured(tmp_path, FakeHTTP())
    token = auth.token_for_repo("acme/demo")
    assert token.startswith("ghs_11_")

    with pytest.raises(GitHubAuthError):
        auth.token_for_repo("acme/missing")


def test_status_and_install_url(tmp_path):
    auth = _configured(tmp_path, FakeHTTP())
    auth.installations(refresh=True)
    auth.repos(refresh=True)
    status = auth.status()
    assert status["installations"] == 1
    assert status["repos"] == 1
    assert status["webhook_configured"] is True
    assert status["install_url"].endswith("/installations/new")


def test_clear_removes_credentials(tmp_path):
    auth = _configured(tmp_path, FakeHTTP())
    auth.clear()
    assert auth.configured is False
    assert not (tmp_path / "data" / "github-app.json").exists()


class _FlakyResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self.content = b"{}"
        self.text = "unauthorized"
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FlakyHTTP:
    def __init__(self):
        self.calls = 0

    def request(self, method, url, headers=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return _FlakyResponse(401)
        return _FlakyResponse(200, {"full_name": "acme/demo"})


def test_client_401_invalidates_and_retries_once():
    invalidated: list[str | None] = []
    client = GitHubClient(
        None,
        token_provider=lambda repo: "ghs_token",
        token_invalidator=invalidated.append,
    )
    client._client = _FlakyHTTP()
    assert client.get_repo("acme/demo") == {"full_name": "acme/demo"}
    assert invalidated == ["acme/demo"]
    assert client._client.calls == 2


def test_client_merge_retries_transient_405(monkeypatch):
    class _MergeHTTP:
        def __init__(self):
            self.calls: list[tuple[str, str]] = []
            self.merge_attempts = 0

        def request(self, method, url, headers=None, **kwargs):
            self.calls.append((method, url))
            if url.endswith("/merge"):
                self.merge_attempts += 1
                if self.merge_attempts < 2:
                    resp = _FlakyResponse(405)
                    resp.text = '{"message":"Base branch was modified. Review and try the merge again."}'
                    return resp
                return _FlakyResponse(200, {"sha": "merged_sha_123"})
            return _FlakyResponse(200, {"number": 10, "mergeable": True})

    monkeypatch.setattr("codeguard.github.client.time.sleep", lambda *_: None)
    client = GitHubClient("tok")
    client._client = _MergeHTTP()
    res = client.merge("acme/demo", 10, sha="a" * 40, method="squash", title="t", message="m")
    assert res["sha"] == "merged_sha_123"
    assert client._client.merge_attempts == 2
