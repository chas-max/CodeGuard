"""中层导演 Agent 的提示词：规划+角色圣经、内容监督。"""
from __future__ import annotations

import json


def plan_prompt(brief: dict) -> str:
    """根据 brief 产出制作计划 + 角色圣经（含 FROZEN 外观规格）。"""
    brief_str = json.dumps(brief, ensure_ascii=False)
    return f"""你是漫剧导演。根据创作 brief 制定制作计划与角色圣经（Character Bible）。

Brief：
{brief_str}

要求：
1. plan：场景数、总时长(秒)、节奏、分镜风格。
2. characters：为每个角色生成 CharacterCard。核心是 appearance_spec（FROZEN 外观规格：年龄/脸型/眉眼/发型/发色/瞳色/服饰/配色/标志特征/画风，中英双语，将逐字注入每张分镜图以保持角色跨镜一致）、base_seed（0 到 2147483647 的整数）、voice_id（TTS 音色，如 longxiaochun 女声、longwan、longcheng 男声）、current_costume。
3. char_id 用 C001/C002...，与 brief 中的角色对应；若 brief 角色无名请合理命名。

输出 JSON（只输出 JSON）：{{"plan":{{"scene_count":8,"total_duration_sec":60,"pace":"节奏描述","style_prefix":"分镜画风前缀"}},"characters":[{{"char_id":"C001","name":"","role":"主角","appearance_spec":"","base_seed":12345,"voice_id":"longxiaochun","current_costume":"初始服装"}}]}}"""


def supervise_prompt(draft: str, brief: dict) -> str:
    """机器审核通过后的内容监督：连贯/人物/基调/艺术质量（不评判合规底线）。"""
    brief_str = json.dumps(brief, ensure_ascii=False)
    return f"""你是漫剧导演，做内容监督。你不评判合规底线（那是审核 Agent 的职责），你评判：剧情连贯性、人物一致性、基调是否契合 brief、艺术质量。

Brief：
{brief_str}

剧本草稿：
{draft}

输出 JSON（只输出 JSON）：
- 通过：{{"decision":"approve","feedback":""}}
- 需修订：{{"decision":"revise","feedback":"结构化修订意见：定位到具体段落/场景 + 具体问题 + 修订方向"}}"""


def supervise_verify_prompt(draft: str, brief: dict, initial_judgment: dict) -> str:
    """自省验证：复核初始监督判断是否公平、是否遗漏问题。"""
    brief_str = json.dumps(brief, ensure_ascii=False)
    judgment_str = json.dumps(initial_judgment, ensure_ascii=False)
    return f"""你是漫剧导演的自省模块。复核对剧本的初始监督判断是否公平、是否遗漏重要问题。

Brief：
{brief_str}

剧本草稿：
{draft}

初始判断：
{judgment_str}

自省检查：
1. 初始判断是否过于宽松？是否有明显问题被遗漏？
2. 初始判断是否过于严苛？是否对不影响整体质量的小问题过度反应？
3. 修订意见（如有）是否具体、可操作？是否有更好的修订方向？

输出 JSON（只输出 JSON）：
- 确认初始判断：{{"confirmed":true,"revised_decision":"","revised_feedback":""}}
- 推翻/修正：{{"confirmed":false,"revised_decision":"approve|revise","revised_feedback":"修正后的反馈"}}"""
