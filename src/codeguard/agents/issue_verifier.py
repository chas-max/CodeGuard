"""问题验证 Agent：claim 提取。"""
from __future__ import annotations

from ..models import IssueClaim
from ..prompts.issue_verifier import issue_claim_prompt
from ..prompts.tools import tool_protocol_block
from ..tools.spec import ToolBox, ToolLimits
from .base import call_json_with_tools


def extract_claim(
    ctx,
    *,
    pr_title: str,
    pr_body: str,
    linked_issue: str,
    repo_files: list[str],
    diff_text: str,
    profile: str = "bugfix",
    tools: ToolBox | None = None,
    limits: ToolLimits | None = None,
) -> tuple[IssueClaim, list[dict], list[dict]]:
    limits = limits or ToolLimits()
    tools_block = tool_protocol_block(tools.specs(), limits) if tools else ""
    prompt = issue_claim_prompt(
        pr_title=pr_title,
        pr_body=pr_body,
        linked_issue=linked_issue,
        repo_files=repo_files,
        diff_text=diff_text,
        profile=profile,
        tools_block=tools_block,
    )
    return call_json_with_tools(
        ctx, "issue_verifier", prompt, IssueClaim,
        tools=tools, limits=limits, temperature=0.1,
    )


__all__ = ["extract_claim"]
