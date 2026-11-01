"""Tech Lead 监督 Agent 提示词：两阶段 Reflection（初始判断 + 自省复核）。"""
from __future__ import annotations

import json


def supervise_prompt(diff_text: str, issue_context: dict) -> str:
    """阶段1：审查补丁 diff 的边界条件与潜在风险。"""
    return f"""你是 Tech Lead，请审查以下 Bug 修复补丁（Unified Diff），重点检查边界条件与隐性风险。

## 故障上下文
{json.dumps(issue_context, ensure_ascii=False, indent=2)}

## 补丁 Diff
```diff
{diff_text or "（空补丁）"}
```

## 输出要求
只输出一个 JSON 对象：
{{
  "decision": "approve | revise",
  "feedback": "审查意见（具体到文件与代码行为）",
  "boundary_risks": ["边界条件风险，逐条"]
}}

审查维度：边界输入（0/负数/空值/极大值）、异常处理完整性、对既有调用方的兼容性、遗漏的同类问题点。"""


def supervise_verify_prompt(diff_text: str, issue_context: dict, initial_judgment: dict) -> str:
    """阶段2：自省复核初始判断是否公平、是否遗漏或夸大问题。"""
    return f"""你是 Tech Lead 的复核人。请复核以下审查结论是否公平准确。

## 故障上下文
{json.dumps(issue_context, ensure_ascii=False, indent=2)}

## 补丁 Diff
```diff
{diff_text or "（空补丁）"}
```

## 初始审查结论
{json.dumps(initial_judgment, ensure_ascii=False)}

## 输出要求
只输出一个 JSON 对象：
{{
  "confirmed": true/false —— 是否认同初始结论
  "revised_decision": "若不认同，给出修正后的 approve | revise（认同则与初始一致）",
  "revised_feedback": "若不认同，给出修正后的意见（认同则可复述）",
  "reason": "复核理由（一句话）"
}}"""
