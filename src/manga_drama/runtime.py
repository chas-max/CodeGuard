"""运行时封装：启动/恢复任务、读取中断状态、SSE 事件广播。"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, AsyncGenerator, Optional

from langgraph.types import Command

from .config import MangaDramaConfig
from .graph import build_graph, make_checkpointer
from .nodes import GraphNodes
from .state import WorkflowState, initial_state


class MangaDramaRuntime:
    """对编译后图的薄封装，提供 start / resume / snapshot / 事件流。

    interrupt 状态由 SqliteSaver 持久化，进程重启后用同 job_id（=thread_id）
    调用 snapshot/resume 即从断点恢复。

    事件系统：每个 job 有一个 asyncio.Queue，后台线程中的节点通过 emit_event()
    发射事件，SSE 端点通过 subscribe() 消费事件流。
    """

    def __init__(
        self,
        cfg: MangaDramaConfig,
        nodes: Optional[GraphNodes] = None,
        checkpointer=None,
    ):
        self.cfg = cfg
        self.checkpointer = checkpointer or make_checkpointer(cfg.checkpoint_path)
        self.nodes = nodes or GraphNodes(cfg)
        self.nodes.rt = self  # Expose runtime instance to nodes
        self.graph = build_graph(self.nodes, checkpointer=self.checkpointer)
        self.progress: dict[str, dict] = {}
        # SSE 事件系统：每个 job_id 一个队列列表（支持多个订阅者）
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._lock = threading.Lock()
        # 保存主事件循环引用，供后台线程的 emit_event 使用
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def _config(self, job_id: str) -> dict:
        return {"configurable": {"thread_id": job_id}, "recursion_limit": 100}

    # ── SSE 事件系统 ──

    def emit_event(self, job_id: str, event_type: str, data: dict | None = None):
        """线程安全的事件发射。在后台线程中调用，将事件推送到所有订阅者的队列。

        Args:
            job_id: 任务 ID。
            event_type: 事件类型（progress / draft_ready / review_done / interrupt / done / error）。
            data: 事件数据字典。
        """
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
            except Exception:
                pass

    def subscribe(self, job_id: str) -> tuple[asyncio.Queue, list[asyncio.Queue]]:
        """为 SSE 端点创建一个事件订阅。返回 (queue, subscribers_list)。

        调用方负责在连接关闭时调用 unsubscribe()。
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._lock:
            if job_id not in self._subscribers:
                self._subscribers[job_id] = []
            self._subscribers[job_id].append(q)
        return q, self._subscribers.get(job_id, [])

    def unsubscribe(self, job_id: str, q: asyncio.Queue):
        """移除 SSE 订阅者的队列。"""
        with self._lock:
            queues = self._subscribers.get(job_id, [])
            if q in queues:
                queues.remove(q)

    async def event_stream(self, job_id: str) -> AsyncGenerator[str, None]:
        """SSE 事件流生成器。yield 格式化的 SSE 文本行。"""
        q, _ = self.subscribe(job_id)
        try:
            # 先发送当前进度快照
            progress = self.get_progress(job_id)
            yield f"event: progress\ndata: {self._json_dumps({'status_text': progress.get('status_text', ''), 'percentage': progress.get('percentage', 0)})}\n\n"

            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                    event_type = event.get("type", "message")
                    event_data = event.get("data", {})
                    yield f"event: {event_type}\ndata: {self._json_dumps(event_data)}\n\n"
                    # done 或 error 事件后结束流
                    if event_type in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    # 发送心跳保持连接
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            self.unsubscribe(job_id, q)

    @staticmethod
    def _json_dumps(obj: dict) -> str:
        """JSON 序列化，确保中文不被转义。"""
        import json
        return json.dumps(obj, ensure_ascii=False)

    # ── 进度更新 ──

    def update_progress(self, job_id: str, status_text: str, percentage: int = 0):
        """更新任务在内存中的实时进度与描述，同时发射 progress 事件。"""
        self.progress[job_id] = {
            "status_text": status_text,
            "percentage": percentage,
        }
        self.emit_event(job_id, "progress", {
            "status_text": status_text,
            "percentage": percentage,
        })

    def get_progress(self, job_id: str) -> dict:
        """获取任务的当前进度。"""
        return self.progress.get(job_id, {"status_text": "准备中...", "percentage": 0})

    # ── 任务生命周期 ──

    def start(self, user_input: str, job_id: str) -> dict:
        """启动新任务，跑到首个中断或结束。完成后发射 done/interrupt 事件。"""
        self.graph.invoke(initial_state(user_input, job_id), self._config(job_id))
        snap = self.snapshot(job_id)
        self._emit_terminal(job_id, snap)
        return snap

    def resume(self, job_id: str, value: Any) -> dict:
        """从中断恢复，value 为人工决策（字符串/字典）。完成后发射事件。"""
        self.graph.invoke(Command(resume=value), self._config(job_id))
        snap = self.snapshot(job_id)
        self._emit_terminal(job_id, snap)
        return snap

    def _emit_terminal(self, job_id: str, snap: dict):
        """任务到达终态时发射 done/interrupt 事件。"""
        if snap.get("done"):
            values = snap.get("values", {})
            if values.get("status") == "done":
                self.emit_event(job_id, "done", {
                    "video_path": values.get("video_path", ""),
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
        values: WorkflowState = st.values
        interrupt = None
        if st.next and st.tasks:
            for t in st.tasks:
                if t.interrupts:
                    interrupt = t.interrupts[0].value
                    break

        # 获取并计算当前进度
        progress = self.get_progress(job_id)
        if not st.next:
            if values.get("status") == "done":
                progress = {"status_text": "制作完成", "percentage": 100}
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
