"""修复 Agent：单轮生成 patch，或在沙箱副本内多轮"改码 → 跑测试 → 再改"。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..models import FixProposal
from ..prompts.fixer import fixer_prompt
from ..prompts.tools import tool_protocol_block
from ..tools.spec import ToolBox, ToolLimits
from .base import call_json, call_json_with_tools


class _FixPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    patch: str = ""
    files: list[str] = Field(default_factory=list)
    explanation: str = ""
    confidence: float = 0.0


def _proposal(payload: _FixPayload, round_no: int) -> FixProposal:
    return FixProposal(
        patch=payload.patch,
        files=payload.files,
        explanation=payload.explanation,
        round=round_no,
        applies_cleanly=False,
    )


def propose_fix(
    ctx,
    *,
    diff_text: str,
    findings: list[dict],
    validation_logs: str,
    repo_context: str,
    allowed_files: list[str],
    round_no: int,
) -> tuple[FixProposal, list[dict]]:
    """单轮修复（回退路径）：只产出 unified diff，由节点校验并应用。"""
    prompt = fixer_prompt(
        diff_text=diff_text,
        findings=findings,
        validation_logs=validation_logs,
        repo_context=repo_context,
        allowed_files=allowed_files,
        round_no=round_no,
    )
    payload, costs = call_json(ctx, "fixer", prompt, _FixPayload, temperature=0.1)
    return _proposal(payload, round_no), costs


def propose_fix_with_tools(
    ctx,
    *,
    diff_text: str,
    findings: list[dict],
    validation_logs: str,
    repo_context: str,
    allowed_files: list[str],
    round_no: int,
    tools: ToolBox | None = None,
    limits: ToolLimits | None = None,
) -> tuple[FixProposal, list[dict], list[dict]]:
    """多轮修复：可在 fix_root 上 apply_patch/write_file 并 run_tests 验证。

    最终仍返回 FixProposal；若 Agent 只改了文件没给 patch，节点会用 git diff
    生成补丁，再走既有 validate_fix_patch 与合并门控。
    """
    limits = limits or ToolLimits()
    tools_block = tool_protocol_block(tools.specs(), limits) if tools else ""
    prompt = fixer_prompt(
        diff_text=diff_text,
        findings=findings,
        validation_logs=validation_logs,
        repo_context=repo_context,
        allowed_files=allowed_files,
        round_no=round_no,
        tools_block=tools_block,
    )
    payload, costs, events = call_json_with_tools(
        ctx, "fixer", prompt, _FixPayload,
        tools=tools, limits=limits, temperature=0.1,
    )
    return _proposal(payload, round_no), costs, events


__all__ = ["propose_fix", "propose_fix_with_tools"]
