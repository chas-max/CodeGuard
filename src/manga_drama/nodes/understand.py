"""顶层需求理解 Agent + 澄清中断 + 对话式修改支持。

架构：
- understand：LLM 节点，评估信息完整度（首次 briefing）或识别用户修改意图（revision 模式）。
  维护对话历史与摘要，支持跨轮记忆。
- clarify_ask：interrupt 节点，向用户追问或呈现当前状态供用户提修改建议。
- _maybe_summarize：当对话历史超过阈值时，自动压缩为摘要（增量更新）。

对话模式（chat_mode）：
- "briefing"：需求澄清阶段，理解用户创意意图，产出 brief
- "revision"：用户对已生成内容提出修改，识别目标节点并提炼指令
- "idle"：流程自动执行中，等待下一次用户输入
"""
from __future__ import annotations

import datetime

from langgraph.types import interrupt

from ..prompts.understand import (
    assess_prompt,
    revision_classify_prompt,
    summarize_conversation_prompt,
)
from ..state import WorkflowState
from ._common import call_llm_json, settle_costs

# 超过此数量的历史消息时触发摘要压缩（保留最近 N 条原文）
_SUMMARY_TRIGGER = 20
_SUMMARY_KEEP_RECENT = 10


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _maybe_summarize(state: WorkflowState, ctx) -> tuple[list[dict], str | None, list[dict]]:
    """若对话历史超过阈值，压缩旧轮次为摘要。

    返回 (new_history_to_keep, new_summary, costs)。
    - 将旧消息（超出保留窗口的部分）+ 现有摘要 → LLM 压缩 → 新摘要
    - 保留最近 _SUMMARY_KEEP_RECENT 条作为原文上下文
    """
    history: list[dict] = state.get("conversation_history") or []
    existing_summary: str | None = state.get("conversation_summary")
    costs: list[dict] = []

    if len(history) <= _SUMMARY_TRIGGER:
        return [], existing_summary, costs  # 返回空列表表示无需更新

    # 需要压缩：旧的 → 摘要，保留最近 N 条
    to_summarize = history[:-_SUMMARY_KEEP_RECENT]
    keep_recent = history[-_SUMMARY_KEEP_RECENT:]

    prompt = summarize_conversation_prompt(to_summarize, existing_summary)
    obj, c = call_llm_json(ctx, "understand_summary", prompt, temperature=0.3)
    costs.extend(c)

    # understand_summary 返回的是纯文本，不是 JSON；兼容处理
    new_summary = obj.get("summary", "") if isinstance(obj, dict) else existing_summary
    if not new_summary:
        # LLM 可能直接返回文本而非 JSON，尝试从 content 取
        new_summary = existing_summary or ""

    return keep_recent, new_summary, costs


def _add_message(role: str, content: str) -> dict:
    """构造对话消息记录。"""
    return {"role": role, "content": content, "timestamp": _now_iso()}


