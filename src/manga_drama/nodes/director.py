"""中层导演 Agent：规划+角色圣经、内容监督、全图异常统筹协调。

改造说明：
- director_resolve：新节点，统筹各 Agent 上报的问题与异常，给出解决策略
- director_supervise：发现严重问题时写入 agent_issues，交由 director_resolve 处理
- 各条件路由增加 director_resolve 分支
"""
from __future__ import annotations

import datetime

from langgraph.graph import END

from ..prompts.director import plan_prompt, supervise_prompt, supervise_verify_prompt
from ..prompts.director_resolve import resolve_prompt
from ..state import WorkflowState
from ._common import call_llm_json, settle_costs


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def director_plan(state: WorkflowState, ctx) -> dict:
    """根据 brief 产出 plan + 角色圣经（含 FROZEN 外观规格）。"""
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.update_progress(state.get("job_id"), "导演正在规划故事分镜与角色设定...", 25)
    brief = state.get("brief") or {}

    try:
        obj, costs = call_llm_json(ctx, "director_plan", plan_prompt(brief), temperature=0.5)
    except Exception as e:
        # LLM 调用失败：上报 agent_issues，由 director_resolve 处理
        return {
            "agent_issues": [{
                "agent": "director_plan",
                "issue_type": "llm_error",
                "detail": str(e),
                "timestamp": _now_iso(),
            }],
            "status": "planning",
        }

    update = settle_costs(state, ctx, costs)

    plan = obj.get("plan") or {}
    chars = obj.get("characters") or []

    bible: dict = {}
    for i, c in enumerate(chars, 1):
        cid = c.get("char_id") or f"C{i:03d}"
        try:
            seed = int(c.get("base_seed", 0) or 0)
        except (TypeError, ValueError):
            seed = 0
        bible[cid] = {
            "char_id": cid,
            "name": c.get("name", ""),
            "role": c.get("role", ""),
            "appearance_spec": c.get("appearance_spec", ""),
            "base_seed": seed,
            "voice_id": c.get("voice_id", "longxiaochun"),
            "current_costume": c.get("current_costume", ""),
            "anchor_image_path": None,
            "confirmed": False,
        }

    if not brief.get("style_prefix") and plan.get("style_prefix"):
        nb = dict(brief)
        nb["style_prefix"] = plan["style_prefix"]
        update["brief"] = nb

    update.update({
        "plan": plan,
        "character_bible": bible,
        "status": "planning",
        # 用户发起重新规划时清除旧修改信号
        "user_revision_request": None,
        "_revision_target": None,
        "chat_mode": "idle",
    })
    return update


def director_supervise(state: WorkflowState, ctx) -> dict:
    """机器审核通过后的内容监督（Reflection 两阶段）。

    1. 初始判断：连贯/人物/基调/艺术质量
    2. 自省验证：复核判断是否公平、是否遗漏问题
    """
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.update_progress(state.get("job_id"), "导演正在审阅剧本并进行自省监督...", 65)
    brief = state.get("brief") or {}
    draft = state.get("draft") or ""
    all_costs: list[dict] = []

    try:
        # 阶段1：初始判断
        obj, costs1 = call_llm_json(
            ctx, "director_supervise", supervise_prompt(draft, brief), temperature=0.3
        )
        all_costs.extend(costs1)
        initial_decision = obj.get("decision") or "revise"
        initial_feedback = obj.get("feedback") or ""

        # 阶段2：自省验证
        initial_judgment = {"decision": initial_decision, "feedback": initial_feedback}
        obj2, costs2 = call_llm_json(
            ctx, "director_supervise_verify",
            supervise_verify_prompt(draft, brief, initial_judgment),
            temperature=0.2,
        )
        all_costs.extend(costs2)
    except Exception as e:
        update = settle_costs(state, ctx, all_costs)
        update.update({
            "agent_issues": [{
                "agent": "director_supervise",
                "issue_type": "llm_error",
                "detail": str(e),
                "timestamp": _now_iso(),
            }],
            "director_decision": "approve",
            "director_feedback": "（导演监督 LLM 调用失败，强制放行至人工审核）",
            "status": "awaiting_human",
        })
        return update

    # 自省结果：如推翻初始判断，使用修订后的结果
    if not obj2.get("confirmed", True):
        decision = obj2.get("revised_decision") or initial_decision
        feedback = obj2.get("revised_feedback") or initial_feedback
    else:
        decision = initial_decision
        feedback = initial_feedback

    # 强制将决策设为 approve，将把关权交给人工审核，但保留 AI 导演反馈意见供界面参考
    update = settle_costs(state, ctx, all_costs)
    update.update({
        "director_decision": "approve",
        "director_feedback": feedback,
        "status": "awaiting_human",
    })

    # 发射 SSE 事件：导演监督完成
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.emit_event(state.get("job_id"), "supervise_done", {
            "decision": "approve",
            "feedback": feedback,
        })

    return update


