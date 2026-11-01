import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch
import asyncio

# 确保在导入 server 之前，将 src 添加到 sys.path
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from manga_drama.server import app
import manga_drama.server as server

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture
def mock_rt():
    mock = MagicMock()
    # 模拟 snapshot 方法返回值，模拟不同类型的中断
    mock.snapshot.return_value = {
        "job_id": "test_job_123",
        "values": {"status": "awaiting_human"},
        "interrupt": {"type": "human_review", "draft": "测试剧本"},
        "next": ["human_review"],
        "done": False,
        "progress": {"status_text": "等待人工审核", "percentage": 65}
    }
    return mock

def test_resume_job_clarify(client, mock_rt):
    # 替换全局 of rt 实例
    server.rt = mock_rt
    
    # 模拟为 clarify 中断
    mock_rt.snapshot.return_value["interrupt"] = {
        "type": "clarify",
        "question": "题材是什么？"
    }
    
    # 模拟前端发送的数据类型为 {"action": "古风伤春"} 或是字符串
    # 1. 字典格式
    payload = {"action": "古风伤春"}
    
    # 让我们使用 patch 来 mock rt.resume，看看是否被以正确的参数调用
    with patch.object(mock_rt, 'resume') as mock_resume:
        response = client.post("/api/jobs/test_job_123/resume", json=payload)
        # 等待后台 task 被调度执行完毕
        import time
        time.sleep(0.1)
        mock_resume.assert_any_call("test_job_123", "古风伤春")

def test_resume_job_human_review_approve(client, mock_rt):
    server.rt = mock_rt
    mock_rt.snapshot.return_value["interrupt"] = {
        "type": "human_review",
        "draft": "测试剧本"
    }
    
    # 前端发送：{"action": {"approved": true, "feedback": "赞"}}
    payload = {"action": {"approved": True, "feedback": "赞"}}
    
    with patch.object(mock_rt, 'resume') as mock_resume:
        response = client.post("/api/jobs/test_job_123/resume", json=payload)
        import time
        time.sleep(0.1)
        mock_resume.assert_any_call("test_job_123", {"approved": True, "feedback": "赞"})

def test_resume_job_confirm_anchors_approve(client, mock_rt):
    server.rt = mock_rt
    mock_rt.snapshot.return_value["interrupt"] = {
        "type": "confirm_anchors",
        "anchors": []
    }
    
    # 前端发送：{"action": {"approved": true}}
    payload = {"action": {"approved": True}}
    
    with patch.object(mock_rt, 'resume') as mock_resume:
        response = client.post("/api/jobs/test_job_123/resume", json=payload)
        import time
        time.sleep(0.1)
        mock_resume.assert_any_call("test_job_123", {"approved": True, "revisions": {}})
