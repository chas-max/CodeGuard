import sys
import uuid
from pathlib import Path

# 把 src 加入路径，方便直接 python src/manga_drama/server.py 运行
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Any, Dict, Optional

from manga_drama.config import load_config
from manga_drama.runtime import MangaDramaRuntime
from manga_drama.state import initial_state

app = FastAPI(title="Manga Drama AI API")

# Setup CORS for React/Vite
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Runtime on startup
rt: Optional[MangaDramaRuntime] = None

@app.on_event("startup")
async def startup_event():
    global rt
    cfg = load_config()
    cfg.validate_for_pipeline()
    rt = MangaDramaRuntime(cfg)
    # 确保后台线程能通过此引用发射 SSE 事件
    rt._loop = asyncio.get_running_loop()

    # Ensure output directory exists for static files
    output_dir = Path("data/drama_output")
    output_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/output", StaticFiles(directory="data/drama_output"), name="output")
    # 挂载素材目录（锚定图、分镜图片等）
    assets_dir = Path("data/drama_assets")
    assets_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/assets", StaticFiles(directory="data/drama_assets"), name="assets")

class StartJobRequest(BaseModel):
    idea: str

@app.post("/api/jobs")
async def start_job(req: StartJobRequest):
    if not rt:
        raise HTTPException(status_code=500, detail="Runtime not initialized")
    job_id = uuid.uuid4().hex[:12]

    # 异步在后台线程中启动，不阻塞 FastAPI 主事件循环
    asyncio.create_task(asyncio.to_thread(rt.start, req.idea, job_id))

    # 初始化内存进度
    rt.update_progress(job_id, "正在初始化漫剧制作流程...", 0)

    # 直接返回初始状态，让前端立刻开始轮询
    init_snap = {
        "job_id": job_id,
        "values": initial_state(req.idea, job_id),
        "interrupt": None,
        "next": ["understand"],
        "done": False,
        "progress": {"status_text": "正在初始化漫剧制作流程...", "percentage": 0}
    }
    return {"job_id": job_id, "snapshot": init_snap}

@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    if not rt:
        raise HTTPException(status_code=500, detail="Runtime not initialized")
    snapshot = rt.snapshot(job_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"snapshot": snapshot}

@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str):
    """SSE 端点：实时推送任务进度、剧本内容、中断事件。

    事件类型：
    - progress: 进度更新 {status_text, percentage}
    - draft_ready: 剧本就绪 {draft, outline}
    - review_done: 审核完成 {verdict}
    - supervise_done: 导演监督完成 {decision, feedback}
    - interrupt: 需要用户介入 {type, question, ...}
    - done: 任务完成 {video_path}
    - error: 任务失败 {error}
    """
    if not rt:
        raise HTTPException(status_code=500, detail="Runtime not initialized")
    snapshot = rt.snapshot(job_id)
    if not snapshot:
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

    # Handle different interrupt types mapping from frontend
    action = payload.get("action") if isinstance(payload, dict) else payload

    # 异步在后台线程中恢复执行
    if t == "clarify":
        # expects a string answer
        answer = action if not isinstance(action, dict) else action.get("answer", "")
        asyncio.create_task(asyncio.to_thread(rt.resume, job_id, answer))

    elif t == "human_review":
        approved = False
        feedback = ""
        if isinstance(action, dict):
            approved = action.get("approved", False)
            feedback = action.get("feedback", "")
        asyncio.create_task(asyncio.to_thread(rt.resume, job_id, {"approved": approved, "feedback": feedback}))

    elif t == "confirm_anchors":
        approved = False
        if isinstance(action, dict):
            approved = action.get("approved", False)
        asyncio.create_task(asyncio.to_thread(rt.resume, job_id, {"approved": approved, "revisions": {}}))

    else:
        asyncio.create_task(asyncio.to_thread(rt.resume, job_id, action))

    # 清理当前快照的 interrupt，避免前端在定时器轮询前产生 UI 闪烁
    snapshot["interrupt"] = None
    return {"snapshot": snapshot}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("manga_drama.server:app", host="127.0.0.1", port=8000, reload=True)
