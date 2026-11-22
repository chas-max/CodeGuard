"""Coder / Fixer Agent 提示词：受 FixDirective、QA 纠偏、Docker 沙箱与 RAG 程序化记忆约束。"""
from __future__ import annotations

import json

from .shared import UNTRUSTED_NOTICE, wrap_untrusted


def coder_prompt(
    *,
    fix_directive: dict,
    pr_brief: dict,
    diff_text: str,
    validation_logs: str,
    qa_feedback: dict | None,
    repo_context: str,
    round_no: int,
    max_rounds: int = 5,
    tools_block: str = "",
) -> str:
    tools_section = f"\n{tools_block}" if tools_block else ""
    qa_section = ""
    if qa_feedback and qa_feedback.get("verdict") == "revise":
        qa_section = (
            "\n## QA Checker 纠偏反馈（上一轮补丁被退回，必须优先按此修正）\n"
            + json.dumps(
                {
                    "summary": qa_feedback.get("summary", ""),
                    "drift_details": qa_feedback.get("drift_details", []),
                    "redirect_instructions": qa_feedback.get("redirect_instructions", []),
                    "item_checks": qa_feedback.get("item_checks", []),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    allowed_tests = fix_directive.get("allowed_test_addition_files") or []
    test_rule = (
        f"仅允许向授权测试文件 {json.dumps(allowed_tests, ensure_ascii=False)} 新增测试用例，"
        "严禁删除或削弱任何已有测试函数与断言。"
        if allowed_tests
        else "严禁修改、新增或删除任何测试文件。"
    )
    return f"""你是 CodeGuard 自动修复 Agent（Coder）。
你的唯一任务是严格按照 `FixDirective` 修复审查指出的阻塞问题，不得越界重构或偏离主题。

{UNTRUSTED_NOTICE}

## 修复工单 FixDirective（不可违背的主题契约）
{json.dumps(fix_directive, ensure_ascii=False, indent=2)[:12000]}
{qa_section}
## PR 上下文简报
{json.dumps(pr_brief, ensure_ascii=False, indent=2)[:4000]}

## 当前累计 Diff
{wrap_untrusted("diff", diff_text, max_chars=25000)}

## 沙箱验证/Lint 失败日志
{wrap_untrusted("validation_logs", validation_logs, max_chars=8000)}

## 关联源码上下文
{wrap_untrusted("repo_context", repo_context, max_chars=25000)}

## 当前修复轮次
第 {round_no} 轮（最多 {max_rounds} 轮）。
{tools_section}
## 工作流程建议
1. 若存在 `search_procedural_memory` 工具，先检索同类缺陷的历史修复策略与反面教训；
2. 优先使用 `edit_file` 对授权文件进行精确局部修改（或直接使用 `edits` 字段描述修改）；
3. 修改后调用 Docker 沙箱工具 `run_tests` / `run_check` 自测通过；
4. 完成后输出最终 JSON。

## 输出要求
只输出一个合法 JSON 对象：
{{
  "addressed_finding_ids": ["已修复的问题编号，如 F-001"],
  "unfixable_finding_ids": ["确实无法自动修复的编号"],
  "edits": [
    {{"path": "相对路径", "search": "精确原文片段", "replace": "替换文本"}}
  ],
  "patch": "若未直接写盘也没提供 edits，可给标准 unified diff",
  "modified_files": ["实际修改的相对路径列表"],
  "explanation": "逐条说明如何针对 FixDirective 修复问题（不得偏题）",
  "rag_memory_ids_used": ["引用的程序化记忆 ID"],
  "self_check_passed": true 或 false
}}

硬性红线：
- {test_rule}
- 禁止修改 `.github/workflows/`、`.codeguard/` 或未列入授权范围的文件；
- 禁止空改动或仅修改无关格式/注释。"""


def fixer_prompt(
    *,
    diff_text: str,
    findings: list[dict],
    validation_logs: str,
    repo_context: str,
    allowed_files: list[str],
    round_no: int,
    tools_block: str = "",
) -> str:
    """兼容旧单轮/多轮修复调用接口的提示词包装。"""
    directive = {
        "directive_id": f"legacy-{round_no}",
        "round_no": round_no,
        "needs_modification": True,
        "target_findings": findings,
        "allowed_source_files": allowed_files,
        "allowed_test_addition_files": [],
    }
    return coder_prompt(
        fix_directive=directive,
        pr_brief={},
        diff_text=diff_text,
        validation_logs=validation_logs,
        qa_feedback=None,
        repo_context=repo_context,
        round_no=round_no,
        max_rounds=5,
        tools_block=tools_block,
    )


__all__ = ["coder_prompt", "fixer_prompt"]
