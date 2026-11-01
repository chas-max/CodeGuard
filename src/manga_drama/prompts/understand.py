"""顶层需求理解 Agent 的提示词。

聊天式改造：支持对话历史+摘要、revision 模式识别、精准追问。
"""
from __future__ import annotations

import json


def _fmt_conversation_history(history: list[dict], summary: str | None) -> str:
    """格式化对话历史上下文（摘要 + 最近轮次）。"""
    parts = []
    if summary:
        parts.append(f"【历史摘要】\n{summary}")
    if history:
        recent = history[-10:]  # 最近 10 条消息直接展示
        lines = []
        for msg in recent:
            role_label = "用户" if msg.get("role") == "user" else "助手"
            lines.append(f"{role_label}：{msg.get('content', '')}")
        parts.append("【最近对话】\n" + "\n".join(lines))
    return "\n\n".join(parts) if parts else "（暂无历史对话）"


def _fmt_qa(clarification_qa: list[dict]) -> str:
    if not clarification_qa:
        return "（无）"
    lines = []
    for item in clarification_qa:
        lines.append(f"Q{item.get('round','?')}: {item.get('question','')}")
        lines.append(f"A: {item.get('answer','')}")
    return "\n".join(lines)


def assess_prompt(
    user_input: str,
    clarification_qa: list[dict],
    round_num: int,
    max_clarify: int,
    conversation_history: list[dict] | None = None,
    conversation_summary: str | None = None,
) -> str:
    """评估信息完整度并决策：出 brief 或继续追问。

    新增对话历史上下文，使 LLM 能感知完整沟通背景，做出更精准的判断。
    每轮追问必须针对最关键的单一缺失维度，避免一次提问多个问题让用户困惑。
    """
    history_ctx = _fmt_conversation_history(
        conversation_history or [], conversation_summary
    )
    qa_str = _fmt_qa(clarification_qa)
    remaining = max_clarify - round_num

    return f"""你是漫剧创作的需求理解助手，擅长通过精确追问理解用户真实创作意图。
你的目标：在充分理解用户需求后，输出一份完整的漫剧制作简报（Brief）。

=== 对话历史 ===
{history_ctx}

=== 本次创意输入 ===
{user_input}

=== 历史追问与回答 ===
{qa_str}

=== 决策规则 ===
- 剩余追问机会：{remaining} 次（已用 {round_num}/{max_clarify}）
- 若以下核心维度均已明确，输出 action="brief"：
  题材类型、主要人物（至少1个）、核心故事/情节方向、基调/氛围
- 若仍有关键缺失且还有追问机会，输出 action="clarify"
- 追问原则：每次只问一个最关键的问题，问题要具体且易回答
- 若追问机会用尽，必须基于已有信息合理推断，输出 brief（在 assumptions 中注明推断）

输出 JSON（只输出 JSON）：
- 追问：{{"action":"clarify","question":"你想做什么题材的漫剧？（如古风/现代/奇幻/武侠）","missing":["题材类型"]}}
- 出 brief：{{"action":"brief","brief":{{"title":"漫剧标题","genre":"题材类型","theme":"主题立意","tone":"基调风格","target_duration_sec":30,"audience":"受众群体","characters":[{{"name":"名字","role":"主角/配角"}}],"plot_summary":"剧情梗概","plot_points":["情节关键点1","情节关键点2"],"taboos":["禁忌点"],"style_prefix":"全局画风前缀","negative_prompt":"全局负向提示词"}},"assumptions":["合理推断项"]}}"""


def revision_classify_prompt(
    user_message: str,
    current_status: str,
    brief: dict | None,
    draft: str | None,
    conversation_history: list[dict] | None = None,
    conversation_summary: str | None = None,
) -> str:
    """识别用户修改请求并提炼为结构化指令。

    判断用户的新消息是：
    1. 对内容的修改要求（revision）→ 提炼为精简指令传递给下层
    2. 继续澄清需求（clarify）→ 仍在 briefing 阶段
    3. 其他（询问进度/闲聊等）→ 直接回复，不触发流程

    同时输出修改应该路由到哪个节点（write_draft / director_plan / understand）。
    """
    history_ctx = _fmt_conversation_history(
        conversation_history or [], conversation_summary
    )
    brief_str = json.dumps(brief, ensure_ascii=False) if brief else "（未生成）"
    draft_preview = (draft or "（未生成）")[:500]

    return f"""你是漫剧创作的需求理解助手，当前处于聊天式交互模式。
用户发来一条新消息，你需要判断它的意图，并提炼出精简的操作指令。

=== 对话历史 ===
{history_ctx}

=== 当前制作状态 ===
状态：{current_status}
创作简报：{brief_str}
剧本草稿（前500字）：{draft_preview}

=== 用户新消息 ===
{user_message}

=== 判断规则 ===
1. **revision**（修改请求）：用户对已生成内容提出修改/调整要求
   - 路由 "write_draft"：修改剧情/台词/剧本内容 → 从写剧本开始
   - 路由 "director_plan"：修改角色设定/整体方向/重大改动 → 从导演规划开始
   - 路由 "understand"：用户要全面改变创意方向，接近重新来过 → 回到需求理解
2. **clarify**：用户是在继续补充初始需求（brief 尚未生成或刚生成），不是修改已有内容
3. **chat**：询问进度/表达满意/无关话题，无需流程操作

输出 JSON（只输出 JSON）：
- 修改：{{"intent":"revision","route":"write_draft","refined_request":"精简的修改指令（清晰、具体、可操作，30字以内）","reply_to_user":"简短确认用户修改意图的回复"}}
- 继续澄清：{{"intent":"clarify","route":"understand","refined_request":"","reply_to_user":"好的，我来确认一下..."}}
- 闲聊/进度：{{"intent":"chat","route":"","refined_request":"","reply_to_user":"直接回复用户的话"}}"""


def summarize_conversation_prompt(
    conversation_history: list[dict],
    existing_summary: str | None,
) -> str:
    """压缩对话历史为摘要，保留关键信息，控制 token 消耗。

    采用增量摘要：将旧摘要 + 新增对话 → 新摘要。
    保留：用户确认的创作意图、修改历史、关键决策点。
    """
    history_lines = []
    for msg in conversation_history:
        role_label = "用户" if msg.get("role") == "user" else "助手"
        history_lines.append(f"{role_label}：{msg.get('content', '')}")
    history_str = "\n".join(history_lines)

    existing_str = f"\n现有摘要：\n{existing_summary}" if existing_summary else ""

    return f"""请将以下漫剧创作对话历史压缩为简洁摘要，保留关键创作信息。{existing_str}

新增对话：
{history_str}

摘要要求：
1. 保留：用户的核心创作意图、已确认的修改要求、关键决策（如角色设定、剧情方向）
2. 省略：重复内容、客套话、进度查询
3. 用中文第三人称描述，不超过300字
4. 格式：先总体创意方向，再列关键修改历史

输出纯文本摘要（不要 JSON，不要标题）："""
