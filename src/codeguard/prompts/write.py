"""补丁编写 Agent 提示词：Search-Plan-Execute-Reflect 四阶段。"""
from __future__ import annotations

import json


def outline_prompt(issue_context: dict, repair_strategy: list[dict]) -> str:
    """阶段2 Plan：把修复策略细化为执行大纲。"""
    return f"""你是资深 Python 工程师。基于故障上下文与 Tech Lead 的修复策略，编写补丁执行大纲。

## 故障上下文
{json.dumps(issue_context, ensure_ascii=False, indent=2)}

## 修复策略
{json.dumps(repair_strategy, ensure_ascii=False, indent=2)}

## 输出要求
只输出一个 JSON 对象：
{{
  "outline": "分步骤执行大纲文本：每个文件先改什么、后改什么、如何兼顾边界条件与调用方兼容"
}}"""


def patch_code_prompt(
    file_code: str,
    hypothesis: str,
    change_sketch: str,
    frozen_contracts: str,
    issue_context: dict,
) -> str:
    """阶段3 Execute：为单个文件生成修复后的完整代码。"""
    return f"""你是资深 Python 工程师。请修复下方文件中的 Bug，输出修复后的完整文件代码。

## 故障上下文
{json.dumps(issue_context, ensure_ascii=False)}

## 该文件的根因假设
{hypothesis}

## 修复要点
{change_sketch}

## 冻结的公共接口契约（参数签名与名称严禁改变）
{frozen_contracts}

## 文件当前完整代码
```python
{file_code}
```

## 输出要求
只输出一个 JSON 对象：
{{
  "patched_code": "修复后的完整文件代码（整个文件，不是 diff 片段）",
  "change_note": "本次改动说明（一句话）"
}}

红线：
1. 保持所有公共函数/类的签名（函数名、参数名、参数顺序）与原文件一致。
2. 只做必要的最小修复，不要重构无关代码。
3. 禁止使用 eval/exec/os.system/shutil.rmtree。"""


def reflect_prompt(
    patched_files: dict,
    symbol_bible_digest: str,
    issue_context: dict,
) -> str:
    """阶段4 Reflect：跨文件契约一致性自省。"""
    files_text = "\n\n".join(f"=== {p} ===\n{code}" for p, code in patched_files.items())
    return f"""你是代码审查员。请自省以下修复补丁是否满足契约与根因要求。

## 故障上下文
{json.dumps(issue_context, ensure_ascii=False)}

## 冻结的符号契约
{symbol_bible_digest}

## 修复后的补丁
{files_text}

## 输出要求
只输出一个 JSON 对象：
{{
  "consistent": true/false —— 是否所有冻结签名保持一致、根因被正确修复且无越界改动
  "issues": ["发现的问题，逐条；无问题则空数组"]
}}"""
