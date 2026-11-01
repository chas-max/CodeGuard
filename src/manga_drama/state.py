"""LangGraph 全图状态 schema 与 reducers。

单一 WorkflowState 贯穿全图。关键字段：
- brief：顶层产出，全程只读。
- character_bible：导演产出，per-key merge（支持单卡更新如 current_costume）。
- shots：创作产出，按 shot_id keyed merge（支持幂等 resume 与单镜增量更新）。
- machine_verdict / human_decision / anchor_decision：审核闸门结果，驱动条件路由。
- budget_events：cost_collector 回填，check_budget 求和后写 budget_exceeded。
- 五个独立计数器：clarification_round / machine_revision_count / human_revision_count
  / anchor_round / production_attempt_count，各自封顶防死循环。

聊天式改造新增字段：
- conversation_history：完整对话历史（摘要压缩，无长度上限）。
- conversation_summary：对话历史摘要，压缩旧轮次以控制 token 消耗。
- user_revision_request：理解Agent 提炼后的用户修改请求。
- chat_mode：当前对话模式（briefing/revision/idle）。
- agent_issues：各 Agent 上报的问题与异常，由导演统筹处理。
- director_resolve_count：导演解决尝试计数，防死循环。
- director_resolution：导演给出的解决方案。
- reference_materials：Tavily 搜索到的参考资料（注入创作提示词）。
"""
from __future__ import annotations

from typing import Annotated, Optional

from typing_extensions import TypedDict

# ---- reducers ----


def merge_shots(left: list[dict], right: list[dict]) -> list[dict]:
    """按 shot_id 字段合并：后写覆盖先写字段，支持单镜增量更新与幂等 resume。"""
    by_id: dict[str, dict] = {s["shot_id"]: dict(s) for s in (left or [])}
    for s in right or []:
        sid = s.get("shot_id")
        if sid is None:
            continue
        by_id[sid] = {**by_id.get(sid, {}), **s}
    return list(by_id.values())


def merge_character_bible(left: dict, right: dict) -> dict:
    """按 char_id per-key 合并：支持单卡更新（如 current_costume）不丢其他卡。"""
    merged = dict(left or {})
    for k, v in (right or {}).items():
        merged[k] = {**merged.get(k, {}), **v}
    return merged


def append_list(left: list, right: list) -> list:
    """单调追加（用于日志/事件流，不触发消息去重语义）。"""
    return (left or []) + (right or [])


# ---- 子结构 TypedDict ----


class CharacterCard(TypedDict):
    char_id: str  # 如 C001
    name: str
    role: str  # 主角 / 配角 / 群演
    appearance_spec: str  # FROZEN 外观规格（脸/发/服装/配色/标志特征/画风，中英双语）
    base_seed: int  # 角色固定基础种子
    voice_id: str  # TTS 音色 ID，跨分镜锁定
    current_costume: str  # 受控可变：当前服装（剧情换装时导演更新，仅此字段可变）
    anchor_image_path: Optional[str]  # 锚定图路径（human_review 后生成，confirm 后 FROZEN）
    confirmed: bool  # 是否经人工确认


class Brief(TypedDict, total=False):
    title: str
    genre: str
    theme: str
    tone: str
    target_duration_sec: int
    audience: str
    characters: list[dict]  # 角色草稿（供导演细化成 CharacterCard）
    plot_summary: str
    plot_points: list[str]
    taboos: list[str]
    style_prefix: str  # 全局画风前缀，注入所有 image prompt
    negative_prompt: str  # 全局负向提示
    assumptions: list[str]  # 顶层超限时显式标注的未确认项


class Shot(TypedDict, total=False):
    shot_id: str  # 如 S001
    index: int
    scene_desc: str
    dialogue: str
    narration: str
    characters_in_shot: list[str]  # char_id 列表
    camera: str
    emotion: str
    duration_sec: float
    image_prompt: str  # prompt_builder 生成（已注入冻结规格 + style_prefix）
    negative_prompt: str
    seed: int
    image_path: Optional[str]
    audio_path: Optional[str]
    status: str  # pending/generating/generated/failed/skipped（幂等关键）
    image_attempts: int
    audio_attempts: int
    consistency_scores: dict[str, float]  # char_id -> 与锚定图相似度
    prev_shot_summary: Optional[str]  # 上一镜视觉摘要，跨镜连续性


class ReviewResult(TypedDict, total=False):
    passed: bool
    issues: list[str]  # 法律/暴力/色情/政治敏感/版权
    severity: str  # none/minor/major/critical
    category: str  # legal/violence/sexual/political/copyright/none
    feedback: str  # 强制结构化：定位 scene_id/shot_id/维度 + 具体修订点


class ConversationMessage(TypedDict):
    role: str   # "user" | "assistant"
    content: str
    timestamp: str  # ISO8601


