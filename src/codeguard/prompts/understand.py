"""故障理解 Agent 提示词：现象解析、信息完备度评估、修改意图分类、历史摘要。"""
from __future__ import annotations

import json


def parse_issue_prompt(issue_text: str, repo_file_tree: list[str]) -> str:
    """故障现象解析：从 issue 文本提取结构化上下文与完备度评估。"""
    tree = "\n".join(repo_file_tree[:80]) or "（空仓库）"
    return f"""你是资深 SRE 与故障诊断专家。请解析下方 Bug 报告，提取结构化故障上下文。

## 仓库文件列表
{tree}

## Bug 报告原文
{issue_text}

## 输出要求
只输出一个 JSON 对象（不要 markdown 围栏），字段如下：
{{
  "title": "问题一句话标题",
  "symptom": "症状描述（什么行为错误、期望 vs 实际）",
  "stack_trace": "报错堆栈原文（若报告中有；没有则空字符串）",
  "suspected_modules": ["疑似受影响的模块或函数名"],
  "repro_steps": ["复现步骤，逐条"],
  "env_hints": "环境线索（Python版本/依赖/OS），没有则空字符串",
  "missing_info": ["定位与修复必需但报告中缺失的信息，逐条；若信息充分则为空数组"],
  "completeness": "clear | partial | unclear 三选一：clear=可定位修复；partial=有线索但缺关键细节；unclear=无法理解问题"
}}

评估标准：stack_trace 或 repro_steps 或 symptom 中至少一项足够具体到可写出复现测试，才算 clear。"""


def revision_classify_prompt(message: str) -> str:
    """用户在澄清阶段补充消息的意图分类。"""
    return f"""用户在 Bug 修复任务的澄清对话中发来一条消息。请分类其意图。

## 消息
{message}

## 输出要求
只输出一个 JSON 对象：
{{
  "intent": "supplement | revise_scope | chat",
  "detail": "补充/修改的内容摘要"
}}

intent 说明：supplement=补充故障信息（回答之前的追问）；revise_scope=修改任务范围或目标；chat=无关闲聊。"""


def summarize_prompt(history_text: str) -> str:
    """对话历史摘要压缩。"""
    return f"""请将以下故障澄清对话历史压缩为一段简洁摘要，保留：Bug 关键信息、已确认的复现条件、用户约束。

## 对话历史
{history_text}

直接输出摘要文本（不要 JSON、不要围栏），不超过 300 字。"""
