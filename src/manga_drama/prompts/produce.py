"""下层漫剧制作 Agent 的提示词（ReAct per-shot 架构）。

逐镜 Think-Act-Observe-Decide 循环：
1. Think: 分析 shot 需求，检查上一镜结果，决定生成策略
2. Act:   调用 T2I 生成图片 + TTS 生成音频（外部工具，非 LLM）
3. Observe: 检查生成结果（文件是否存在、大小是否合理）
4. Decide: retry(修改prompt重试) / continue(继续下一镜) / skip(跳过)
"""
from __future__ import annotations

import json


def shot_think_prompt(shot: dict, prev_result: dict | None, attempt: int) -> str:
    """Think 阶段：分析 shot 需求，决定生成策略。"""
    shot_str = json.dumps(shot, ensure_ascii=False)
    prev_str = json.dumps(prev_result, ensure_ascii=False) if prev_result else "无（首个分镜）"
    return f"""你是漫剧制作的分镜执行模块。分析当前分镜的生成需求，决定最佳策略。

当前分镜：
{shot_str}

上一镜生成结果：
{prev_str}

当前重试次数：{attempt}

分析要求：
1. 检查分镜描述是否清晰、是否需要调整 prompt 策略
2. 如有上一镜结果，分析其成功/失败原因，为当前镜提供参考
3. 如是重试，分析上次失败原因，提出改进方向

输出 JSON（只输出 JSON）：
{{"strategy":"normal|retry_adjust|skip","reason":"策略原因","prompt_adjustments":"如需调整prompt，给出调整方向（如：更强调某元素、弱化某元素、调整构图等）"}}"""


def shot_retry_prompt(shot: dict, prev_error: str, attempt: int) -> str:
    """Retry 阶段：分析失败原因，修改 prompt 策略。"""
    shot_str = json.dumps(shot, ensure_ascii=False)
    return f"""你是漫剧制作的分镜重试模块。上次生成失败，请分析原因并提出改进策略。

当前分镜：
{shot_str}

上次失败信息：
{prev_error}

当前重试次数：{attempt}（最多重试 2 次）

分析要求：
1. 判断失败类型：内容审核(prompt被拒) / 技术问题(API错误) / 质量问题(生成结果不佳)
2. 针对失败类型提出具体改进：
   - 内容审核：替换敏感词、调整描述方式
   - 技术问题：调整参数、简化 prompt
   - 质量问题：增加细节描述、调整风格关键词

输出 JSON（只输出 JSON）：
{{"action":"retry|skip","reason":"决策原因","new_strategy":"如重试，给出新的prompt策略方向"}}"""