class AgentIssue(TypedDict):
    agent: str          # 上报问题的 Agent 名称
    issue_type: str     # "llm_error" | "tool_error" | "quality" | "budget" | "t2i_error" | "tts_error"
    detail: str         # 问题详情
    timestamp: str      # ISO8601


# ---- 主状态 ----


class WorkflowState(TypedDict, total=False):
    # 顶层
    job_id: str  # = thread_id，checkpointer 寻址键
    user_input: str
    messages: Annotated[list, append_list]  # 对话消息（轻量，仅文本）
    clarification_qa: Annotated[list[dict], append_list]  # 追问与回答（可审计）
    clarification_round: int  # 顶层澄清计数，上限 max_clarify
    brief: Optional[Brief]

    # ---- 聊天式新增字段 ----
    # 对话历史（ConversationMessage 列表，完整记录），无长度上限，由摘要机制控制 token
    conversation_history: Annotated[list[dict], append_list]
    # 对话摘要（旧轮次压缩后的摘要文本，直接注入 LLM 上下文）
    conversation_summary: Optional[str]
    # 理解Agent 提炼后的用户修改请求（传递给下层的精简指令）
    user_revision_request: Optional[str]
    # 当前对话模式：briefing（需求澄清中）| revision（用户提修改）| idle（等待）
    chat_mode: str
    # 各 Agent 上报的问题与异常（导演统筹处理）
    agent_issues: Annotated[list[dict], append_list]
    # 导演解决尝试计数（防死循环）
    director_resolve_count: int
    # 导演给出的解决方案 {action: retry|escalate|skip|downgrade, target_node, strategy}
    director_resolution: Optional[dict]
    # Tavily 搜索到的参考资料摘要（注入创作提示词）
    reference_materials: Optional[str]
    # ---- 聊天式新增字段 end ----

    # 中层（导演）
    plan: Optional[dict]  # 场景数/时长/节奏/style_prefix
    character_bible: Annotated[dict, merge_character_bible]  # char_id -> CharacterCard
    director_decision: str  # revise / approve
    director_feedback: str
    # 创作层（大纲驱动：outline -> draft -> reflect）
    outline: Optional[str]  # 大纲文本（结构化场景描述），创作锚点
    outline_version: int  # 大纲版本号，首次=1，每次微调+1
    draft: Optional[str]
    shots: Annotated[list[dict], merge_shots]  # keyed merge，幂等更新
    # 审核层（machine/human 独立计数）
    machine_verdict: Optional[ReviewResult]
    machine_revision_count: int  # 机器打回计数，上限 max_machine
    human_decision: Optional[dict]  # {approved, feedback}
    human_revision_count: int  # 人工打回计数，上限 max_human
    # 锚定确认
    anchor_round: int  # 上限 max_anchor
    anchor_decision: Optional[dict]  # {approved, revisions:{char_id:...}}
    # 制作层
    production_attempt_count: int  # 一致性升级计数，上限 max_prod
    video_path: Optional[str]
    # 控制 / 可观测 / 成本
    production_log: Annotated[list[dict], append_list]  # 每步事件
    errors: Annotated[list[dict], append_list]
    budget_events: Annotated[list[dict], append_list]  # cost_collector 回填
    budget_exceeded: bool  # check_budget 写入，驱动路由 -> escalate
    status: str  # clarifying/briefed/planning/writing/reviewing/supervising/
    #            awaiting_human/confirming_anchors/producing/done/failed
    error: Optional[str]
    # 内部路由信号（不暴露给用户）：理解Agent 识别到修改请求后，指示图跳转到哪个节点
    _revision_target: Optional[str]


def initial_state(user_input: str, job_id: str) -> WorkflowState:
    """构造图的初始状态。"""
    return {
        "job_id": job_id,
        "user_input": user_input,
        "messages": [],
        "clarification_qa": [],
        "clarification_round": 0,
        "brief": None,
        # 聊天式新增
        "conversation_history": [],
        "conversation_summary": None,
        "user_revision_request": None,
        "chat_mode": "briefing",
        "agent_issues": [],
        "director_resolve_count": 0,
        "director_resolution": None,
        "reference_materials": None,
        "_revision_target": None,
        # 中层
        "plan": None,
        "character_bible": {},
        "director_decision": "",
        "director_feedback": "",
        "outline": None,
        "outline_version": 0,
        "draft": None,
        "shots": [],
        "machine_verdict": None,
        "machine_revision_count": 0,
        "human_decision": None,
        "human_revision_count": 0,
        "anchor_round": 0,
        "anchor_decision": None,
        "production_attempt_count": 0,
        "video_path": None,
        "production_log": [],
        "errors": [],
        "budget_events": [],
        "budget_exceeded": False,
        "status": "clarifying",
        "error": None,
    }