def director_resolve(state: WorkflowState, ctx) -> dict:
    """统筹各 Agent 上报的问题与异常，给出解决策略。

    分析 agent_issues 列表，输出：
    - action: retry | downgrade | skip | escalate
    - target_node: 重试目标节点
    - strategy: 具体处理策略
    """
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.update_progress(state.get("job_id"), "导演正在分析异常并协调解决方案...", 50)

    issues = state.get("agent_issues") or []
    current_status = state.get("status", "")
    brief = state.get("brief")
    resolve_count = state.get("director_resolve_count", 0)
    cfg = ctx.cfg

    # 若无问题，直接放行（防止误路由）
    if not issues:
        return {
            "director_resolution": {"action": "skip", "target_node": "", "strategy": "无问题需要解决"},
            "director_resolve_count": resolve_count,
        }

    try:
        prompt = resolve_prompt(issues, current_status, brief, resolve_count, cfg.max_resolve)
        obj, costs = call_llm_json(ctx, "director_resolve", prompt, temperature=0.3)
        update = settle_costs(state, ctx, costs)
    except Exception:
        # 导演本身也 LLM 失败：直接 escalate
        update = {}
        obj = {
            "action": "escalate",
            "target_node": "",
            "strategy": "",
            "escalate_reason": "导演解决模块也 LLM 调用失败，无法继续",
        }

    action = obj.get("action", "escalate")
    target_node = obj.get("target_node", "")
    strategy = obj.get("strategy", "")
    escalate_reason = obj.get("escalate_reason", "")

    resolution = {
        "action": action,
        "target_node": target_node,
        "strategy": strategy,
        "escalate_reason": escalate_reason,
        "resolved_issues": obj.get("resolved_issues", []),
    }

    # 处理降级策略：downgrade Tavily → 禁用
    if action == "downgrade" and "tavily" in strategy.lower():
        # 注：cfg 是 frozen dataclass，无法直接修改；通过 ctx 临时标记
        ctx._tavily_disabled = True

    update.update({
        "director_resolution": resolution,
        "director_resolve_count": resolve_count + 1,
        # 处理完问题后清空 issues，避免重复触发
        "agent_issues": [],
        "production_log": [{
            "event": "director_resolve",
            "action": action,
            "strategy": strategy,
            "timestamp": _now_iso(),
        }],
    })

    if action == "escalate":
        update.update({
            "status": "failed",
            "error": escalate_reason or "导演判断无法继续推进，流程终止",
        })

    return update


# ---- 条件路由（确定性，不调 LLM）----


def route_after_machine(state: WorkflowState, ctx) -> str:
    # 有未处理的 agent_issues → 先走 director_resolve
    if state.get("agent_issues"):
        return "director_resolve"
    if state.get("budget_exceeded"):
        return "escalate"
    mv = state.get("machine_verdict") or {}
    if mv.get("passed"):
        return "director_supervise"
    if state.get("machine_revision_count", 0) >= ctx.cfg.max_machine:
        return "director_supervise"
    return "write_draft"


def route_after_supervise(state: WorkflowState, ctx) -> str:
    if state.get("agent_issues"):
        return "director_resolve"
    if state.get("budget_exceeded"):
        return "escalate"
    if state.get("director_decision") == "approve":
        return "human_review"
    total = state.get("machine_revision_count", 0) + state.get("human_revision_count", 0)
    if total >= (ctx.cfg.max_machine + ctx.cfg.max_human):
        return "escalate"
    return "write_draft"


def route_after_human(state: WorkflowState, ctx) -> str:
    if state.get("budget_exceeded"):
        return "escalate"
    d = state.get("human_decision") or {}
    if d.get("approved"):
        return "build_anchors"
    if state.get("human_revision_count", 0) >= ctx.cfg.max_human:
        return "escalate"
    return "write_draft"


def route_after_anchors(state: WorkflowState, ctx) -> str:
    if state.get("agent_issues"):
        return "director_resolve"
    d = state.get("anchor_decision") or {}
    if d.get("approved"):
        return "produce_video"
    if state.get("anchor_round", 0) >= ctx.cfg.max_anchor:
        return "escalate"
    return "build_anchors"


def route_after_produce(state: WorkflowState, ctx) -> str:
    if state.get("agent_issues"):
        return "director_resolve"
    if state.get("budget_exceeded"):
        return "escalate"
    if state.get("status") == "done":
        return END
    if state.get("production_attempt_count", 0) >= ctx.cfg.max_prod:
        return "escalate"
    return "produce_video"


def route_after_resolve(state: WorkflowState, ctx) -> str:
    """导演解决后的路由：根据解决方案决定下一步。"""
    resolution = state.get("director_resolution") or {}
    action = resolution.get("action", "escalate")

    if action == "escalate" or state.get("status") == "failed":
        return "escalate"

    target = resolution.get("target_node", "")
    if action in ("retry", "downgrade") and target:
        # 验证 target 是合法节点名
        valid_nodes = {
            "write_draft", "director_plan", "machine_review",
            "director_supervise", "produce_video", "build_anchors",
        }
        if target in valid_nodes:
            return target

    # skip / 无效 target → 尝试根据当前状态决定继续哪里
    status = state.get("status", "")
    status_map = {
        "planning": "write_draft",
        "writing": "machine_review",
        "reviewing": "director_supervise",
        "supervising": "human_review",
        "producing": "produce_video",
    }
    return status_map.get(status, "escalate")
