"""下层文本创作 Agent：Plan-Execute-Reflection 三阶段流水线 + Tavily 参考资料搜索。

架构：
1. Search（参考资料）：调用 Tavily 搜索题材/人设相关资料（可选，有 key 时启用）
2. Plan（大纲）：从 brief 生成大纲，或按反馈微调大纲
3. Execute（创作）：严格按大纲写剧本（注入参考资料）
4. Reflect（自省）：检查剧本与大纲一致性，发现冲突则修订一次

计数策略：进入 write_draft 时，按当前活跃反馈来源递增对应计数器（首次创作不递增）。
- 人工打回 -> human_revision_count
- 导演 revise / 机器打回 -> machine_revision_count（非人工的修订统一计入，供路由封顶）
- 用户聊天修改 -> human_revision_count（与人工打回同源，计入人工计数）

合规优先：当反馈来源为机器审核（source=="machine"）时，Plan/Execute 阶段走 is_compliance
路径，解除"禁止大改"约束，合规底线高于大纲稳定性，确保审核反对的内容能被真正改掉。
"""
from __future__ import annotations

import datetime

from ..prompts.write import (
    outline_prompt,
    outline_revise_prompt,
    reflect_prompt,
    revise_with_outline_prompt,
    write_with_outline_prompt,
)
from ..state import WorkflowState
from ._common import call_llm_json, settle_costs


def _pick_feedback(state: WorkflowState) -> tuple[str | None, dict, str | None]:
    """返回 (feedback, counter_update, source)。按优先级判断当前活跃反馈来源。

    source: "human" | "director" | "machine" | "user_chat" | None；
    用于决定修订时是否解除大纲冻结约束（仅机器审核反馈走合规优先路径，允许大改）。
    """
    # 优先：用户聊天修改请求（来自理解 Agent 提炼）
    revision_req = state.get("user_revision_request")
    if revision_req and state.get("_revision_target") == "write_draft":
        return revision_req, {
            "human_revision_count": state.get("human_revision_count", 0) + 1
        }, "user_chat"

    human = state.get("human_decision") or {}
    if human.get("feedback") and not human.get("approved"):
        return human["feedback"], {
            "human_revision_count": state.get("human_revision_count", 0) + 1
        }, "human"
    if state.get("director_decision") == "revise" and state.get("director_feedback"):
        return state.get("director_feedback"), {
            "machine_revision_count": state.get("machine_revision_count", 0) + 1
        }, "director"
    mv = state.get("machine_verdict") or {}
    if mv and not mv.get("passed"):
        fb = mv.get("feedback") or (
            "请重新创作，确保内容符合合规底线（法律/暴力/色情/政治敏感/版权），"
            "不保留任何审核可能反对的情节。"
        )
        return fb, {
            "machine_revision_count": state.get("machine_revision_count", 0) + 1
        }, "machine"
    return None, {}, None


