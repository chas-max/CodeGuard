"""GitHub App 接入路由：manifest 注册、回调换凭据、状态查询与断开。

安全边界：
- 写接口默认只允许本机调用；非本机需要 `X-CodeGuard-Admin` 且与服务端
  `CODEGUARD_ADMIN_TOKEN` 一致；
- manifest 使用一次性 state（10 分钟 TTL）防重放；
- 响应只包含掩码状态，绝不回传 private key / webhook secret。
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Any, Callable
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..errors import GitHubAuthError

logger = logging.getLogger("codeguard.github.app_api")

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
STATE_TTL_SEC = 600


class ManifestRequest(BaseModel):
    name: str = Field(default="CodeGuard", max_length=60)
    public_url: str = ""


class DisconnectRequest(BaseModel):
    confirm: str = ""


class AppAPI:
    def __init__(
        self,
        *,
        get_runtime: Callable[[], Any],
        refresh_github: Callable[[], None] | None = None,
        state_ttl: int = STATE_TTL_SEC,
    ):
        self._get_runtime = get_runtime
        self._refresh = refresh_github or (lambda: None)
        self._state_ttl = state_ttl
        self._states: dict[str, float] = {}
        self.router = APIRouter()
        self._register_routes()

    # ---- 安全 ----

    def _guard(self, request: Request) -> None:
        host = (request.client.host if request.client else "") or ""
        if host in LOCAL_HOSTS:
            return
        expected = getattr(self._get_runtime().cfg, "console_admin_token", "") or ""
        supplied = request.headers.get("X-CodeGuard-Admin", "")
        if expected and supplied and secrets.compare_digest(supplied, expected):
            return
        raise HTTPException(status_code=403, detail="App 管理接口仅允许本机访问")

    def _issue_state(self) -> str:
        now = time.time()
        self._states = {key: exp for key, exp in self._states.items() if exp > now}
        state = secrets.token_urlsafe(24)
        self._states[state] = now + self._state_ttl
        return state

    def _consume_state(self, state: str) -> None:
        expires = self._states.pop(state, 0.0)
        if not expires or expires < time.time():
            raise HTTPException(status_code=403, detail="state 无效或已过期，请重新发起注册")

    # ---- 路由 ----

    def _register_routes(self) -> None:
        router = self.router

        @router.get("/api/github/app")
        async def app_status():
            return {"status": self._get_runtime().github_status()}

        @router.post("/api/github/app/manifest")
        async def create_manifest(req: ManifestRequest, request: Request):
            self._guard(request)
            runtime = self._get_runtime()
            public_url = (req.public_url or getattr(runtime.cfg, "public_url", "") or "").strip().rstrip("/")
            if not public_url:
                raise HTTPException(
                    status_code=400,
                    detail="缺少公网地址：先启动内网穿透（如 cloudflared tunnel --url http://127.0.0.1:8002），"
                           "把 HTTPS 地址填到 CODEGUARD_PUBLIC_URL 或本请求的 public_url",
                )
            try:
                manifest = runtime.app_auth.manifest(public_url, name=req.name or "CodeGuard")
            except GitHubAuthError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            state = self._issue_state()
            action = "https://github.com/settings/apps/new?" + urlencode({"state": state})
            return {
                "action": action,
                "state": state,
                "manifest": manifest,
                "public_url": public_url,
                "hint": "把 manifest 作为字符串放进同名表单字段，用 POST 提交到 action",
            }

        @router.get("/api/github/app/callback")
        async def manifest_callback(code: str = "", state: str = "", format: str = ""):
            self._consume_state(state)
            if not code:
                raise HTTPException(status_code=400, detail="缺少 code")
            runtime = self._get_runtime()
            try:
                creds = runtime.app_auth.exchange_manifest(code)
            except GitHubAuthError as exc:
                logger.warning("manifest 交换失败: %s", exc)
                raise HTTPException(status_code=502, detail=f"manifest 交换失败: {exc}") from exc
            self._refresh()
            logger.info("GitHub App 已接入: %s (id=%s)", creds.slug, creds.app_id)
            if format == "json":
                return {"ok": True, "status": runtime.github_status()}
            return RedirectResponse(url="/ui/#/settings?github=connected", status_code=303)

        @router.post("/api/github/app/refresh")
        async def refresh_installations(request: Request):
            self._guard(request)
            runtime = self._get_runtime()
            auth = runtime.app_auth
            if not auth.configured:
                raise HTTPException(status_code=400, detail="GitHub App 尚未配置")
            try:
                auth.invalidate()
                auth.installations(refresh=True)
                auth.repos(refresh=True)
            except GitHubAuthError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            self._refresh()
            return {"status": runtime.github_status()}

        @router.delete("/api/github/app")
        async def disconnect(req: DisconnectRequest, request: Request):
            self._guard(request)
            runtime = self._get_runtime()
            slug = (runtime.app_auth.status().get("slug") or "")
            if not slug:
                raise HTTPException(status_code=400, detail="GitHub App 尚未配置")
            if req.confirm.strip() != slug:
                raise HTTPException(status_code=400, detail=f"需要输入 App 名称 {slug} 确认断开")
            runtime.app_auth.clear()
            self._refresh()
            return {"ok": True, "status": runtime.github_status()}


__all__ = ["AppAPI", "ManifestRequest", "DisconnectRequest", "LOCAL_HOSTS"]
