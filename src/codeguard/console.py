"""控制台 API：仓库切换、PR 列表、任务列表、全链路追踪与定时轮询。

本模块只做**展示与调度**，不改变审查主流程：
- 定时轮询只负责「发现新 PR/新 head」并按需触发既有 `start_job`；
- 追踪数据来自 LangGraph checkpoint 历史，纯只读；
- 任何异常都收敛为该 watcher 的错误信息，不影响 webhook / 主流程。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .errors import GitHubError
from .trace import build_trace, current_node

logger = logging.getLogger("codeguard.console")

PR_STATES = ("open", "closed", "all")
MIN_INTERVAL = 30
MAX_INTERVAL = 86400
MAX_AUTO_JOBS_PER_POLL = 3


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _plus(seconds: int) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=seconds)).isoformat()


# ────────────────────────────── 定时轮询 ──────────────────────────────


@dataclass
class Watcher:
    id: str
    repo: str
    interval_sec: int = 300
    state: str = "open"
    auto_review: bool = False
    dry_run: bool = False
    profile: str = ""
    created_at: str = field(default_factory=_now)
    last_poll_at: str = ""
    next_poll_at: str = ""
    last_error: str = ""
    polls: int = 0
    seen: dict[str, str] = field(default_factory=dict)
    detected: list[dict] = field(default_factory=list)
    task: Any = None

    def public(self) -> dict:
        return {
            "id": self.id,
            "repo": self.repo,
            "interval_sec": self.interval_sec,
            "state": self.state,
            "auto_review": self.auto_review,
            "dry_run": self.dry_run,
            "profile": self.profile,
            "created_at": self.created_at,
            "last_poll_at": self.last_poll_at,
            "next_poll_at": self.next_poll_at,
            "last_error": self.last_error,
            "polls": self.polls,
            "tracked": len(self.seen),
            "detected": self.detected,
            "running": bool(self.task and not self.task.done()),
        }

    def to_json(self) -> dict:
        data = self.public()
        data.pop("task", None)
        data["seen"] = self.seen
        return data


class WatchRequest(BaseModel):
    repo: str = Field(min_length=3)
    interval_sec: int = Field(default=300, ge=MIN_INTERVAL, le=MAX_INTERVAL)
    state: str = "open"
    auto_review: bool = False
    dry_run: bool = False
    profile: str = ""


class WatchPatch(BaseModel):
    interval_sec: Optional[int] = Field(default=None, ge=MIN_INTERVAL, le=MAX_INTERVAL)
    state: Optional[str] = None
    auto_review: Optional[bool] = None
    dry_run: Optional[bool] = None


# ────────────────────────────── API ──────────────────────────────


class ConsoleAPI:
    """把控制台能力挂到既有 server 上，依赖通过回调注入，便于测试替身。"""

    def __init__(
        self,
        *,
        get_runtime: Callable[[], Any],
        start_job: Callable[..., str],
        trigger_from_github: Callable[..., str],
        jobs_index: Callable[[], list[dict]],
    ):
        self._get_runtime = get_runtime
        self._start_job = start_job
        self._trigger_from_github = trigger_from_github
        self._jobs_index = jobs_index
        self._watchers: dict[str, Watcher] = {}
        self.router = APIRouter()
        self._register_routes()

    # ---- 存储 ----

    def _watch_path(self) -> Path:
        runtime = self._get_runtime()
        return Path(runtime.cfg.data_dir) / "watchers.json"

    def _save(self) -> None:
        try:
            path = self._watch_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = [w.to_json() for w in self._watchers.values()]
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:  # 持久化失败不影响内存态
            logger.warning("watchers 持久化失败: %s", exc)

    async def startup(self) -> None:
        """恢复上次的 watcher 并重新启动轮询循环。"""
        try:
            path = self._watch_path()
            if not path.is_file():
                return
            for item in json.loads(path.read_text(encoding="utf-8")):
                watcher = Watcher(
                    id=str(item.get("id") or uuid.uuid4().hex[:8]),
                    repo=str(item.get("repo") or ""),
                    interval_sec=int(item.get("interval_sec") or 300),
                    state=str(item.get("state") or "open"),
                    auto_review=bool(item.get("auto_review")),
                    dry_run=bool(item.get("dry_run")),
                    profile=str(item.get("profile") or ""),
                    created_at=str(item.get("created_at") or _now()),
                    polls=int(item.get("polls") or 0),
                    last_poll_at=str(item.get("last_poll_at") or ""),
                    detected=list(item.get("detected") or [])[:50],
                    seen={str(k): str(v) for k, v in (item.get("seen") or {}).items()},
                )
                if watcher.repo:
                    self._watchers[watcher.id] = watcher
                    self._spawn(watcher)
                    # 服务重启后立刻同步一次，避免界面展示过期数据。
                    asyncio.create_task(self.poll_once(watcher))
        except (OSError, ValueError) as exc:
            logger.warning("watchers 恢复失败: %s", exc)

    async def shutdown(self) -> None:
        for watcher in self._watchers.values():
            if watcher.task and not watcher.task.done():
                watcher.task.cancel()

    def _spawn(self, watcher: Watcher) -> None:
        watcher.task = asyncio.create_task(self._loop(watcher))
        watcher.next_poll_at = _plus(watcher.interval_sec)

    async def _loop(self, watcher: Watcher) -> None:
        try:
            while True:
                watcher.next_poll_at = _plus(watcher.interval_sec)
                await asyncio.sleep(watcher.interval_sec)
                await self.poll_once(watcher)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 轮询异常不能拖垮服务
            logger.error("watcher %s 崩溃: %s", watcher.id, exc, exc_info=exc)
            watcher.last_error = str(exc)

    # ---- 轮询 ----

    def _fetch_pulls(self, repo: str, state: str) -> list[dict]:
        runtime = self._get_runtime()
        client = runtime.nodes.github
        if client is None:
            raise GitHubError("未配置 GitHub 凭据，无法拉取 PR")
        return client.list_pulls(repo, state=state, per_page=50) or []

    async def poll_once(self, watcher: Watcher) -> dict:
        try:
            pulls = await asyncio.to_thread(self._fetch_pulls, watcher.repo, watcher.state)
        except Exception as exc:  # noqa: BLE001
            watcher.last_error = str(exc)
            watcher.polls += 1
            watcher.last_poll_at = _now()
            self._save()
            return watcher.public()

        jobs = {(item.get("repo"), item.get("pr_number")): item for item in self._jobs_index()}
        detected: list[dict] = []
        triggered = 0
        for pr in pulls:
            number = int(pr.get("number") or 0)
            if number <= 0:
                continue
            head = pr.get("head") or {}
            sha = str(head.get("sha") or "")
            key = str(number)
            changed = watcher.seen.get(key) != sha
            watcher.seen[key] = sha
            job = jobs.get((watcher.repo, number))
            item = {
                "number": number,
                "title": str(pr.get("title") or "")[:200],
                "author": str((pr.get("user") or {}).get("login") or ""),
                "state": str(pr.get("state") or ""),
                "draft": bool(pr.get("draft")),
                "head_sha": sha,
                "base_ref": str((pr.get("base") or {}).get("ref") or ""),
                "html_url": str(pr.get("html_url") or ""),
                "updated_at": str(pr.get("updated_at") or ""),
                "created_at": str(pr.get("created_at") or ""),
                "changed": changed,
                "job": job,
            }
            detected.append(item)
            if (
                changed and watcher.auto_review and triggered < MAX_AUTO_JOBS_PER_POLL
                and not item["draft"] and item["state"] == "open" and not job
            ):
                try:
                    job_id = await asyncio.to_thread(
                        self._trigger_from_github, watcher.repo, number,
                        watcher.dry_run, watcher.profile,
                    )
                    item["job"] = {"job_id": job_id, "status": "running"}
                    item["triggered"] = True
                    triggered += 1
                except Exception as exc:  # noqa: BLE001
                    item["trigger_error"] = str(exc)

        watcher.detected = detected[:50]
        watcher.polls += 1
        watcher.last_poll_at = _now()
        watcher.last_error = ""
        self._save()
        return watcher.public()

    # ---- 路由 ----

    def _require_watcher(self, watcher_id: str) -> Watcher:
        watcher = self._watchers.get(watcher_id)
        if watcher is None:
            raise HTTPException(status_code=404, detail="Watcher 不存在")
        return watcher

    def _register_routes(self) -> None:
        router = self.router

        @router.get("/api/config")
        async def console_config():
            from .engine.policy import default_policy, load_policy

            runtime = self._get_runtime()
            cfg = runtime.cfg
            default_file = Path("policy/default.yml")
            pol = load_policy(default_file) if default_file.is_file() else default_policy()
            kind = runtime.github_auth_kind
            login = ""
            if kind == "app":
                login = str((runtime.app_auth.status() or {}).get("slug") or "")
            elif cfg.github_token and runtime.nodes.github is not None:
                try:
                    login = str((runtime.nodes.github.get_user() or {}).get("login") or "")
                except Exception:  # noqa: BLE001 - 拿不到身份不影响配置展示
                    login = ""
            return {
                "github_configured": kind != "none",
                "github_auth": kind,
                "github_login": login,
                "github_app": runtime.github_status(),
                "public_url": cfg.public_url,
                "default_repos": list(cfg.github_repos),
                "merge_mode": getattr(cfg.merge_mode, "value", str(cfg.merge_mode)),
                "merge_method": cfg.merge_method,
                "review_mode": cfg.review_mode or "policy",
                "sandbox_backend": cfg.sandbox_backend,
                "budget_max_cny": cfg.budget_max_cny,
                "max_review_rounds": cfg.max_review_rounds,
                "max_fix_rounds": cfg.max_fix_rounds,
                "post_merge_watch_hours": cfg.post_merge_watch_hours,
                "mcp_enabled": bool(pol.agents.mcp.enabled and not cfg.mcp_disabled),
                "tool_protocol": str(pol.agents.tools.protocol),
            }

        @router.get("/api/mcp/status")
        async def mcp_status(probe: int = 0):
            from .engine.policy import default_policy, load_policy
            from .mcp import McpClientManager

            runtime = self._get_runtime()
            cfg = runtime.cfg
            default_file = Path("policy/default.yml")
            pol = load_policy(default_file) if default_file.is_file() else default_policy()
            mgr = getattr(runtime.nodes, "mcp_manager", None) or McpClientManager()
            statuses = mgr.probe_all(pol.agents.mcp, force_stdio=bool(probe))
            return {
                "enabled": bool(pol.agents.mcp.enabled and not cfg.mcp_disabled),
                "max_capability": str(pol.agents.mcp.max_capability),
                "external_max_capability": str(pol.agents.mcp.external_max_capability),
                "tool_protocol": str(pol.agents.tools.protocol),
                "servers": [s.model_dump() for s in statuses],
            }

        @router.get("/api/repos")
        async def list_repos(q: str = ""):
            runtime = self._get_runtime()
            cfg = runtime.cfg
            fallback = [
                {"full_name": name, "private": None, "default_branch": cfg.target_branch or "main",
                 "description": "来自 CODEGUARD_REPOS 配置", "html_url": f"https://github.com/{name}",
                 "language": "", "pushed_at": "", "open_issues": None}
                for name in cfg.github_repos
            ]
            if runtime.github_auth_kind == "app":
                try:
                    app_repos = await asyncio.to_thread(runtime.app_auth.repos)
                except GitHubError as exc:
                    return {"repos": fallback, "source": "config", "message": str(exc)}
                needle_app = q.strip().lower()
                picked = [
                    dict(item) for item in app_repos
                    if not needle_app
                    or needle_app in str(item.get("full_name") or "").lower()
                    or needle_app in str(item.get("description") or "").lower()
                ]
                return {
                    "repos": picked,
                    "source": "app",
                    "app_slug": runtime.app_auth.status().get("slug", ""),
                    "installations": len({item.get("installation_id") for item in picked}),
                    "message": "" if picked else "App 已安装，但当前 installation 没有可访问仓库",
                }

            client = runtime.nodes.github
            if client is None or not cfg.github_token:
                return {"repos": fallback, "source": "config", "message": "未配置 GitHub 凭据（App 或 GITHUB_TOKEN）"}
            try:
                raw = await asyncio.to_thread(client.list_repos)
            except GitHubError as exc:
                return {"repos": fallback, "source": "config", "message": str(exc)}
            needle = q.strip().lower()
            repos = []
            for item in raw:
                full_name = str(item.get("full_name") or "")
                if not full_name:
                    continue
                if needle and needle not in full_name.lower():
                    continue
                repos.append({
                    "full_name": full_name,
                    "private": bool(item.get("private")),
                    "default_branch": str(item.get("default_branch") or "main"),
                    "description": str(item.get("description") or ""),
                    "html_url": str(item.get("html_url") or ""),
                    "language": str(item.get("language") or ""),
                    "pushed_at": str(item.get("pushed_at") or ""),
                    "open_issues": item.get("open_issues_count"),
                })
            return {"repos": repos, "source": "github", "message": ""}

        @router.get("/api/pulls")
        async def list_pulls(repo: str = Query(..., min_length=3), state: str = "open"):
            if state not in PR_STATES:
                raise HTTPException(status_code=400, detail=f"state 只允许 {PR_STATES}")
            runtime = self._get_runtime()
            client = runtime.nodes.github
            if client is None:
                raise HTTPException(status_code=400, detail="未配置 GitHub 凭据，无法拉取 PR")
            try:
                raw = await asyncio.to_thread(client.list_pulls, repo, state=state, per_page=50)
            except GitHubError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            jobs = {(item.get("repo"), item.get("pr_number")): item for item in self._jobs_index()}
            pulls = []
            for pr in raw or []:
                number = int(pr.get("number") or 0)
                pulls.append({
                    "number": number,
                    "title": str(pr.get("title") or ""),
                    "author": str((pr.get("user") or {}).get("login") or ""),
                    "state": str(pr.get("state") or ""),
                    "draft": bool(pr.get("draft")),
                    "merged": bool(pr.get("merged_at")),
                    "head_ref": str((pr.get("head") or {}).get("ref") or ""),
                    "base_ref": str((pr.get("base") or {}).get("ref") or ""),
                    "head_sha": str((pr.get("head") or {}).get("sha") or ""),
                    "html_url": str(pr.get("html_url") or ""),
                    "created_at": str(pr.get("created_at") or ""),
                    "updated_at": str(pr.get("updated_at") or ""),
                    "comments": int(pr.get("comments") or 0),
                    "labels": [str((x or {}).get("name") or "") for x in (pr.get("labels") or [])],
                    "job": jobs.get((repo, number)),
                })
            return {"repo": repo, "state": state, "pulls": pulls, "fetched_at": _now()}

        @router.get("/api/jobs")
        async def list_jobs(repo: str = "", limit: int = 100):
            items = self._jobs_index()
            if repo:
                items = [item for item in items if item.get("repo") == repo]
            items.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
            return {"jobs": items[: max(1, min(limit, 500))], "total": len(items)}

        @router.get("/api/jobs/{job_id}/trace")
        async def job_trace(job_id: str, limit: int = 80):
            runtime = self._get_runtime()
            snap = runtime.snapshot(job_id)
            if not snap.get("values"):
                raise HTTPException(status_code=404, detail="Job 不存在")
            states: list[dict] = []
            try:
                history = runtime.graph.get_state_history(
                    runtime._config(job_id), limit=max(2, min(limit, 200)),  # noqa: SLF001
                )
                for state in history:
                    created = state.created_at
                    states.append({
                        "values": dict(state.values or {}),
                        "next": list(state.next or []),
                        "created_at": created if isinstance(created, str) else created.isoformat(),
                        "step": int((state.metadata or {}).get("step", 0) or 0),
                    })
            except Exception as exc:  # noqa: BLE001 - 历史不可用时降级为空轨迹
                logger.warning("读取 %s 历史失败: %s", job_id, exc)
            steps = build_trace(states)
            return {
                "job_id": job_id,
                "steps": steps,
                "current": current_node(states),
                "next": snap.get("next") or [],
                "interrupt": snap.get("interrupt"),
                "done": bool(snap.get("done")),
                "tracked_snapshots": len(states),
            }

        @router.get("/api/watch")
        async def list_watchers():
            items = [w.public() for w in self._watchers.values()]
            items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
            return {"watchers": items}

        @router.post("/api/watch")
        async def create_watcher(req: WatchRequest):
            repo = req.repo.strip()
            if "/" not in repo or repo.count("/") != 1:
                raise HTTPException(status_code=400, detail="repo 必须为 owner/name")
            if req.state not in PR_STATES:
                raise HTTPException(status_code=400, detail=f"state 只允许 {PR_STATES}")
            if any(w.repo == repo for w in self._watchers.values()):
                raise HTTPException(status_code=409, detail=f"{repo} 已在定时查看中")
            watcher = Watcher(
                id=uuid.uuid4().hex[:8],
                repo=repo,
                interval_sec=req.interval_sec,
                state=req.state,
                auto_review=req.auto_review,
                dry_run=req.dry_run,
                profile=req.profile,
            )
            self._watchers[watcher.id] = watcher
            self._spawn(watcher)
            await self.poll_once(watcher)
            self._save()
            return watcher.public()

        @router.patch("/api/watch/{watcher_id}")
        async def patch_watcher(watcher_id: str, req: WatchPatch):
            watcher = self._require_watcher(watcher_id)
            if req.state is not None:
                if req.state not in PR_STATES:
                    raise HTTPException(status_code=400, detail=f"state 只允许 {PR_STATES}")
                watcher.state = req.state
            if req.interval_sec is not None:
                watcher.interval_sec = req.interval_sec
            if req.auto_review is not None:
                watcher.auto_review = req.auto_review
            if req.dry_run is not None:
                watcher.dry_run = req.dry_run
            if watcher.task is None or watcher.task.done():
                self._spawn(watcher)
            self._save()
            return watcher.public()

        @router.post("/api/watch/{watcher_id}/poll")
        async def poll_watcher(watcher_id: str):
            watcher = self._require_watcher(watcher_id)
            return await self.poll_once(watcher)

        @router.delete("/api/watch/{watcher_id}")
        async def delete_watcher(watcher_id: str):
            watcher = self._require_watcher(watcher_id)
            if watcher.task and not watcher.task.done():
                watcher.task.cancel()
            self._watchers.pop(watcher_id, None)
            self._save()
            return {"ok": True}


__all__ = ["ConsoleAPI", "Watcher", "WatchRequest", "WatchPatch"]