def _search_references(state: WorkflowState, ctx) -> tuple[str | None, list[dict]]:
    """Search 阶段：调用 Tavily 搜索与题材/人设相关的参考资料。

    返回 (reference_text, cost_events)。
    若 tavily_enabled=False 或搜索失败，返回 (None, [])，不影响主流程。
    """
    cfg = ctx.cfg
    if not cfg.tavily_enabled:
        return None, []

    try:
        from tavily import TavilyClient  # type: ignore
    except ImportError:
        return None, []

    brief = state.get("brief") or {}
    title = brief.get("title", "")
    genre = brief.get("genre", "")
    theme = brief.get("theme", "")
    characters = brief.get("characters", [])
    char_names = "、".join(c.get("name", "") for c in characters[:3] if c.get("name"))

    # 构建精准搜索词
    query_parts = []
    if char_names:
        query_parts.append(f"{char_names} 人物背景")
    if genre or theme:
        query_parts.append(f"{genre} {theme} 故事背景")
    if title:
        query_parts.append(title)
    query = " ".join(filter(None, query_parts)) or f"{genre}漫剧创作参考"

    try:
        api_key = cfg.require_tavily()
        client = TavilyClient(api_key=api_key)
        results = client.search(
            query=query,
            search_depth="basic",
            max_results=3,
            include_answer=True,
        )

        snippets = []
        if results.get("answer"):
            snippets.append(f"综合摘要：{results['answer']}")
        for r in (results.get("results") or [])[:3]:
            if r.get("content"):
                snippets.append(f"- {r.get('title', '')}: {r['content'][:300]}")

        ref_text = "\n".join(snippets) if snippets else None
        return ref_text, []

    except Exception as e:
        # Tavily 失败不影响主流程，记录到 agent_issues
        issue = {
            "agent": "write_draft",
            "issue_type": "tool_error",
            "detail": f"Tavily 搜索失败（已降级跳过）: {e}",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        # 将异常写入 state 但不终止流程
        ctx._pending_issues = getattr(ctx, "_pending_issues", []) + [issue]
        return None, []


def _plan_outline(
    state: WorkflowState, ctx, feedback: str | None, source: str | None
) -> tuple[str, int, list[dict]]:
    """Plan 阶段：生成或微调大纲。返回 (outline, outline_version, costs)。

    机器审核反馈（source=="machine"）走合规优先路径，允许大改核心剧情以消除合规风险。
    用户聊天修改（source=="user_chat"）视为人工指令，按普通修订处理。
    """
    brief = state.get("brief") or {}
    bible = state.get("character_bible") or {}
    current_outline = state.get("outline")
    current_version = state.get("outline_version", 0)

    costs: list[dict] = []

    if feedback and current_outline:
        is_compliance = source == "machine"
        prompt = outline_revise_prompt(
            brief, bible, current_outline, feedback, is_compliance=is_compliance
        )
        obj, c = call_llm_json(ctx, "write_outline", prompt, temperature=0.6)
        costs.extend(c)
        new_outline = obj.get("outline", current_outline)
        return new_outline, current_version + 1, costs
    elif not current_outline:
        prompt = outline_prompt(brief, bible)
        obj, c = call_llm_json(ctx, "write_outline", prompt, temperature=0.6)
        costs.extend(c)
        new_outline = obj.get("outline", "")
        return new_outline, 1, costs
    else:
        return current_outline, current_version, costs


def _execute_draft(
    state: WorkflowState, ctx, outline: str,
    feedback: str | None, source: str | None,
    reference_materials: str | None = None,
) -> tuple[str, list[dict]]:
    """Execute 阶段：按大纲写剧本。返回 (draft, costs)。

    机器审核反馈（source=="machine"）走合规优先路径，合规底线高于大纲忠实度。
    """
    brief = state.get("brief") or {}
    bible = state.get("character_bible") or {}
    costs: list[dict] = []

    if feedback:
        is_compliance = source == "machine"
        prompt = revise_with_outline_prompt(
            brief, bible, outline, feedback,
            is_compliance=is_compliance,
            reference_materials=reference_materials,
        )
    else:
        prompt = write_with_outline_prompt(
            brief, bible, outline,
            reference_materials=reference_materials,
        )

    obj, c = call_llm_json(ctx, "write", prompt, temperature=0.8)
    costs.extend(c)
    return obj.get("draft", ""), costs


def _reflect_draft(state: WorkflowState, ctx, outline: str, draft: str) -> tuple[str, list[dict]]:
    """Reflect 阶段：自省检查剧本与大纲一致性。返回 (final_draft, costs)。

    如果自省发现问题且提供了修订版，使用修订版；否则使用原 draft。
    """
    costs: list[dict] = []

    prompt = reflect_prompt(outline, draft)
    obj, c = call_llm_json(ctx, "write_reflect", prompt, temperature=0.2)
    costs.extend(c)

    if obj.get("passed", True):
        return draft, costs

    revised = obj.get("revised_draft", "")
    if revised:
        return revised, costs
    return draft, costs


def write_draft(state: WorkflowState, ctx) -> dict:
    """写剧本：Search-Plan-Execute-Reflection 四阶段。

    0. Search: 调用 Tavily 获取参考资料（可选）
    1. Plan: 生成/微调大纲（大纲是创作锚点，可细微修改，不可大改）
    2. Execute: 严格按大纲写剧本（注入参考资料）
    3. Reflect: 自省检查一致性，发现问题则修订一次
    """
    all_costs: list[dict] = []

    # 0. 检查反馈来源，更新计数器（source 决定是否走合规优先修订路径）
    feedback, counter_update, source = _pick_feedback(state)

    # 1. Search：Tavily 参考资料搜索
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.update_progress(state.get("job_id"), "编剧正在查阅相关背景资料...", 32)
    ref_materials, _ = _search_references(state, ctx)
    # 若搜索有结果则更新，否则沿用 state 中已有的（上次搜索结果可复用）
    if ref_materials is None:
        ref_materials = state.get("reference_materials")

    # 2. Plan：生成或微调大纲
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.update_progress(state.get("job_id"), "编剧正在构思并生成故事大纲...", 35)
    outline, outline_version, plan_costs = _plan_outline(state, ctx, feedback, source)
    all_costs.extend(plan_costs)

    # 3. Execute：按大纲写剧本
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.update_progress(state.get("job_id"), "编剧正在撰写剧本分镜脚本...", 45)
    draft, exec_costs = _execute_draft(state, ctx, outline, feedback, source, ref_materials)
    all_costs.extend(exec_costs)

    # 4. Reflect：自省一致性检查
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.update_progress(state.get("job_id"), "编剧正对剧本进行一致性自检微调...", 55)
    final_draft, reflect_costs = _reflect_draft(state, ctx, outline, draft)
    all_costs.extend(reflect_costs)

    # 5. 结算
    update = settle_costs(state, ctx, all_costs)
    update.update({
        "outline": outline,
        "outline_version": outline_version,
        "draft": final_draft,
        "reference_materials": ref_materials,
        "status": "writing",
        # 完成修改后清除修改请求信号，避免下次重入时误触发
        "user_revision_request": None,
        "_revision_target": None,
        "chat_mode": "idle",
    })
    update.update(counter_update)

    # 收集来自 _search_references 的 pending issues
    pending_issues = getattr(ctx, "_pending_issues", [])
    if pending_issues:
        update["agent_issues"] = pending_issues
        ctx._pending_issues = []

    # 发射 SSE 事件：剧本就绪
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.emit_event(state.get("job_id"), "draft_ready", {
            "draft": final_draft,
            "outline": outline,
            "outline_version": outline_version,
        })

    return update
