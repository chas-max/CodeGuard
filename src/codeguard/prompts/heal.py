"""TDD 复现测试锚定与沙箱 ReAct 自愈提示词。"""
from __future__ import annotations

import json


def f2p_test_prompt(issue_context: dict, file_code: str, existing_test_style: str) -> str:
    """build_anchors：生成专门复现该 Bug 的 Fail-to-Pass 测试。"""
    return f"""你是 TDD 教练。请编写一个专门复现该 Bug 的失败测试（Fail-to-Pass）。

## 故障上下文
{json.dumps(issue_context, ensure_ascii=False, indent=2)}

## 被测文件当前代码
```python
{file_code}
```

## 仓库既有测试风格参考
{existing_test_style or "（无既有测试，使用 pytest 函数式风格 + 直接 import 被测模块）"}

## 输出要求
只输出一个 JSON 对象：
{{
  "test_file": "新测试文件相对路径（如 test_repro_issue.py）",
  "test_code": "完整测试文件代码（pytest 函数式； import 必须能在仓库根目录直接运行）",
  "node_ids": ["该文件中的 pytest 节点ID，如 test_repro_issue.py::test_bug_repro"]
}}

要求：
1. 测试必须表达"修复后的正确行为"（断言期望值，而非断言报错）。
2. 当前未修复的代码运行该测试必须失败（这是 Red 锚定的意义）。
3. 只测这个 Bug，不要掺杂无关断言。"""


def react_think_prompt(
    unit: dict,
    traceback_text: str,
    current_code: str,
) -> str:
    """sandbox_execute 的 Think 阶段：分析裁剪后的 Traceback，定向重写该槽位补丁。

    同构于原漫剧 _react_single_shot 的 Think 步骤：Observe（测试失败/Traceback）
    → Think（LLM 分析根因并重写）→ Act（下轮沙箱执行）。
    """
    return f"""你是调试专家。沙箱中该文件的补丁执行失败，请分析精简堆栈并重写修复代码。

## 槽位信息
{json.dumps({k: unit.get(k, "") for k in ("unit_id", "target_file", "issue_hypothesis", "attempts")}, ensure_ascii=False)}

## 失败堆栈（已裁剪）
```
{traceback_text}
```

## 当前（失败）的文件代码
```python
{current_code}
```

## 输出要求
只输出一个 JSON 对象：
{{
  "analysis": "失败根因分析（针对堆栈，一句话）",
  "patched_code": "重写后的完整文件代码",
  "should_skip": false —— 若判断该问题无法通过改代码解决（如环境缺失）则为 true
}}

红线：保持公共函数签名不变；禁止 eval/exec/os.system/shutil.rmtree。"""
