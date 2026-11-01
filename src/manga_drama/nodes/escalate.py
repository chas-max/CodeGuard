"""确定性 escalate 节点：达到重试上限/预算熔断时终止并保留产物。不调 LLM。

改造说明：写入 agent_issues 以便审计；escalate 前已由 director_resolve 尝试解决。
"""
from __future__ import annotations

import datetime

from ..state import WorkflowState


def escalate(state: WorkflowState, ctx) -> dict:
    reason = (
        state.get("error")
        or "预算熔断或达到重试上限，流程终止（已生成产物已保留）"
    )
    return {
        "status": "failed",
        "error": reason,
        "agent_issues": [{
            "agent": "escalate",
            "issue_type": "budget" if state.get("budget_exceeded") else "quality",
            "detail": reason,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }],
        "production_log": [{"event": "escalate", "reason": reason}],
    }
