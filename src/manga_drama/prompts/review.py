"""下层机器审核 Agent 的提示词。"""
from __future__ import annotations

import json


def review_prompt(draft: str, brief: dict) -> str:
    """合规审核：法律/暴力/色情/政治敏感/版权。仅底线判断，不评判艺术与价值观。"""
    taboos = json.dumps(brief.get("taboos", []) if brief else [], ensure_ascii=False)
    return f"""你是内容合规审核员。审核剧本是否触及法律、暴力、色情、政治敏感、版权等合规底线。只做合规底线判断，不评判艺术质量，不评判价值观取向。

审核维度：法律、暴力、色情、政治敏感、版权。
Brief 禁忌参考：{taboos}

剧本：
{draft}

判断原则：
- 默认倾向通过。正常的戏剧冲突、反派角色、情感张力属于可接受的艺术表达，不构成合规问题。
- 仅当明确触及上述合规底线、且严重程度达到 major 或 critical 时，才判不通过。
- minor 级别的潜在风险判通过，但在 issues 中记录供参考。

输出 JSON（只输出 JSON）：
- 通过：{{"passed":true,"issues":[],"severity":"none","category":"none","feedback":""}}
- 不通过：{{"passed":false,"issues":["具体问题1"],"severity":"major|critical","category":"legal|violence|sexual|political|copyright","feedback":"结构化：定位段落 + 问题 + 修订建议"}}"""


def review_verify_prompt(draft: str, brief: dict, initial_verdict: dict) -> str:
    """自省验证：复核初始审核结果，防止误判（过度判不通过）或漏判（有问题未发现）。"""
    taboos = json.dumps(brief.get("taboos", []) if brief else [], ensure_ascii=False)
    verdict_str = json.dumps(initial_verdict, ensure_ascii=False)
    return f"""你是内容合规审核的自省模块。复核初始审核结果，防止误判（过度判不通过）或漏判（有问题未发现）。

审核维度：法律、暴力、色情、政治敏感、版权。
Brief 禁忌参考：{taboos}

剧本：
{draft}

初始审核结果：
{verdict_str}

自省检查：
1. 如果初始结果是"不通过"：问题是否真的严重到需要打回？是否属于可接受的戏剧表达/艺术处理？是否过度判不通过？除非明确触及法律/色情/暴力/政治敏感/版权底线且达 major/critical，否则应改为通过。
2. 如果初始结果是"通过"：是否存在明确触及上述底线的合规风险被遗漏？仅当有明确触线证据时才改为不通过。
3. 反馈是否具体、可操作？

判断原则：默认倾向通过。不要因主观不适、价值观偏好或艺术风格而判不通过。除非有明确触线证据，否则应维持或改为"通过"。

输出 JSON（只输出 JSON）：
- 确认初始结果：{{"confirmed":true,"revised_passed":null,"revised_feedback":""}}
- 推翻/修正：{{"confirmed":false,"revised_passed":true/false,"revised_feedback":"修正后的反馈"}}"""
