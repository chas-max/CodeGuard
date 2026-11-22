"""运行时封装：启动/恢复 PR 审查任务、SSE 事件广播、快照读取。"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, AsyncGenerator, Optional

from langgraph.types import Command

from .config import Settings
from .github.app_auth import CredentialStore, GitHubAppAuth
from .github.client import GitHubClient
from .graph import build_graph, make_checkpointer
from .models import PullRequestContext
from .nodes import ReviewNodes
from .state import ReviewState, initial_state


class CodeGuardRuntime:
    def __init__(
        self,
        cfg: Settings,
        nodes: Optional[ReviewNodes] = None,
        checkpointer=None,
        app_auth: Optional[GitHubAppAuth] = None,
    ):
        self.cfg = cfg
        self.checkpointer = checkpointer or make_checkpointer(cfg.checkpoint_path)
        self.app_auth = app_auth or GitHubAppAuth(
            CredentialStore(cfg.github_app_store), api_url=cfg.github_api_url,
        )
        try:
            self.app_auth.load()
        except Exception:  # noqa: BLE001 - 凭据损坏按未接入处理，状态里会体现
            pass
        self._owns_github = nodes is None
        provider = self.app_auth.token_for_repo if self._app_enabled() else None
        self.nodes = nodes or ReviewNodes(cfg, token_provider=provider)
        if self._owns_github:
            self.nodes.github = self._build_github_client()
        self.nodes.rt = self
        self.graph = build_graph(self.nodes, checkpointer=self.checkpointer)
        self.progress: dict[str, dict] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._lock = threading.Lock()
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    # ── GitHub 鉴权装配 ──

    def _app_enabled(self) -> bool:
        if self.cfg.github_auth_mode == "pat":
            return False
        return self.app_auth.configured

    def _build_github_client(self):
        if self._app_enabled():
            return GitHubClient(
                None,
                token_provider=self.app_auth.token_for_repo,
                token_invalidator=self.app_auth.invalidate,
                api_url=self.cfg.github_api_url,
            )
        if self.cfg.github_token:
            return GitHubClient(self.cfg.github_token, api_url=self.cfg.github_api_url)
        return None

    def refresh_github(self) -> None:
        """App 连接/断开后重新装配 client 与 token provider；不改图拓扑。"""
        provider = self.app_auth.token_for_repo if self._app_enabled() else None
        self.nodes.token_provider = provider
        if self._owns_github:
            # 不关闭旧 client：可能仍有 job 线程在用它发请求；连接/断开是低频操作。
            self.nodes.github = self._build_github_client()

    def github_webhook_secret(self) -> str:
        """App 自动注册的 secret 优先，其次才是 .env 的 GITHUB_WEBHOOK_SECRET。"""
        return self.app_auth.webhook_secret() or (self.cfg.github_webhook_secret or "")

    @property
    def github_auth_kind(self) -> str:
        if self._app_enabled():
            return "app"
        if self.cfg.github_token:
            return "pat"
        return "none"

    def github_status(self) -> dict:
        status = self.app_auth.status()
        status["mode"] = self.cfg.github_auth_mode
        status["auth"] = self.github_auth_kind
        status["pat_configured"] = bool(self.cfg.github_token)
        status["public_url"] = self.cfg.public_url
        status["webhook_url"] = (
            f"{self.cfg.public_url}/webhooks/github" if self.cfg.public_url else ""
        )
        return status

    def _config(self, job_id: str) -> dict:
        limit = int(getattr(self.cfg, "recursion_limit", 150) or 150)
        return {"configurable": {"thread_id": job_id}, "recursion_limit": limit}

    # ── SSE ──

    def emit_event(self, job_id: str, event_type: str, data: dict | None = None):
        with self._lock:
            queues = list(self._subscribers.get(job_id, []))
        if not queues or not self._loop:
            return
        event = {"type": event_type, "data": data or {}}
        for q in queues:
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:  # noqa: BLE001
                pass

    def subscribe(self, job_id: str) -> tuple[asyncio.Queue, list[asyncio.Queue]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._lock:
            self._subscribers.setdefault(job_id, []).append(q)
            return q, self._subscribers[job_id]

    def unsubscribe(self, job_id: str, q: asyncio.Queue):
        with self._lock:
            queues = self._subscribers.get(job_id, [])
            if q in queues:
                queues.remove(q)

    async def event_stream(self, job_id: str) -> AsyncGenerator[str, None]:
        q, _ = self.subscribe(job_id)
        try:
            progress = self.get_progress(job_id)
            yield f"event: progress\ndata: {json.dumps(progress, ensure_ascii=False)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"event: {event['type']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
                    if event["type"] in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            self.unsubscribe(job_id, q)

    def update_progress(self, job_id: str, text: str, percentage: int = 0):
        self.progress[job_id] = {"status_text": text, "percentage": percentage}
        self.emit_event(job_id, "progress", {"status_text": text, "percentage": percentage})

    def get_progress(self, job_id: str) -> dict:
        return self.progress.get(job_id, {"status_text": "准备中...", "percentage": 0})

    # ── 生命周期 ──

    def start(self, pr: PullRequestContext, job_id: str, **overrides) -> dict:
        try:
            self.graph.invoke(initial_state(pr, job_id, **overrides), self._config(job_id))
        except Exception as exc:  # noqa: BLE001
            self._record_crash(job_id, exc)
            raise
        snap = self.snapshot(job_id)
        self._emit_terminal(job_id, snap)
        return snap

    def resume(self, job_id: str, value: Any) -> dict:
        try:
            self.graph.invoke(Command(resume=value), self._config(job_id))
        except Exception as exc:  # noqa: BLE001
            self._record_crash(job_id, exc)
            raise
        snap = self.snapshot(job_id)
        self._emit_terminal(job_id, snap)
        return snap

    def _record_crash(self, job_id: str, err: Exception):
        self.progress[job_id] = {"status_text": f"执行异常: {err}", "percentage": 100}
        self.emit_event(job_id, "error", {"error": str(err), "status": "failed"})

    def _emit_terminal(self, job_id: str, snap: dict):
        values = snap.get("values", {})
        if snap.get("done"):
            if values.get("outcome"):
                self.emit_event(job_id, "done", {
                    "outcome": values.get("outcome"),
                    "merge_commit_sha": values.get("merge_commit_sha", ""),
                })
            else:
                self.emit_event(job_id, "error", {
                    "error": values.get("error", "任务结束但无 outcome"),
                    "status": values.get("status", "failed"),
                })
        elif snap.get("interrupt"):
            self.emit_event(job_id, "interrupt", snap["interrupt"])

    def snapshot(self, job_id: str) -> dict:
        st = self.graph.get_state(self._config(job_id))
        values: ReviewState = st.values
        interrupt = None
        if st.next and st.tasks:
            for task in st.tasks:
                if task.interrupts:
                    interrupt = task.interrupts[0].value
                    break
        progress = self.get_progress(job_id)
        if interrupt and not progress.get("status_text", "").startswith("等待"):
            # 中断信息可能来自 checkpoint 恢复，进程内进度已过期，以 interrupt 为准。
            progress = {"status_text": "等待人工介入", "percentage": int(progress.get("percentage", 0))}
        if not st.next and not interrupt:
            if values.get("outcome"):
                progress = {"status_text": f"完成: {values.get('outcome')}", "percentage": 100}
            else:
                progress = {"status_text": f"失败: {values.get('error', '')}", "percentage": 100}
        return {
            "job_id": job_id,
            "values": values,
            "interrupt": interrupt,
            "next": list(st.next or []),
            "done": not st.next,
            "progress": progress,
        }


__all__ = ["CodeGuardRuntime"]
