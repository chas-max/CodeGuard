"""问题验证 Agent：从 PR/issue 提取可验证的 bug claim（不做意图分类）。"""
from __future__ import annotations

from .shared import UNTRUSTED_NOTICE, wrap_untrusted


def issue_claim_prompt(
    *,
    pr_title: str,
    pr_body: str,
    linked_issue: str,
    repo_files: list[str],
    diff_text: str,
    profile: str = "bugfix",
    tools_block: str = "",
) -> str:
    tree = "\n".join(repo_files[:120]) or "（空）"
    tools_section = f"\n{tools_block}" if tools_block else ""
    if profile == "change":
        goal = (
            "你是代码审查系统中的变更意图解析器。任务不是猜测作者意图，而是提取 PR 声称要解决的问题、"
            "目标行为与验收证据，供审查 Agent 判断变更是否解决它自己的问题。"
        )
    else:
        goal = "你是代码审查系统中的问题验证器。任务不是猜测作者意图，而是把 PR 声称的 bug 提取成可验证 claim。"
    return f"""{goal}

{UNTRUSTED_NOTICE}

## 仓库文件（节选）
{tree}

## PR 标题
{wrap_untrusted("pr_title", pr_title, max_chars=1000)}

## PR 正文
{wrap_untrusted("pr_body", pr_body, max_chars=8000)}

## 关联 Issue
{wrap_untrusted("linked_issue", linked_issue, max_chars=8000)}

## PR Diff
{wrap_untrusted("diff", diff_text, max_chars=30000)}
{tools_section}
## 输出要求
只输出一个 JSON 对象：
{{
  "kind": "bugfix | feature | refactor | docs | chore | unknown",
  "statement": "一句话描述要解决的问题（bug 或变更目标）",
  "expected": "期望行为 / 验收标准",
  "actual": "实际行为（bugfix）或变更前状态",
  "repro_steps": ["可执行/可写成测试的复现或验收步骤"],
  "affected_paths": ["仓库相对路径"],
  "linked_issue": 123 或 null,
  "confidence": 0.0
}}

只有当 PR 确实在描述一个可复现 bug 时才用 bugfix；功能/重构/文档用对应 kind。
没有给出任何验收证据时把 confidence 调低，不要编造。
不要输出 markdown 围栏。"""
