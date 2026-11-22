"""Node 6 (qa_checker) Agent：防偏航监督与 FixDirective 对齐校验。"""
from __future__ import annotations

from typing import Any

from ..engine.diff import patch_paths
from ..models import FixDirective, QACheckReport, QAItemCheck
from ..prompts.qa_checker import qa_checker_prompt
from ..prompts.tools import tool_protocol_block
from ..tools.spec import ToolBox, ToolLimits
from .base import call_json_with_tools


def run_qa_checker(
    ctx: Any,
    *,
    fix_directive: FixDirective | dict[str, Any],
    fix_result: dict[str, Any],
    actual_patch: str,
    round_no: int,
    max_fix_rounds: int,
    tools: ToolBox | None = None,
    limits: ToolLimits | None = None,
    tier: str = "T1",
) -> tuple[QACheckReport, list[dict], list[dict]]:
    """对照 FixDirective 与工作区真实 Diff 判定是否一致、是否偏航。"""
    directive = (
        fix_directive
        if isinstance(fix_directive, FixDirective)
        else FixDirective.model_validate(fix_directive)
    )
    limits = limits or ToolLimits()
    tools_block = tool_protocol_block(tools.specs(), limits) if tools else ""

    # 确定性前置护栏：先检查空补丁与未授权路径越界
    det_drifts: list[str] = []
    if directive.needs_modification and not (actual_patch or "").strip():
        det_drifts.append("FixDirective 要求修改代码，但真实工作区 Diff 为空。")
    try:
        touched_paths = patch_paths(actual_patch) if (actual_patch or "").strip() else []
    except Exception as exc:  # noqa: BLE001
        touched_paths = []
        det_drifts.append(f"补丁路径解析失败: {exc}")

    allowed = set(directive.allowed_source_files) | set(directive.allowed_test_addition_files)
    if allowed:
        for path in touched_paths:
            if path not in allowed:
                det_drifts.append(f"补丁修改了 FixDirective 未授权的文件: {path}")

    prompt = qa_checker_prompt(
        fix_directive=directive.model_dump(mode="json"),
        fix_result=fix_result,
        actual_patch=actual_patch,
        round_no=round_no,
        max_fix_rounds=max_fix_rounds,
        tools_block=tools_block,
    )
    report, costs, events = call_json_with_tools(
        ctx,
        "qa_checker",
        prompt,
        QACheckReport,
        tools=tools,
        limits=limits,
        temperature=0.0,
        tier=tier,
    )

    if det_drifts:
        merged_drifts = list(dict.fromkeys(det_drifts + list(report.drift_details)))
        merged_redirects = list(dict.fromkeys(
            [f"修正以下硬性问题: {d}" for d in det_drifts] + list(report.redirect_instructions)
        ))
        next_verdict = "escalate" if round_no >= max_fix_rounds else "revise"
        report = report.model_copy(update={
            "verdict": next_verdict,
            "is_aligned": False,
            "has_scope_drift": True,
            "alignment_score": min(report.alignment_score, 0.3),
            "drift_details": merged_drifts,
            "redirect_instructions": merged_redirects,
            "summary": report.summary or "；".join(det_drifts),
        })
    elif report.verdict == "revise" and round_no >= max_fix_rounds:
        report = report.model_copy(update={
            "verdict": "escalate",
            "summary": (report.summary + "（已达到最大修复轮次上限，升级人工处理）").strip(),
        })

    if not report.item_checks and directive.target_findings:
        checks = [
            QAItemCheck(
                finding_id=str(item.get("directive_item_id") or item.get("id") or f"F-{idx:03d}"),
                addressed=(report.verdict == "pass"),
                aligned=report.is_aligned,
            )
            for idx, item in enumerate(directive.target_findings, start=1)
        ]
        report = report.model_copy(update={"item_checks": checks})

    return report, costs, events


__all__ = ["run_qa_checker"]
