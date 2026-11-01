"""下层机器审核 Agent：Reflection 两阶段自省式审核。

1. 初始审核：合规底线判断
2. 自省验证：复核是否有误判或漏判
"""
from __future__ import annotations

from ..prompts.review import review_prompt, review_verify_prompt
from ..state import WorkflowState
from ._common import call_llm_json, settle_costs


def machine_review(state: WorkflowState, ctx) -> dict:
    """法律/道德/暴力/版权 合规审核（Reflection 两阶段）。

    阶段1：初始审核 -> pass/fail verdict
    阶段2：自省验证 -> 确认或修正 verdict（防止误判/漏判）
    """
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.update_progress(state.get("job_id"), "正在进行内容安全与合规性审查...", 60)
    brief = state.get("brief") or {}
    draft = state.get("draft") or ""
    all_costs: list[dict] = []

    # 阶段1：初始审核
    obj, costs1 = call_llm_json(ctx, "review", review_prompt(draft, brief), temperature=0.2)
    all_costs.extend(costs1)
    initial_verdict = {
        "passed": bool(obj.get("passed", False)),
        "issues": obj.get("issues") or [],
        "severity": obj.get("severity") or "none",
        "category": obj.get("category") or "none",
        "feedback": obj.get("feedback") or "",
    }

    # 阶段2：自省验证
    obj2, costs2 = call_llm_json(
        ctx, "review_verify",
        review_verify_prompt(draft, brief, initial_verdict),
        temperature=0.1,
    )
    all_costs.extend(costs2)

    # 自省结果：如推翻初始判断，使用修订后的结果（同步更新 issues/severity/category）
    if not obj2.get("confirmed", True):
        revised_passed = obj2.get("revised_passed")
        passed = (
            bool(revised_passed)
            if revised_passed is not None
            else initial_verdict["passed"]
        )
        if passed:
            # 推翻为通过：清空问题字段，避免残留误导下游
            issues, severity, category = [], "none", "none"
        else:
            # 推翻为不通过：保留初始问题定位，采用修订后反馈
            issues, severity, category = (
                initial_verdict["issues"],
                initial_verdict["severity"],
                initial_verdict["category"],
            )
        verdict = {
            "passed": passed,
            "issues": issues,
            "severity": severity,
            "category": category,
            "feedback": obj2.get("revised_feedback") or initial_verdict["feedback"],
        }
    else:
        verdict = initial_verdict

    update = settle_costs(state, ctx, all_costs)
    update.update({"machine_verdict": verdict, "status": "reviewing"})

    # 发射 SSE 事件：审核完成
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.emit_event(state.get("job_id"), "review_done", {
            "passed": verdict.get("passed", False),
            "issues": verdict.get("issues", []),
            "feedback": verdict.get("feedback", ""),
        })

    return update
