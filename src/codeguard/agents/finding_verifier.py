"""blocking finding 的独立复核：两个不同 provider 各投一票，只降不升。

为什么是 2 票而不是投票集群：
- 同源模型多次采样误差高度相关，票数增加带来的独立信息很少；
- 复核只对「已经会阻塞合并的语义 finding」触发，数量级是个位数；
- 复核只能把 blocking 降级，不能把非 blocking 提升，因此不会扩大误报面。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..engine.findings import finding_key
from ..prompts.finding_verifier import finding_verifier_prompt
from ..prompts.tools import tool_protocol_block
from ..tools.spec import ToolBox, ToolLimits
from .base import call_json_with_tools

VERIFIER_ROLES = ("finding_verifier_a", "finding_verifier_b")


class _Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    verdict: Literal["keep", "drop"] = "keep"
    reason: str = ""
    counter_evidence: str = Field(default="")


def _demote(item: dict, *, by: str, reason: str) -> dict:
    note = f"[复核降级:{by}] {reason}".strip()
    suggestion = str(item.get("suggestion", ""))
    return {
        **item,
        "blocking": False,
        "demoted_by": by,
        "suggestion": (note + ("；" + suggestion if suggestion else ""))[:1000],
    }


def verify_findings(
    ctx,
    findings: list[dict] | None,
    *,
    repo_context: str,
    diff_text: str,
    max_items: int = 5,
    tools: ToolBox | None = None,
    limits: ToolLimits | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """返回 `(findings, costs, issues, tool_events)`；复核不可用时保持 blocking 并记录 issue。"""
    limits = limits or ToolLimits()
    tools_block = tool_protocol_block(tools.specs(), limits) if tools else ""
    items = [dict(item) for item in (findings or [])]
    targets = [
        item for item in items
        if str(item.get("source", "")) == "semantic"
        and item.get("blocking")
        and str(item.get("lifecycle") or "open") == "open"
    ][: max(0, max_items)]
    if not targets:
        return items, [], [], []

    costs: list[dict] = []
    issues: list[dict] = []
    tool_events: list[dict] = []
    patches: dict[str, dict] = {}

    for item in targets:
        prompt = finding_verifier_prompt(
            finding=item, repo_context=repo_context, diff_text=diff_text,
            tools_block=tools_block,
        )
        votes: list[_Verdict] = []
        failed = False
        for role in VERIFIER_ROLES:
            try:
                verdict, cost, events = call_json_with_tools(
                    ctx, role, prompt, _Verdict,
                    tools=tools, limits=limits, temperature=0.0,
                )
            except Exception as exc:  # noqa: BLE001 - 复核不可用不能拖垮检测
                failed = True
                issues.append({
                    "agent": "finding_verifier", "issue_type": "llm_error",
                    "detail": f"{role}: {exc}", "finding_id": finding_key(item),
                })
                continue
            costs.extend(cost)
            tool_events.extend(events)
            votes.append(verdict)

        if failed or len(votes) < len(VERIFIER_ROLES):
            # 复核不可用 → 保持 blocking（宁可让人看一眼，也不静默放行）
            continue

        drops = [vote for vote in votes if vote.verdict == "drop"]
        if len(drops) == len(votes):
            patches[finding_key(item)] = _demote(
                item, by="finding_verifier", reason=drops[0].reason or "代码事实不符",
            )
        elif drops:
            patches[finding_key(item)] = _demote(
                item, by="verifier_tie",
                reason="复核意见不一致：" + "；".join(vote.reason for vote in votes if vote.reason)[:500],
            )

    if not patches:
        return items, costs, issues, tool_events
    merged = [patches.get(finding_key(item), item) for item in items]
    return merged, costs, issues, tool_events


__all__ = ["VERIFIER_ROLES", "verify_findings"]
