"""Node 7 (summary) 提示词：生成面向人工审核的证据摘要与 PR 更新说明。"""
from __future__ import annotations

import json

from .shared import UNTRUSTED_NOTICE


def summary_prompt(
    *,
    pr_brief: dict,
    findings: list[dict],
    fix_history: list[dict],
    qa_history: list[dict],
    validation_report: dict | None,
    inherited_level: str,
    effective_level: str,
    escalation_reason: str,
) -> str:
    return f"""你是 CodeGuard 审查总结与递交 Agent（Node 7）。
请基于真实检测结论、Coder 修复记录、QA 核查结果与 Docker 测试验证证据，生成结构化摘要，方便人工或自动门控快速决策。

{UNTRUSTED_NOTICE}

## PR 背景简报
{json.dumps(pr_brief, ensure_ascii=False, indent=2)[:4000]}

## 风险继承信息（只升不降）
- inherited_level: {inherited_level}
- effective_level: {effective_level}
- escalation_reason: {escalation_reason or "无"}

## 问题全景快照
{json.dumps(findings[:30], ensure_ascii=False, indent=2)[:8000]}

## 修复历史
{json.dumps(fix_history[-5:], ensure_ascii=False, indent=2)[:4000]}

## QA Checker 监督历史
{json.dumps(qa_history[-5:], ensure_ascii=False, indent=2)[:4000]}

## Docker 验证证据
{json.dumps(validation_report or {}, ensure_ascii=False, indent=2)[:4000]}

## 输出要求
只输出一个合法 JSON 对象：
{{
  "headline": "PR 更新与审查结论一句话标题",
  "changes_made": ["本次自动修复或代码变更的关键要点"],
  "verification_evidence": ["Lint/语法/Docker测试/QA核查通过的客观证据"],
  "residual_risks": ["仍需人工关注的风险或未修复阻塞项"],
  "markdown_body": "结构清晰、包含等级继承说明与证据清单的 Markdown 评论正文"
}}

红线：不得编造未出现在上述输入中的测试通过结论，也不得尝试降低重要性等级。"""


__all__ = ["summary_prompt"]
