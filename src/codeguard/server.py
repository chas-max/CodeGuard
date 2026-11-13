"""CodeGuard FastAPI 服务：GitHub webhook、手动 PR 审查、SSE、人工恢复与健康检查。"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import load_settings
from .console import ConsoleAPI
from .engine.diff import IGNORE_DIRS
from .errors import ConfigError, GitHubError
from .github.app_api import AppAPI
from .github.webhook import is_supported_action, verify_signature
from .models import FileChange, PullRequestContext
from .runtime import CodeGuardRuntime

logger = logging.getLogger("codeguard.server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

rt: Optional[CodeGuardRuntime] = None
_DELIVERY_MAX = 2000
_delivery_queue: deque[str] = deque()
_deliveries: set[str] = set()
_jobs: set[str] = set()
# job_id -> 展示用元信息（repo/pr/title/source），供控制台列表与 PR 标注使用。
_job_meta: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global rt
    cfg = load_settings()
    try:
        cfg.validate(require_github=False, require_docker=False)
    except ConfigError as exc:
        logger.warning("配置校验提示（开发模式继续）: %s", exc)
    rt = CodeGuardRuntime(cfg)
    rt._loop = asyncio.get_running_loop()
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    await console_api.startup()
    try:
        yield
    finally:
        await console_api.shutdown()


app = FastAPI(title="CodeGuard PR Review API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


def _spawn_bg(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)

    def _done(t: asyncio.Task):
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("后台任务崩溃: %s", exc, exc_info=exc)

    task.add_done_callback(_done)
    return task


def _require_rt() -> CodeGuardRuntime:
    if rt is None:
        raise HTTPException(status_code=500, detail="Runtime not initialized")
    return rt


def _seen_delivery(delivery: str) -> bool:
    """有界去重：GitHub 重投由 delivery id 挡住，长期运行不会无限增长。"""
    if not delivery:
        return False
    if delivery in _deliveries:
        return True
    _deliveries.add(delivery)
    _delivery_queue.append(delivery)
    while len(_delivery_queue) > _DELIVERY_MAX:
        _deliveries.discard(_delivery_queue.popleft())
    return False


def _refresh_installations(runtime: CodeGuardRuntime) -> None:
    """installation 事件后刷新 installation/repo 缓存，失败只记日志。"""
    try:
        runtime.app_auth.invalidate()
        runtime.app_auth.installations(refresh=True)
        runtime.app_auth.repos(refresh=True)
        runtime.refresh_github()
    except Exception as exc:  # noqa: BLE001 - 缓存刷新失败不能影响 webhook 应答
        logger.warning("刷新 GitHub App installation 失败: %s", exc)


def _pr_from_github(repo: str, number: int) -> PullRequestContext:
    client = _require_rt().nodes.github
    if client is None:
        raise HTTPException(status_code=400, detail="未配置 GITHUB_TOKEN，无法拉取 PR")
    pr = client.get_pr(repo, number)
    files = client.list_files(repo, number)
    base, head = pr["base"], pr["head"]
    ctx = PullRequestContext(
        repo=repo, number=number,
        base_ref=base["ref"], base_sha=base.get("sha", ""),
        head_ref=head["ref"], head_sha=head.get("sha", ""),
        title=pr.get("title", ""), body=pr.get("body") or "",
        author=(pr.get("user") or {}).get("login", ""),
        is_fork=bool(head.get("repo") and head["repo"].get("full_name") != repo),
        is_draft=bool(pr.get("draft")),
        mergeable=bool(pr.get("mergeable", True)),
        additions=int(pr.get("additions", 0)), deletions=int(pr.get("deletions", 0)),
        changed_file_count=int(pr.get("changed_files", 0)),
        default_branch="main", html_url=pr.get("html_url", ""),
        labels=[str((item or {}).get("name", "")) for item in (pr.get("labels") or [])],
        created_at=pr.get("created_at", ""),
        changed_files=[
            FileChange(
                path=f["filename"], status=f.get("status", "modified"),
                additions=int(f.get("additions", 0)), deletions=int(f.get("deletions", 0)),
                patch=f.get("patch", "") or "",
            ) for f in files
        ],
    )
    return ctx


def _start_job(pr: PullRequestContext, source: str = "api", **overrides) -> str:
    runtime = _require_rt()
    job_id = uuid.uuid4().hex[:12]
    _jobs.add(job_id)
    _job_meta[job_id] = {
        "repo": pr.repo,
        "pr_number": pr.number,
        "title": pr.title,
        "author": pr.author,
        "source": source,
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "dry_run": bool(overrides.get("dry_run")),
        "html_url": pr.html_url,
    }
    runtime.update_progress(job_id, "已入队，等待执行...", 0)
    _spawn_bg(asyncio.to_thread(runtime.start, pr, job_id, **overrides))
    return job_id


def _trigger_from_github(repo: str, number: int, dry_run: bool = False, profile: str = "") -> str:
    """定时轮询发现新 PR 时复用同一条入队路径。"""
    pr = _pr_from_github(repo, number)
    if profile:
        pr = pr.model_copy(update={"profile": profile})
    return _start_job(pr, source="watch", dry_run=bool(dry_run))


def _live_job_item(job_id: str) -> dict:
    runtime = _require_rt()
    meta = _job_meta.get(job_id, {})
    progress = runtime.get_progress(job_id)
    values: dict = {}
    snap: dict = {}
    if job_id in _jobs:
        try:
            snap = runtime.snapshot(job_id)
        except Exception:  # noqa: BLE001 - 列表接口不应因单任务损坏而失败
            snap = {}
        values = snap.get("values") or {}
    outcome = str(values.get("outcome") or "")
    if snap.get("interrupt"):
        status = "waiting_human"
    elif not values:
        status = "queued"
    elif snap.get("done"):
        status = "done" if outcome else "failed"
    else:
        status = "running"
    risk = values.get("risk_tier") or {}
    return {
        "job_id": job_id,
        "repo": meta.get("repo") or values.get("repo", ""),
        "pr_number": meta.get("pr_number") or values.get("pr_number", 0),
        "title": meta.get("title") or values.get("pr_title", ""),
        "author": meta.get("author") or values.get("author", ""),
        "tier": risk.get("tier", ""),
        "source": meta.get("source", ""),
        "status": status,
        "outcome": outcome,
        "progress": progress,
        "started_at": meta.get("started_at") or values.get("started_at", ""),
        "finished_at": values.get("finished_at", ""),
        "duration_sec": None,
        "cost_cny": round(sum(
            float(item.get("cost_cny", 0.0)) for item in (values.get("budget_events") or [])
        ), 4),
        "llm_calls": int(values.get("llm_calls", 0) or 0),
        "findings_total": len(values.get("findings") or []),
        "findings_blocking": len(values.get("blocking_findings") or []),
        "approvals": len(values.get("human_approvals") or []),
        "required_approvals": int(risk.get("min_human_approvals", 0) or 0),
        "next": list(snap.get("next") or []),
        "requires_human": bool(snap.get("interrupt")),
        "html_url": meta.get("html_url", ""),
        "live": True,
    }


def _jobs_index() -> list[dict]:
    """内存中的活动任务 + reports/jobs 里的历史记录，按 job_id 去重。"""
    runtime = _require_rt()
    items: dict[str, dict] = {}
    reports = Path(runtime.cfg.reports_dir) / "jobs"
    if reports.is_dir():
        for path in reports.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            job_id = str(record.get("job_id") or path.stem)
            outcome = str(record.get("outcome") or "")
            items[job_id] = {
                "job_id": job_id,
                "repo": record.get("repo", ""),
                "pr_number": record.get("pr_number", 0),
                "title": record.get("title", ""),
                "author": record.get("author", ""),
                "tier": record.get("tier", ""),
                "source": "",
                "status": "done" if outcome else "failed",
                "outcome": outcome,
                "progress": {"status_text": record.get("message", ""), "percentage": 100},
                "started_at": record.get("started_at", ""),
                "finished_at": record.get("finished_at", ""),
                "duration_sec": record.get("duration_sec"),
                "cost_cny": record.get("cost_cny", 0),
                "llm_calls": record.get("llm_calls", 0),
                "findings_total": record.get("findings_total", 0),
                "findings_blocking": record.get("findings_blocking", 0),
                "approvals": record.get("approvals", 0),
                "required_approvals": record.get("required_approvals", 0),
                "next": [],
                "requires_human": False,
                "html_url": "",
                "live": False,
            }
    for job_id in list(dict.fromkeys([*_jobs, *runtime.progress.keys()])):
        items[job_id] = _live_job_item(job_id)
    return list(items.values())


class ReviewRequest(BaseModel):
    repo: str
    pr_number: int = Field(gt=0)
    base_ref: str = "main"
    base_sha: str = ""
    head_ref: str = "feature"
    head_sha: str = ""
    title: str = ""
    body: str = ""
    author: str = ""
    is_fork: bool = False
    local_repo: str = ""
    base_root: str = ""
    changed_files: list[str] = Field(default_factory=list)
    dry_run: bool = False


class ResumeRequest(BaseModel):
    approved: bool = False
    feedback: str = ""
    approver: str = ""
    finding_outcomes: dict[str, str] = Field(default_factory=dict)


@app.post("/webhooks/github")
async def github_webhook(request: Request):
    runtime = _require_rt()
    secret = runtime.github_webhook_secret()
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not secret:
        raise HTTPException(status_code=401, detail="未配置 GitHub webhook secret（App 未接入或 GITHUB_WEBHOOK_SECRET 为空）")
    if not verify_signature(secret, body, signature):
        raise HTTPException(status_code=401, detail="webhook 验签失败")
    event = request.headers.get("X-GitHub-Event", "")
    delivery = request.headers.get("X-GitHub-Delivery", "")
    if _seen_delivery(delivery):
        return {"ok": True, "duplicate": True}
    # App 已接入时只接受 App 级投递，避免同路径的仓库 webhook 被误当 App 事件。
    target_type = request.headers.get("X-GitHub-Hook-Installation-Target-Type", "")
    if runtime.github_auth_kind == "app" and target_type and target_type != "integration":
        return {"ok": True, "ignored": f"non-app delivery: {target_type}"}
    if event in ("installation", "installation_repositories"):
        asyncio.create_task(asyncio.to_thread(_refresh_installations, runtime))
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            payload = {}
        return {
            "ok": True,
            "event": event,
            "action": payload.get("action", ""),
            "installation": (payload.get("installation") or {}).get("id"),
        }
    if event != "pull_request":
        return {"ok": True, "ignored": f"unsupported event {event}"}
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="非法 JSON") from exc
    if not is_supported_action(payload):
        return {"ok": True, "ignored": payload.get("action")}
    try:
        pr = _pr_from_github(payload["repository"]["full_name"], int(payload["pull_request"]["number"]))
    except (KeyError, GitHubError) as exc:
        raise HTTPException(status_code=400, detail=f"读取 PR 失败: {exc}") from exc
    if pr.is_draft:
        return {"ok": True, "ignored": "draft"}
    job_id = _start_job(pr, source="webhook", delivery_id=delivery)
    return {"ok": True, "job_id": job_id}


@app.post("/api/reviews")
async def start_review(req: ReviewRequest):
    runtime = _require_rt()
    if req.local_repo:
        root = Path(req.local_repo).resolve()
        if not root.is_dir():
            raise HTTPException(status_code=400, detail=f"本地仓库不存在: {root}")
        files = [FileChange(path=p) for p in req.changed_files] or [
            FileChange(path=p.relative_to(root).as_posix())
            for p in sorted(root.rglob("*"))
            if p.is_file() and not any(part in IGNORE_DIRS for part in p.relative_to(root).parts)
        ]
        pr = PullRequestContext(
            repo=req.repo, number=req.pr_number,
            base_ref=req.base_ref, base_sha=req.base_sha,
            head_ref=req.head_ref, head_sha=req.head_sha,
            title=req.title, body=req.body, author=req.author,
            is_fork=req.is_fork, changed_files=files,
        )
        overrides = {
            "repo_root": str(root),
            "base_root": req.base_root or str(root),
            "dry_run": bool(req.dry_run),
        }
    else:
        pr = _pr_from_github(req.repo, req.pr_number)
        overrides = {}
    job_id = _start_job(pr, source="api", **overrides)
    return {"job_id": job_id, "snapshot": runtime.snapshot(job_id) if job_id in runtime.progress else None}


def _job_exists(job_id: str) -> bool:
    runtime = _require_rt()
    if job_id in _jobs or job_id in runtime.progress:
        return True
    if (Path(runtime.cfg.reports_dir) / "jobs" / f"{job_id}.json").is_file():
        return True
    try:  # 进程重启后仍可通过 checkpoint 恢复只读视图
        return bool(runtime.snapshot(job_id).get("values"))
    except Exception:  # noqa: BLE001
        return False


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    runtime = _require_rt()
    if not _job_exists(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"snapshot": runtime.snapshot(job_id)}


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str):
    runtime = _require_rt()
    if not _job_exists(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return StreamingResponse(
        runtime.event_stream(job_id), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str, req: ResumeRequest):
    runtime = _require_rt()
    if not _job_exists(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    snapshot = runtime.snapshot(job_id)
    if not snapshot.get("interrupt"):
        raise HTTPException(status_code=400, detail="Job is not interrupted")
    if req.approved and not req.approver.strip():
        raise HTTPException(status_code=400, detail="人工审批必须填写审批人")
    _spawn_bg(asyncio.to_thread(runtime.resume, job_id, {
        "approved": req.approved,
        "feedback": req.feedback,
        "approver": req.approver,
        "finding_outcomes": req.finding_outcomes,
    }))
    return {"ok": True}


@app.get("/api/health")
async def health():
    runtime = _require_rt()
    docker_ok, docker_msg = True, "not checked"
    if runtime.cfg.sandbox_backend == "docker":
        from .sandbox.health import check_docker
        docker_ok, docker_msg = check_docker()
    app_status = runtime.app_auth.status()
    return {
        "ok": True,
        "github_configured": runtime.github_auth_kind != "none",
        "github_auth": runtime.github_auth_kind,
        "github_pat_configured": bool(runtime.cfg.github_token),
        "github_app": {
            "configured": app_status["configured"],
            "slug": app_status["slug"],
            "installations": app_status["installations"],
            "webhook_configured": app_status["webhook_configured"],
        },
        "public_url": runtime.cfg.public_url,
        "sandbox_backend": runtime.cfg.sandbox_backend,
        "docker": {"ok": docker_ok, "message": docker_msg},
        "sandbox_error": runtime.nodes.sandbox_error,
    }


# ────────────────────────── 控制台（仓库/PR/追踪/定时轮询） ──────────────────────────

console_api = ConsoleAPI(
    get_runtime=_require_rt,
    start_job=_start_job,
    trigger_from_github=_trigger_from_github,
    jobs_index=_jobs_index,
)
app.include_router(console_api.router)

app_api = AppAPI(get_runtime=_require_rt, refresh_github=lambda: _require_rt().refresh_github())
app.include_router(app_api.router)


def _frontend_dir() -> Path | None:
    """前端构建产物目录：优先 WEB_DIST，其次仓库内 web/dist。"""
    candidates = []
    explicit = os.environ.get("WEB_DIST", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path(__file__).resolve().parents[2] / "web" / "dist")
    for path in candidates:
        if (path / "index.html").is_file():
            return path
    return None


_web_dir = _frontend_dir()
if _web_dir is not None:
    app.mount("/ui", StaticFiles(directory=str(_web_dir), html=True), name="ui")

    @app.get("/", include_in_schema=False)
    async def _root():
        return RedirectResponse(url="/ui/")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("codeguard.server:app", host="127.0.0.1", port=8002, reload=False)