def understand(state: WorkflowState, ctx) -> dict:
    """评估信息完整度（briefing）或识别修改意图（revision）。

    - briefing 模式：判断用户创意信息是否足够出 brief，不足则准备追问
    - revision 模式：识别用户修改请求，提炼指令，路由到目标节点
    """
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.update_progress(state.get("job_id"), "正在理解您的需求...", 10)

    cfg = ctx.cfg
    chat_mode = state.get("chat_mode", "briefing")
    conversation_history: list[dict] = state.get("conversation_history") or []
    conversation_summary: str | None = state.get("conversation_summary")

    # ── 摘要压缩 ──
    keep_recent, new_summary, sum_costs = _maybe_summarize(state, ctx)
    update = settle_costs(state, ctx, sum_costs)
    if keep_recent:
        # 用压缩后的 keep_recent 替换 conversation_history（非 append，需重置）
        # 注：conversation_history 用 append_list reducer，所以此处返回增量
        # 实际上我们不能"替换"只能"追加"。采用标记方式：摘要更新后历史视为已压缩
        update["conversation_summary"] = new_summary
        conversation_summary = new_summary

    # ── revision 模式：识别修改意图 ──
    if chat_mode == "revision":
        user_message = state.get("user_input", "")
        current_status = state.get("status", "")
        brief = state.get("brief")
        draft = state.get("draft")

        prompt = revision_classify_prompt(
            user_message,
            current_status,
            brief,
            draft,
            conversation_history,
            conversation_summary,
        )
        obj, costs = call_llm_json(ctx, "understand", prompt, temperature=0.3)
        update = settle_costs(state, ctx, costs)

        intent = obj.get("intent", "chat")
        route = obj.get("route", "")
        refined = obj.get("refined_request", "")
        reply = obj.get("reply_to_user", "")

        # 记录助手回复到对话历史
        new_msgs = [
            _add_message("user", user_message),
            _add_message("assistant", reply or f"好的，我将{refined}"),
        ]

        if intent == "revision" and route:
            update.update({
                "user_revision_request": refined,
                "_revision_target": route,
                "chat_mode": "revision",
                "conversation_history": new_msgs,
                # 若用户要求重新制作，清除旧的视频制作状态
                **({"video_path": None, "shots": [], "production_attempt_count": 0}
                   if route in ("director_plan", "understand") else {}),
            })
            return update

        if intent == "clarify":
            update.update({
                "chat_mode": "briefing",
                "conversation_history": new_msgs,
                "_revision_target": None,
            })
            return update

        # intent == "chat"：直接回复，不改变流程
        update.update({
            "conversation_history": new_msgs,
            "_revision_target": None,
            "pending_clarification": {
                "question": reply or "有什么我可以帮您的？",
                "missing": [],
                "round": state.get("clarification_round", 0),
                "is_chat_reply": True,  # 标记：这是直接回复，不是追问
            },
        })
        return update

    # ── briefing 模式：评估信息完整度 ──
    round_num = state.get("clarification_round", 0)
    qa = state.get("clarification_qa", [])
    user_input = state.get("user_input", "")

    prompt = assess_prompt(
        user_input, qa, round_num, cfg.max_clarify,
        conversation_history, conversation_summary
    )
    obj, costs = call_llm_json(ctx, "understand", prompt, temperature=0.3)
    update = settle_costs(state, ctx, costs)

    action = obj.get("action")
    if action == "brief" or round_num >= cfg.max_clarify:
        brief = obj.get("brief") or {}
        # 记录助手完成简报的消息
        update.update({
            "brief": brief,
            "status": "briefed",
            "pending_clarification": None,
            "chat_mode": "idle",
            "conversation_history": [
                _add_message("assistant", f"✅ 已整理创作简报：《{brief.get('title', '未命名')}》，开始制作！"),
            ],
        })
        return update

    # 需追问
    question = obj.get("question") or "请补充更多创作细节。"
    missing = obj.get("missing") or []
    update.update({
        "pending_clarification": {
            "question": question,
            "missing": missing,
            "round": round_num + 1,
        },
        "status": "clarifying",
    })
    return update


def clarify_ask(state: WorkflowState, ctx) -> dict:
    """中断追问用户（briefing）或等待用户修改建议（revision/idle）。

    resume 后：
    - briefing 模式：把回答并入 clarification_qa，更新对话历史
    - revision/idle 模式：将用户消息设为 user_input，切换为 revision 模式
    不调 LLM（interrupt 节点）。
    """
    pending = state.get("pending_clarification") or {}
    chat_mode = state.get("chat_mode", "briefing")

    payload = {
        "type": "clarify",
        "question": pending.get("question", "请继续输入您的创作想法，或对已生成内容提出修改建议。"),
        "missing": pending.get("missing", []),
        "round": pending.get("round", state.get("clarification_round", 0) + 1),
        "chat_mode": chat_mode,
        "is_chat_reply": pending.get("is_chat_reply", False),
    }
    answer = interrupt(payload)
    if not isinstance(answer, str):
        answer = str(answer)

    now = _now_iso()

    # briefing 模式：记录追问历史
    if chat_mode == "briefing" and not pending.get("is_chat_reply"):
        round_num = state.get("clarification_round", 0) + 1
        qa = state.get("clarification_qa", []) + [{
            "round": round_num,
            "question": pending.get("question", ""),
            "answer": answer,
        }]
        return {
            "clarification_qa": qa,
            "clarification_round": round_num,
            "pending_clarification": None,
            "status": "clarifying",
            "conversation_history": [
                {"role": "assistant", "content": pending.get("question", ""), "timestamp": now},
                {"role": "user", "content": answer, "timestamp": now},
            ],
        }

    # revision/idle 模式或纯聊天回复：用户的新输入视为修改请求
    return {
        "user_input": answer,
        "pending_clarification": None,
        "chat_mode": "revision",
        "status": "clarifying",
        "conversation_history": [
            {"role": "user", "content": answer, "timestamp": now},
        ],
    }


def route_after_understand(state: WorkflowState, ctx) -> str:
    """理解 Agent 的路由。

    - _revision_target 有值：路由到用户指定的修改目标节点
    - brief 已产出且是 briefing 模式：进导演规划
    - pending_clarification 有值：进追问中断
    - 默认：追问
    """
    revision_target = state.get("_revision_target")
    if revision_target:
        # 合法的路由目标：write_draft / director_plan / understand（回到自身走 clarify）
        if revision_target in ("write_draft", "director_plan"):
            return revision_target
        # 其他（如 understand 自身）→ 重新追问
        return "clarify_ask"

    if state.get("brief") and state.get("chat_mode") == "idle":
        return "director_plan"

    return "clarify_ask"
