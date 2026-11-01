"""运行时封装：启动/恢复任务、读取中断状态、SSE 事件广播（CodeGuard 版）。

修复（原系统 P0）：start/resume 的 graph.invoke 全程 try/except——任何节点异常
（含 GraphRecursionError）都会发射 error 事件并更新内存进度，前端立刻可见，
彻底杜绝"任务静默卡死、进度永远停住"。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, AsyncGenerator, Optional

from langgraph.types import Command

from .config import CodeGuardConfig
from .graph import build_graph, make_checkpointer
from .nodes import CodeGuardNodes
from .state import CodeGuardState, initial_state


class CodeGuardRuntime:
    """对编译后图的薄封装，提供 start / resume / snapshot / 事件流。

    interrupt 状态由 SqliteSaver 持久化，进程重启后用同 job_id（=thread_id）
    调用 snapshot/resume 即从断点恢复。
    """

    def __init__(
        self,
        cfg: CodeGuardConfig,
        nodes: Optional[CodeGuardNodes] = None,
        checkpointer=None,
    ):
        self.cfg = cfg
        self.checkpointer = checkpointer or make_checkpointer(cfg.checkpoint_path)
        self.nodes = nodes or CodeGuardNodes(cfg)
        self.nodes.rt = self  # 向节点暴露 runtime，供 SSE 事件发射
        self.graph = build_graph(self.nodes, checkpointer=self.checkpointer)
        self.progress: dict[str, dict] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._lock = threading.Lock()
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def _config(self, job_id: str) -> dict:
        return {"configurable": {"thread_id": job_id}, "recursion_limit": 100}

    # ── SSE 事件系统 ──

    def emit_event(self, job_id: str, event_type: str, data: dict | None = None):
        """线程安全的事件发射：推送到该 job 所有订阅者队列。"""
        with self._lock:
            queues = self._subscribers.get(job_id, [])
        if not queues:
            return
        event = {"type": event_type, "data": data or {}}
        loop = self._loop
        if not loop:
            return
        for q in queues:
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:  # noqa: BLE001 — 事件发射失败不阻塞主流程
                pass

    def subscribe(self, job_id: str) -> tuple[asyncio.Queue, list[asyncio.Queue]]:
        """为 SSE 端点创建事件订阅。返回 (queue, subscribers_list)。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._lock:
            if job_id not in self._subscribers:
                self._subscribers[job_id] = []
            self._subscribers[job_id].append(q)
        return q, self._subscribers.get(job_id, [])

    def unsubscribe(self, job_id: str, q: asyncio.Queue):
        with self._lock:
            queues = self._subscribers.get(job_id, [])
            if q in queues:
                queues.remove(q)

    async def event_stream(self, job_id: str) -> AsyncGenerator[str, None]:
        """SSE 事件流生成器。"""
        q, _ = self.subscribe(job_id)
        try:
            progress = self.get_progress(job_id)
            yield f"event: progress\ndata: {self._json_dumps({'status_text': progress.get('status_text', ''), 'percentage': progress.get('percentage', 0)})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                    event_type = event.get("type", "message")
                    event_data = event.get("data", {})
                    yield f"event: {event_type}\ndata: {self._json_dumps(event_data)}\n\n"
                    if event_type in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            self.unsubscribe(job_id, q)

    @staticmethod
    def _json_dumps(obj: dict) -> str:
        import json
        return json.dumps(obj, ensure_ascii=False)

    # ── 进度 ──

    def update_progress(self, job_id: str, status_text: str, percentage: int = 0):
        self.progress[job_id] = {"status_text": status_text, "percentage": percentage}
        self.emit_event(job_id, "progress", {
            "status_text": status_text,
            "percentage": percentage,
        })

    def get_progress(self, job_id: str) -> dict:
        return self.progress.get(job_id, {"status_text": "准备中...", "percentage": 0})

    # ── 任务生命周期 ──

    def start(self, user_input: str, job_id: str, repo_dir: str = "") -> dict:
        """启动新任务，跑到首个中断或结束。异常时发射 error 事件（不再静默卡死）。"""
        try:
            self.graph.invoke(initial_state(user_input, job_id, repo_dir), self._config(job_id))
        except Exception as e:  # noqa: BLE001 — 含 GraphRecursionError / LLM 耗尽
            self._record_crash(job_id, e)
            raise
        snap = self.snapshot(job_id)
        self._emit_terminal(job_id, snap)
        return snap

    def resume(self, job_id: str, value: Any) -> dict:
        """从中断恢复，value 为人工决策。异常时发射 error 事件。"""
        try:
            self.graph.invoke(Command(resume=value), self._config(job_id))
        except Exception as e:  # noqa: BLE001
            self._record_crash(job_id, e)
            raise
        snap = self.snapshot(job_id)
        self._emit_terminal(job_id, snap)
        return snap

    def _record_crash(self, job_id: str, err: Exception):
        """崩溃兜底：进度置败 + error 事件，前端/CLI 立刻可见。"""
        self.progress[job_id] = {
            "status_text": f"执行异常: {err}",
            "percentage": 100,
        }
        self.emit_event(job_id, "error", {"error": str(err), "status": "failed"})

    def _emit_terminal(self, job_id: str, snap: dict):
        """任务到达终态时发射 done/interrupt 事件。"""
        if snap.get("done"):
            values = snap.get("values", {})
            if values.get("status") == "done":
                self.emit_event(job_id, "done", {
                    "patch_path": values.get("patch_path", ""),
                    "coverage_report_path": values.get("coverage_report_path", ""),
                    "status": "done",
                })
            else:
                self.emit_event(job_id, "error", {
                    "error": values.get("error", "未知错误"),
                    "status": values.get("status", "failed"),
                })
        elif snap.get("interrupt"):
            self.emit_event(job_id, "interrupt", snap["interrupt"])

    def snapshot(self, job_id: str) -> dict:
        """读取当前状态与中断信息。"""
        st = self.graph.get_state(self._config(job_id))
        values: CodeGuardState = st.values
        interrupt = None
        if st.next and st.tasks:
            for t in st.tasks:
                if t.interrupts:
                    interrupt = t.interrupts[0].value
                    break

        progress = self.get_progress(job_id)
        if not st.next:
            if values.get("status") == "done":
                progress = {"status_text": "修复完成", "percentage": 100}
            elif values.get("status") == "failed":
                progress = {"status_text": f"失败: {values.get('error', '')}", "percentage": 100}

        return {
            "job_id": job_id,
            "values": values,
            "interrupt": interrupt,
            "next": list(st.next or []),
            "done": not st.next,
            "progress": progress,
        }
