"""下层文本创作 Agent 的提示词（Plan-Execute-Reflection 架构）。

三阶段流水线：
1. Plan（大纲）：从 brief 生成大纲，或按反馈微调大纲
2. Execute（创作）：严格按大纲写剧本
3. Reflect（自省）：检查剧本与大纲一致性，发现冲突则修订一次

合规修订（is_compliance=True）：当反馈来自机器合规审核时，合规底线高于大纲稳定性，
允许且必须做必要的大改以消除法律/暴力/色情/政治/版权风险。
"""
from __future__ import annotations

import json


def _fmt_bible(character_bible: dict) -> str:
    if not character_bible:
        return "（暂无）"
    lines = []
    for cid, card in character_bible.items():
        lines.append(
            f"- {cid} {card.get('name', '')}（{card.get('role', '')}）："
            f"外观={card.get('appearance_spec', '')}；音色={card.get('voice_id', '')}"
        )
    return "\n".join(lines)


# ---- Plan 阶段：大纲生成与微调 ----


def outline_prompt(brief: dict, character_bible: dict) -> str:
    """从 brief 生成创作大纲（首次创作）。"""
    brief_str = json.dumps(brief, ensure_ascii=False)
    return f"""你是漫剧编剧，负责创作大纲。根据 brief 与角色圣经，编写一份结构化的创作大纲。

Brief：
{brief_str}

角色圣经：
{_fmt_bible(character_bible)}

大纲要求：
1. 明确每一场的核心事件、情绪走向、角色互动。
2. 标注关键转折点和高潮位置。
3. 确保角色弧线完整（起始状态->经历->最终状态）。
4. 控制总场景数与 brief 目标时长匹配。
5. 大纲是后续剧本创作的锚点，剧本必须严格围绕大纲展开。

输出 JSON（只输出 JSON）：
{{"outline":"大纲正文（分场景描述，每场含核心事件/情绪/角色/关键台词方向）","scene_count":场景数,"key_turning_points":["转折点1","转折点2"]}}"""


def outline_revise_prompt(
    brief: dict,
    character_bible: dict,
    outline: str,
    feedback: str,
    is_compliance: bool = False,
) -> str:
    """按反馈微调大纲。

    普通修订：只允许细微修改，不得大改核心剧情线。
    合规修订（is_compliance=True）：合规底线高于大纲稳定性，允许且必须做必要的大改。
    """
    brief_str = json.dumps(brief, ensure_ascii=False)
    if is_compliance:
        constraint = """【重要：合规优先，允许大改】
- 本次修订由合规审核触发，允许且必须进行必要的较大修改，包括调整核心剧情线、角色关系、关键转折点、结局走向，以消除法律/暴力/色情/政治敏感/版权等合规风险。
- 合规底线高于大纲稳定性：若不改动核心元素就无法通过审核，则必须改动核心元素。
- 仍应围绕审核指出的问题做有针对性的调整，避免无关大改，尽量保留可保留的剧情结构。
- 审核意见必须逐条落实，不得保留审核明确反对的内容。"""
    else:
        constraint = """【重要约束】
- 只允许细微修改：措辞调整、细节补充、场景顺序微调。
- 禁止大改：不得改变核心剧情线、角色关系、结局走向、关键转折点。
- 大改会导致前后文冲突，破坏已创作内容的连贯性。
- 如修订意见要求大改，请在反馈中说明无法大改的原因，并提出折中方案。"""
    return f"""你是漫剧编剧，负责微调创作大纲。根据修订意见对大纲做修改。

Brief：
{brief_str}

角色圣经：
{_fmt_bible(character_bible)}

当前大纲：
{outline}

修订意见：
{feedback}

{constraint}

输出 JSON（只输出 JSON）：
{{"outline":"微调后大纲正文","changed":true/false,"change_summary":"修改了什么（一句话）"}}"""


# ---- Execute 阶段：按大纲创作 ----


def write_with_outline_prompt(
    brief: dict,
    character_bible: dict,
    outline: str,
    reference_materials: str | None = None,
) -> str:
    """严格按大纲写剧本。大纲是创作锚点，不可偏离。

    reference_materials: Tavily 搜索的参考资料（如有），注入创作提示词以增强真实性。
    """
    brief_str = json.dumps(brief, ensure_ascii=False)
    ref_section = ""
    if reference_materials and reference_materials.strip():
        ref_section = f"""
参考资料（Tavily 网络搜索，供创作参考）：
{reference_materials}
【注意：参考资料仅供背景参考，剧本创作以 brief 和大纲为准，不得照抄内容】
"""
    return f"""你是漫剧编剧。根据以下大纲与角色圣经，编写一集漫剧剧本正文（含场景描述、角色台词、旁白）。

【核心原则】剧本必须严格围绕大纲展开，不得偏离大纲中的核心事件、角色行为和情绪走向。

Brief：
{brief_str}

角色圣经：
{_fmt_bible(character_bible)}
{ref_section}
创作大纲（必须严格遵循）：
{outline}

要求：
1. 中文剧本，契合 brief 的基调与目标时长。
2. 分场景书写，每场含场景描述、角色台词、旁白，便于后续切分镜。
3. 严格只用角色圣经中的角色，保持人物性格与外观设定一致。
4. 每一场必须对应大纲中的对应场景，不得遗漏或自行添加大纲外的重大剧情。
5. 台词和细节可以自由发挥，但核心事件和情绪走向必须与大纲一致。
6. 若有参考资料，可借鉴相关历史文化内容丰富剧本细节，使对话和场景更符合人设和背景设定。

输出 JSON（只输出 JSON）：{{"draft":"剧本正文...","plot_summary":"一句话剧情概要"}}"""


