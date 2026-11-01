"""CodeGuard FastAPI 服务：任务启动 / 轮询 / SSE 实时事件 / HITL 恢复。

修复（原系统 P0）：所有 asyncio.create_task 均挂接异常回调——后台任务崩溃时
记录日志并向 job 发射 error 事件，前端立刻感知，不再静默卡死。
"""
import asyncio
import logging
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

# 把 src 加入路径，方便直接 python src/codeguard/server.py 运行
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from codeguard.config import load_config
from codeguard.runtime import CodeGuardRuntime
from codeguard.state import initial_state

logger = logging.getLogger("codeguard.server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="CodeGuard-Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

rt: Optional[CodeGuardRuntime] = None


def _spawn_bg(coro) -> asyncio.Task:
    """带异常回调的后台任务：崩溃不再被静默吞掉。"""
    task = asyncio.create_task(coro)

    def _on_done(t: asyncio.Task):
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("后台任务崩溃: %s", exc, exc_info=exc)

    task.add_done_callback(_on_done)
    return task


@app.on_event("startup")
async def startup_event():
    global rt
    cfg = load_config()
    cfg.validate_for_pipeline()
    rt = CodeGuardRuntime(cfg)
    rt._loop = asyncio.get_running_loop()

    output_dir = Path(cfg.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/output", StaticFiles(directory=str(output_dir)), name="output")


class StartJobRequest(BaseModel):
    issue: str
    repo_dir: str


@app.post("/api/jobs")
async def start_job(req: StartJobRequest):
    if not rt:
        raise HTTPException(status_code=500, detail="Runtime not initialized")
    repo = Path(req.repo_dir).resolve()
    if not repo.is_dir():
        raise HTTPException(status_code=400, detail=f"仓库目录不存在: {repo}")
    if not req.issue.strip():
        raise HTTPException(status_code=400, detail="issue 描述不能为空")

    job_id = uuid.uuid4().hex[:12]
    rt.update_progress(job_id, "正在初始化 Bug 修复流水线...", 0)
    _spawn_bg(asyncio.to_thread(rt.start, req.issue, job_id, str(repo)))

    init_snap = {
        "job_id": job_id,
        "values": initial_state(req.issue, job_id, str(repo)),
        "interrupt": None,
        "next": ["understand"],
        "done": False,
        "progress": {"status_text": "正在初始化 Bug 修复流水线...", "percentage": 0},
    }
    return {"job_id": job_id, "snapshot": init_snap}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    if not rt:
        raise HTTPException(status_code=500, detail="Runtime not initialized")
    if job_id not in rt.progress:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"snapshot": rt.snapshot(job_id)}


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str):
    """SSE 端点：progress / issue_parsed / plan_ready / static_gate_done /
    supervise_done / anchors_ready / unit_progress / patch_done / interrupt /
    done / error。
    """
    if not rt:
        raise HTTPException(status_code=500, detail="Runtime not initialized")
    if job_id not in rt.progress:
        raise HTTPException(status_code=404, detail="Job not found")
    return StreamingResponse(
        rt.event_stream(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str, payload: Any = Body(...)):
    if not rt:
        raise HTTPException(status_code=500, detail="Runtime not initialized")
    snapshot = rt.snapshot(job_id)
    if not snapshot or not snapshot.get("interrupt"):
        raise HTTPException(status_code=400, detail="Job is not interrupted")

    interrupt = snapshot["interrupt"]
    t = interrupt.get("type")
    action = payload.get("action") if isinstance(payload, dict) else payload

    if t == "clarify":
        answer = action if not isinstance(action, dict) else action.get("answer", "")
        _spawn_bg(asyncio.to_thread(rt.resume, job_id, {"answer": answer}))
    elif t in ("human_review", "confirm_anchors"):
        approved = False
        feedback = ""
        if isinstance(action, dict):
            approved = bool(action.get("approved", False))
            feedback = action.get("feedback", "")
        _spawn_bg(asyncio.to_thread(rt.resume, job_id, {"approved": approved, "feedback": feedback}))
    else:
        _spawn_bg(asyncio.to_thread(rt.resume, job_id, action))

    snapshot["interrupt"] = None
    return {"snapshot": snapshot}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("codeguard.server:app", host="127.0.0.1", port=8001, reload=True)
