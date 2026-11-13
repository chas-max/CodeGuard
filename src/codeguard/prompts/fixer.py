"""修复 Agent 提示词：生成最小 unified diff。"""
from __future__ import annotations

import json

from .shared import UNTRUSTED_NOTICE, wrap_untrusted


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
    tools_section = f"\n{tools_block}" if tools_block else ""
    return f"""你是修复 Agent。根据审查/检测/验证结果，对当前候选代码生成最小修复补丁。

{UNTRUSTED_NOTICE}

## 允许修改的文件
{json.dumps(allowed_files, ensure_ascii=False)}

## 当前 Diff
{wrap_untrusted("diff", diff_text, max_chars=30000)}

## 需要修复的问题（已按类别聚合）
{json.dumps(findings, ensure_ascii=False, indent=2)[:12000]}

同一 `class_id` 下的 `instances` 是同一类问题的全部实例：一次修完所有实例，不要只修第一处；
修好一类后在 `explanation` 里说明覆盖了哪些实例。

## Docker 验证日志
{wrap_untrusted("validation_logs", validation_logs, max_chars=8000)}

## 相关代码
{wrap_untrusted("repo_context", repo_context, max_chars=30000)}

## 轮次
第 {round_no} 轮（最多 2 轮）。
{tools_section}
## 输出要求
只输出 JSON：
{{
  "patch": "标准 unified diff，必须能 git apply --check",
  "files": ["修改的相对路径"],
  "explanation": "修复思路",
  "confidence": 0.0
}}

硬性约束：
- 只能改允许文件；不得新增/删除测试来绕过检查；
- 不得修改 CI、策略、依赖、lockfile（除非明确允许）；
- 保持最小改动；不得 force push；
- 不允许空补丁。"""
