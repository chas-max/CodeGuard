"""LangGraph 全图状态 schema 与 reducers（CodeGuard-Agent 版）。

单一 CodeGuardState 贯穿全图。与原漫剧 WorkflowState 的同构映射：
- brief            -> issue_context（故障现象解析产物，顶层只读）
- character_bible  -> symbol_bible（AST 符号契约圣经，merge_symbol_contracts per-key merge）
- shots            -> patch_units（补丁槽位，merge_patch_units 按 unit_id keyed merge）
- video_path       -> patch_path + coverage_report_path

四能力闭环关键字段：
- agent_issues：append-only 审计日志（所有异常留痕）
- open_issues：**无 reducer 的普通字段，节点返回即整体覆盖**——这是对原系统的关键修复：
  原版 director_resolve 返回 "agent_issues": [] 试图清空 append_list reducer 字段，
  实际是无效操作（空列表追加等于没清），导致 issue 永久累积、resolve 死循环。
  现在路由只看 open_issues，director_resolve 处理完返回 {"open_issues": []} 即真正清空。
- f2p_tests / p2p_tests：冻结的测试锚点（TDD Red 态 Ground Truth）

六个独立计数器（防死循环，与原系统同名同义）：
clarification_round / machine_revision_count / human_revision_count / anchor_round
/ production_attempt_count（沙箱自愈轮次）/ director_resolve_count
"""
from __future__ import annotations

from typing import Annotated, Optional

from typing_extensions import TypedDict

from .coding_agent.models import merge_patch_units, merge_symbol_contracts

__all__ = [
    "CodeGuardState",
    "IssueContext",
    "PatchUnitDict",
    "initial_state",
    "merge_patch_units",
    "merge_symbol_contracts",
    "append_list",
]


def append_list(left: list, right: list) -> list:
    """单调追加（用于日志/事件流/审计）。"""
    return (left or []) + (right or [])


# ---- 子结构 TypedDict ----


class IssueContext(TypedDict, total=False):
    """understand 节点产出的故障解析上下文（同构于漫剧 Brief）。"""

    title: str  # 问题一句话标题
    symptom: str  # 症状描述
    stack_trace: str  # 报错堆栈（若有）
    suspected_modules: list[str]  # 疑似受影响模块
    repro_steps: list[str]  # 复现步骤
    env_hints: str  # 环境线索（Python 版本/依赖等）
    missing_info: list[str]  # 缺失的关键信息（驱动 clarify_ask 追问）
    completeness: str  # clear | partial | unclear


class PatchUnitDict(TypedDict, total=False):
    """state.patch_units 的槽位条目（PatchUnit 的 dict 形态，供 reducer 合并）。"""

    unit_id: str
    target_file: str
    test_file: str
    issue_hypothesis: str
    patched_code: str
    git_diff: str
    status: str  # pending | writing | testing | passed | failed | skipped
    attempts: int
    f2p_passed: bool
    p2p_passed: bool
    last_traceback: str


# ---- 主状态 ----


class CodeGuardState(TypedDict, total=False):
    # 顶层
    job_id: str  # = thread_id，checkpointer 寻址键
    user_input: str  # 原始 issue 文本（GitHub Issue / 报错堆栈 / 待测模块描述）
    repo_dir: str  # 待修复仓库根目录（绝对路径）
    messages: Annotated[list, append_list]
    clarification_qa: Annotated[list[dict], append_list]
    clarification_round: int  # 上限 max_clarify
    issue_context: Optional[IssueContext]

    # 对话通道（复用原系统聊天式交互机制）
    conversation_history: Annotated[list[dict], append_list]
    conversation_summary: Optional[str]
    user_revision_request: Optional[str]
    chat_mode: str  # briefing（故障澄清中）| revision（补充信息）| idle

    # 异常通道：四能力闭环的枢纽
    agent_issues: Annotated[list[dict], append_list]  # append-only 审计日志
    open_issues: list[dict]  # 路由信号（覆盖式，director_resolve 处理后清空）
    director_resolve_count: int  # 上限 max_resolve（硬编码检查）
    director_resolution: Optional[dict]  # {action, target_node, strategy, escalate_reason}

    # 规划产物（四能力②）
    plan: Optional[dict]  # {plan_summary, repair_strategy: [...]}
    located_files: list[str]  # AST 定位的 Top-K 疑似故障文件
    symbol_bible: Annotated[dict, merge_symbol_contracts]  # symbol_id -> 契约卡片

    # 补丁产物（四能力③）
    patch_outline: Optional[str]
    patch_units: Annotated[list[dict], merge_patch_units]

    # 静态门控（四能力①）
    static_verdict: Optional[dict]  # StaticAuditVerdict dict
    machine_revision_count: int  # 上限 max_machine

    # 人工审核 HITL#2
    human_decision: Optional[dict]  # {approved, feedback}
    human_revision_count: int  # 上限 max_human

    # TDD 复现测试锚定（四能力④的 Ground Truth）
    anchor_round: int  # 上限 max_anchor
    anchor_decision: Optional[dict]  # {approved, feedback}
    f2p_tests: list[str]  # 冻结的复现测试节点（pytest nodeid）
    p2p_tests: list[str]  # 冻结的回归测试节点
    # 注意：F2P 测试代码必须进入 state schema（LangGraph 丢弃未声明键），
    # sandbox_execute 依赖它们把复现测试写入沙箱副本。
    f2p_test_file: str  # F2P 测试文件相对路径
    f2p_test_code: str  # F2P 测试完整代码

    # 沙箱执行（四能力③④）
    production_attempt_count: int  # 沙箱自愈轮次，上限 max_prod
    sandbox_result: Optional[dict]  # SandboxTestResult dict
    patch_path: Optional[str]  # 最终交付 fix.patch
    coverage_report_path: Optional[str]

    # 控制 / 可观测 / 成本
    production_log: Annotated[list[dict], append_list]
    errors: Annotated[list[dict], append_list]
    budget_events: Annotated[list[dict], append_list]
    budget_exceeded: bool
    status: str
    # parsing / clarifying / parsed / planning / writing / reviewing /
    # supervising / awaiting_human / anchoring / confirming_anchors /
    # executing / done / failed
    error: Optional[str]
    _revision_target: Optional[str]


def initial_state(user_input: str, job_id: str, repo_dir: str = "") -> CodeGuardState:
    """构造图的初始状态。"""
    return {
        "job_id": job_id,
        "user_input": user_input,
        "repo_dir": repo_dir,
        "messages": [],
        "clarification_qa": [],
        "clarification_round": 0,
        "issue_context": None,
        "conversation_history": [],
        "conversation_summary": None,
        "user_revision_request": None,
        "chat_mode": "briefing",
        "agent_issues": [],
        "open_issues": [],
        "director_resolve_count": 0,
        "director_resolution": None,
        "plan": None,
        "located_files": [],
        "symbol_bible": {},
        "patch_outline": None,
        "patch_units": [],
        "static_verdict": None,
        "machine_revision_count": 0,
        "human_decision": None,
        "human_revision_count": 0,
        "anchor_round": 0,
        "anchor_decision": None,
        "f2p_tests": [],
        "p2p_tests": [],
        "f2p_test_file": "",
        "f2p_test_code": "",
        "production_attempt_count": 0,
        "sandbox_result": None,
        "patch_path": None,
        "coverage_report_path": None,
        "production_log": [],
        "errors": [],
        "budget_events": [],
        "budget_exceeded": False,
        "status": "parsing",
        "error": None,
        "_revision_target": None,
    }
