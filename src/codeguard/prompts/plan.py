"""架构规划 Agent 提示词：基于 AST 定位结果产出修复策略与槽位初始化。"""
from __future__ import annotations

import json


def plan_prompt(
    issue_context: dict,
    located_files: list[str],
    file_excerpts: str,
    symbol_digest: str,
    existing_tests: list[str],
) -> str:
    """director_plan：为每个疑似故障文件给出根因假设与修复策略。"""
    ctx = json.dumps(issue_context, ensure_ascii=False, indent=2)
    tests = "\n".join(existing_tests) or "（仓库无既有测试）"
    return f"""你是 Tech Lead。基于 AST 调用图定位结果与故障上下文，制定多文件修复方案。

## 故障上下文
{ctx}

## 定位到的疑似故障文件（Top-K）
{json.dumps(located_files, ensure_ascii=False)}

## 相关代码摘录
{file_excerpts}

## 冻结的符号契约（公共接口签名，修复时严禁破坏）
{symbol_digest}

## 仓库既有测试（将作为 P2P 回归门控）
{tests}

## 输出要求
只输出一个 JSON 对象：
{{
  "plan_summary": "修复思路概述（根因 → 策略）",
  "repair_strategy": [
    {{
      "target_file": "相对路径（必须来自上方定位文件列表）",
      "test_file": "该文件对应的测试文件路径（既有测试文件，或新建 test_xxx.py 相对路径）",
      "hypothesis": "该文件的故障根因假设",
      "change_sketch": "修复要点（不写完整代码，只写改动思路与边界条件）"
    }}
  ]
}}

注意：
1. repair_strategy 只覆盖真正需要修改的文件（通常 1-2 个），不要为了凑数修改无关文件。
2. 必须保持所有冻结符号的参数签名不变。"""
