"""GitHub App 鉴权：manifest 注册、RS256 JWT、installation token 缓存与凭据存储。

边界：
- 本模块只负责凭据与 token 生命周期，不注册任何 HTTP 路由（见 app_api.py）；
- 私钥与 webhook secret 只落盘到 0600 文件，对外只用 `status()` 暴露掩码状态；
- installation token 提前 60 秒过期，鉴权失败时由 `GitHubClient` 调用 `invalidate()` 重取。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from ..errors import GitHubAuthError

logger = logging.getLogger("codeguard.github.app")

DEFAULT_API = "https://api.github.com"
JWT_TTL_SEC = 540
TOKEN_SKEW_SEC = 60
CACHE_TTL_SEC = 60

APP_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "checks": "write",
    "statuses": "write",
    "issues": "write",
    "metadata": "read",
}
# 只声明可订阅事件。installation / installation_repositories 由 GitHub 自动投递，
# 写进 manifest 会报 "Default events unsupported" / "not supported by permissions"。
APP_EVENTS = ["pull_request"]


@dataclass(frozen=True)
class AppCredentials:
    app_id: int
    slug: str
    private_key: str
    webhook_secret: str = ""
    client_id: str = ""
    client_secret: str = ""
    html_url: str = ""
    name: str = "CodeGuard"
    owner: str = ""

    def to_json(self) -> dict:
        return asdict(self)


class CredentialStore:
    """`data/github-app.json` 的原子读写，权限 0600。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> Optional[AppCredentials]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise GitHubAuthError(f"读取 GitHub App 凭据失败: {exc}") from exc
        try:
            return AppCredentials(
                app_id=int(raw["app_id"]),
                slug=str(raw.get("slug") or ""),
                private_key=str(raw["private_key"]),
                webhook_secret=str(raw.get("webhook_secret") or ""),
                client_id=str(raw.get("client_id") or ""),
                client_secret=str(raw.get("client_secret") or ""),
                html_url=str(raw.get("html_url") or ""),
                name=str(raw.get("name") or "CodeGuard"),
                owner=str(raw.get("owner") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubAuthError(f"GitHub App 凭据格式非法: {exc}") from exc

    def save(self, creds: AppCredentials) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(creds.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise GitHubAuthError(f"写入 GitHub App 凭据失败: {exc}") from exc
        finally:
            if tmp.exists() and tmp != self.path:
                tmp.unlink(missing_ok=True)

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise GitHubAuthError(f"删除 GitHub App 凭据失败: {exc}") from exc


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class GitHubAppAuth:
    """App 凭据持有者与 token provider（线程安全）。"""

    def __init__(self, store: CredentialStore, *, api_url: str = DEFAULT_API, http=None):
        self.store = store
        self.api_url = (api_url or DEFAULT_API).rstrip("/")
        self._http = http
        self._creds: Optional[AppCredentials] = None
        self._tokens: dict[int, tuple[str, float]] = {}
        self._installations: list[dict] = []
        self._installations_at = 0.0
        self._repos: list[dict] = []
        self._repo_map: dict[str, int] = {}
        self._repos_at = 0.0
        self._lock = threading.RLock()
        self.last_error = ""

    # ---- 凭据 ----

    @property
    def configured(self) -> bool:
        return bool(self._creds and self._creds.private_key and self._creds.app_id)

    @property
    def credentials(self) -> Optional[AppCredentials]:
        return self._creds

    def load(self) -> Optional[AppCredentials]:
        self._creds = self.store.load()
        self._reset_cache()
        return self._creds

    def save(self, creds: AppCredentials) -> None:
        self.store.save(creds)
        self._creds = creds
        self.last_error = ""
        self._reset_cache()

    def clear(self) -> None:
        self.store.clear()
        self._creds = None
        self.last_error = ""
        self._reset_cache()

    def _reset_cache(self) -> None:
        with self._lock:
            self._tokens.clear()
            self._installations = []
            self._installations_at = 0.0
            self._repos = []
            self._repo_map = {}
            self._repos_at = 0.0

    def webhook_secret(self) -> str:
        return self._creds.webhook_secret if self._creds else ""

    # ---- HTTP ----

    def _send(self, method: str, path: str, *, headers: dict | None = None, **kwargs) -> Any:
        client = self._http
        owned = client is None
        if client is None:
            client = httpx.Client(timeout=30.0)
        try:
            resp = client.request(method, self.api_url + path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubAuthError(f"GitHub App 请求失败: {method} {path}: {exc}") from exc
        finally:
            if owned:
                client.close()
        status = int(getattr(resp, "status_code", 0) or 0)
        if status >= 400:
            detail = str(getattr(resp, "text", ""))[:300]
            raise GitHubAuthError(f"GitHub App {status}: {method} {path}: {detail}")
        if status == 204 or not getattr(resp, "content", b""):
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise GitHubAuthError(f"GitHub App 响应非 JSON: {method} {path}") from exc

    def app_jwt(self) -> str:
        if not self.configured:
            raise GitHubAuthError("GitHub App 未配置，无法签名 JWT")
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的明确提示
            raise GitHubAuthError("缺少 PyJWT[crypto]，无法签名 GitHub App JWT") from exc
        now = int(time.time())
        try:
            return jwt.encode(
                {"iat": now - 60, "exp": now + JWT_TTL_SEC, "iss": str(self._creds.app_id)},
                self._creds.private_key,
                algorithm="RS256",
            )
        except Exception as exc:  # noqa: BLE001 - 私钥非法要 fail closed
            raise GitHubAuthError(f"GitHub App 私钥签名失败: {exc}") from exc

    def _jwt_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ---- manifest ----

    def manifest(self, public_url: str, *, name: str = "CodeGuard") -> dict:
        base = (public_url or "").rstrip("/")
        if not base.startswith("https://"):
            raise GitHubAuthError("CODEGUARD_PUBLIC_URL 必须是以 https:// 开头的公网地址")
        return {
            "name": name,
            "url": base,
            "description": "CodeGuard PR 审查：混合检测、修复、确定性合并门控",
            "hook_attributes": {"url": f"{base}/webhooks/github", "active": True},
            "redirect_url": f"{base}/api/github/app/callback",
            "public": False,
            "default_permissions": dict(APP_PERMISSIONS),
            "default_events": list(APP_EVENTS),
        }

    def exchange_manifest(self, code: str) -> AppCredentials:
        data = self._send("POST", f"/app-manifests/{code}/conversions")
        owner = (data.get("owner") or {}).get("login") or ""
        creds = AppCredentials(
            app_id=int(data.get("id") or 0),
            slug=str(data.get("slug") or ""),
            private_key=str(data.get("pem") or ""),
            webhook_secret=str(data.get("webhook_secret") or ""),
            client_id=str(data.get("client_id") or ""),
            client_secret=str(data.get("client_secret") or ""),
            html_url=str(data.get("html_url") or ""),
            name=str(data.get("name") or "CodeGuard"),
            owner=str(owner),
        )
        if not creds.app_id or not creds.private_key:
            raise GitHubAuthError("manifest 交换结果缺少 app_id 或私钥")
        self.save(creds)
        return creds

    def install_url(self) -> str:
        if not self._creds:
            return ""
        if self._creds.html_url:
            return f"{self._creds.html_url.rstrip('/')}/installations/new"
        if self._creds.slug:
            return f"https://github.com/apps/{self._creds.slug}/installations/new"
        return ""

    # ---- installation / token ----

    def _parse_ts(self, raw: str, fallback: float) -> float:
        try:
            return _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return fallback

    def _installation_token(self, installation_id: int) -> str:
        with self._lock:
            cached = self._tokens.get(installation_id)
            if cached and cached[1] - TOKEN_SKEW_SEC > time.time():
                return cached[0]
        data = self._send("POST", f"/app/installations/{installation_id}/access_tokens", headers=self._jwt_headers())
        token = str(data.get("token") or "")
        if not token:
            raise GitHubAuthError(f"installation {installation_id} 未返回 token")
        expires = self._parse_ts(data.get("expires_at") or "", time.time() + 3300)
        with self._lock:
            self._tokens[installation_id] = (token, expires)
        return token

    def installations(self, *, refresh: bool = False) -> list[dict]:
        with self._lock:
            if self._installations and not refresh and time.time() - self._installations_at < CACHE_TTL_SEC:
                return list(self._installations)
        if not self.configured:
            return []
        raw = self._send("GET", "/app/installations", headers=self._jwt_headers(), params={"per_page": 100})
        items = [item for item in (raw or []) if item.get("id")]
        with self._lock:
            self._installations = items
            self._installations_at = time.time()
        return list(items)

    def repos(self, *, refresh: bool = False) -> list[dict]:
        """跨 installation 聚合仓库，带 `installation_id` 标注。"""
        with self._lock:
            if self._repos and not refresh and time.time() - self._repos_at < CACHE_TTL_SEC:
                return list(self._repos)
        collected: list[dict] = []
        mapping: dict[str, int] = {}
        for inst in self.installations(refresh=refresh):
            installation_id = int(inst.get("id") or 0)
            try:
                token = self._installation_token(installation_id)
            except GitHubAuthError as exc:
                self.last_error = str(exc)
                continue
            for page in range(1, 6):
                data = self._send(
                    "GET", "/installation/repositories",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                    params={"per_page": 100, "page": page},
                )
                batch = (data or {}).get("repositories") or []
                for item in batch:
                    full_name = str(item.get("full_name") or "")
                    if not full_name or full_name in mapping:
                        continue
                    mapping[full_name] = installation_id
                    collected.append({
                        "full_name": full_name,
                        "private": bool(item.get("private")),
                        "default_branch": str(item.get("default_branch") or "main"),
                        "description": str(item.get("description") or ""),
                        "html_url": str(item.get("html_url") or ""),
                        "language": str(item.get("language") or ""),
                        "pushed_at": str(item.get("pushed_at") or ""),
                        "open_issues": item.get("open_issues_count"),
                        "installation_id": installation_id,
                    })
                if len(batch) < 100:
                    break
        with self._lock:
            self._repos = collected
            self._repo_map = mapping
            self._repos_at = time.time()
        return list(collected)

    def installation_for_repo(self, repo: str) -> Optional[int]:
        if not repo:
            return None
        with self._lock:
            found = self._repo_map.get(repo)
        if found:
            return found
        self.repos(refresh=False)
        with self._lock:
            found = self._repo_map.get(repo)
        return found

    def token_for_repo(self, repo: str | None = None) -> Optional[str]:
        """`GitHubClient.token_provider`：按仓库返回 installation token。"""
        if not self.configured:
            return None
        if repo:
            installation_id = self.installation_for_repo(repo)
        else:
            items = self.installations()
            installation_id = int(items[0].get("id")) if items else None
        if not installation_id:
            raise GitHubAuthError(f"仓库 {repo or '-'} 未安装 CodeGuard App")
        return self._installation_token(int(installation_id))

    def invalidate(self, repo: str | None = None) -> None:
        with self._lock:
            self._tokens.clear()
            if repo:
                self._repo_map.pop(repo, None)
            else:
                self._repo_map = {}
                self._repos = []
                self._repos_at = 0.0
            self._installations_at = 0.0

    # ---- 展示 ----

    def app_info(self) -> dict:
        if not self.configured:
            return {}
        try:
            return self._send("GET", "/app", headers=self._jwt_headers()) or {}
        except GitHubAuthError as exc:
            self.last_error = str(exc)
            return {}

    def status(self) -> dict:
        creds = self._creds
        with self._lock:
            installations = len(self._installations)
            repos = len(self._repos)
        return {
            "configured": self.configured,
            "app_id": creds.app_id if creds else 0,
            "slug": creds.slug if creds else "",
            "name": creds.name if creds else "",
            "owner": creds.owner if creds else "",
            "html_url": creds.html_url if creds else "",
            "install_url": self.install_url(),
            "installations": installations,
            "repos": repos,
            "webhook_configured": bool(creds and creds.webhook_secret),
            "private_key_configured": bool(creds and creds.private_key),
            "last_error": self.last_error,
        }


__all__ = [
    "AppCredentials", "CredentialStore", "GitHubAppAuth",
    "APP_PERMISSIONS", "APP_EVENTS", "DEFAULT_API",
]