def revise_with_outline_prompt(
    brief: dict,
    character_bible: dict,
    outline: str,
    feedback: str,
    is_compliance: bool = False,
    reference_materials: str | None = None,
) -> str:
    """按反馈修订剧本。

    普通修订：确保与大纲一致，冲突时以大纲为准。
    合规修订（is_compliance=True）：合规底线高于大纲忠实度，冲突时优先消除合规风险。
    """
    brief_str = json.dumps(brief, ensure_ascii=False)
    ref_section = ""
    if reference_materials and reference_materials.strip():
        ref_section = f"""
参考资料（Tavily 网络搜索）：
{reference_materials}
【上述资料仅供背景参考，不得照抄】
"""
    if is_compliance:
        requirement = """【重要：合规优先】
1. 本次修订由合规审核触发，优先满足合规修订要求。即使与大纲的核心事件/情绪走向冲突，也以消除合规风险为准。
2. 审核意见逐条落实，不得保留审核明确反对的台词/场景/情节。
3. 在消除合规问题的前提下，尽量维持其余剧情与角色一致性。
4. 修订后如与大纲偏离，无需说明原因（合规优先）。输出完整修订后剧本。"""
    else:
        requirement = """要求：
1. 针对意见逐条修订，保持角色一致。
2. 修订后的剧本必须仍然忠实于大纲的核心事件和情绪走向。
3. 如修订意见与大纲冲突，以大纲为准，在反馈中说明原因。
4. 输出完整修订后剧本。"""
    return f"""你是漫剧编剧。根据修订意见修改剧本。

Brief：
{brief_str}

角色圣经：
{_fmt_bible(character_bible)}
{ref_section}
创作大纲：
{outline}

修订意见：
{feedback}

{requirement}

输出 JSON（只输出 JSON）：{{"draft":"修订后剧本正文...","plot_summary":"一句话剧情概要"}}"""


# ---- Reflect 阶段：自省一致性检查 ----


def reflect_prompt(outline: str, draft: str) -> str:
    """自省检查：剧本是否忠实于大纲？是否有前后矛盾？"""
    return f"""你是漫剧编剧的自省模块。检查剧本是否忠实于创作大纲，是否存在前后矛盾。

创作大纲：
{outline}

剧本：
{draft}

检查维度：
1. 【剧情忠实度】剧本是否覆盖了大纲中的所有核心事件？是否有遗漏或自行添加的重大剧情？
2. 【情绪一致性】每场的情绪走向是否与大纲一致？
3. 【角色一致性】角色行为是否符合大纲中的角色弧线？
4. 【前后矛盾】剧本内部是否有时间线矛盾、角色行为矛盾、逻辑漏洞？

输出 JSON（只输出 JSON）：
- 全部通过：{{"passed":true,"issues":[],"revised_draft":""}}
- 有问题：{{"passed":false,"issues":["问题1: 具体描述","问题2: 具体描述"],"revised_draft":"修订后的完整剧本（如无需修订则留空）"}}"""


# ---- 兼容旧接口（供 fallback 使用）----


def write_prompt(brief: dict, character_bible: dict) -> str:
    """无大纲时的兜底创作 prompt（不推荐，应优先使用 write_with_outline_prompt）。"""
    brief_str = json.dumps(brief, ensure_ascii=False)
    return f"""你是漫剧编剧。根据 brief 与角色圣经编写一集漫剧剧本正文（含场景描述、角色台词、旁白）。

Brief：
{brief_str}

角色圣经：
{_fmt_bible(character_bible)}

要求：
1. 中文剧本，契合 brief 的基调与目标时长。
2. 分场景书写，每场含场景描述、角色台词、旁白，便于后续切分镜。
3. 严格只用角色圣经中的角色，保持人物性格与外观设定一致。

输出 JSON（只输出 JSON）：{{"draft":"剧本正文...","plot_summary":"一句话剧情梗概"}}"""


def revise_prompt(brief: dict, character_bible: dict, feedback: str) -> str:
    """无大纲时的兜底修订 prompt（不推荐，应优先使用 revise_with_outline_prompt）。"""
    brief_str = json.dumps(brief, ensure_ascii=False)
    return f"""你是漫剧编剧。根据修订意见修改剧本。

Brief：
{brief_str}

角色圣经：
{_fmt_bible(character_bible)}

修订意见：
{feedback}

要求：针对意见逐条修订，保持角色一致，输出完整修订后剧本。

输出 JSON（只输出 JSON）：{{"draft":"修订后剧本正文...","plot_summary":"一句话剧情梗概"}}"""
