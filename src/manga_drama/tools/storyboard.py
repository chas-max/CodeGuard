"""分镜工具：用 LLM 把剧本切分为 Shot[]。

writer 产出剧本正文；本模块调 LLM（默认 produce 角色 qwen-plus）把剧本切为结构化
分镜列表，含 scene_desc / dialogue / narration / characters_in_shot / camera /
emotion / duration_sec。返回 list[dict]（未含 image_prompt，由 consistency.build_image_prompt 纯函数生成）。
"""
from __future__ import annotations

from typing import Any

from ..llm import parse_json_loose, track_usage
from ..retry import with_retry
from ..errors import NonRetryableError, RetryableError

_STORYBOARD_PROMPT = """你是漫剧分镜师。把下面的剧本切分为分镜列表，用于制作竖屏漫剧短视频。
要求：
1. 每个分镜是一个独立画面，时长 2-6 秒，整集总时长符合目标。
2. 严格只使用角色名单中的 char_id（不可臆造新角色）。
3. 输出 JSON，结构：{"shots": [{"index":1,"scene_desc":"画面描述","dialogue":"角色台词(可空)","narration":"旁白(可空)","characters_in_shot":["C001"],"camera":"镜头(近景/中景/远景/特写)","emotion":"情绪","duration_sec":3.0}]}。
4. 只输出 JSON，不要解释。

角色名单：
<<ROSTER>>

剧本：
<<DRAFT>>
"""


def _format_roster(character_bible: dict) -> str:
    if not character_bible:
        return "（暂无，按剧本需要设定 char_id 如 C001）"
    lines = []
    for cid, card in character_bible.items():
        lines.append(f"- {cid}：{card.get('name', '')}（{card.get('role', '')}）")
    return "\n".join(lines)


def generate_storyboard(
    draft: str,
    character_bible: dict,
    llm: Any,
    messages_builder: Any = None,
) -> tuple[list[dict], list[dict]]:
    """调用 LLM 切分分镜。

    Args:
        draft: 剧本正文。
        character_bible: char_id -> CharacterCard。
        llm: ChatOpenAI 实例。
        messages_builder: 可选，自定义消息构造器（测试注入）。

    Returns:
        (shots, cost_events)：分镜列表（dict）、成本事件列表（dict）。
    """
    prompt = _STORYBOARD_PROMPT.replace("<<ROSTER>>", _format_roster(character_bible)).replace("<<DRAFT>>", draft)
    if messages_builder is None:
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content=prompt)]
    else:
        messages = messages_builder(prompt)

    @with_retry()
    def _do():
        try:
            return llm.invoke(messages)
        except Exception as e:
            raise RetryableError(f"分镜 LLM 调用失败: {e}") from e

    resp = _do()
    costs: list[dict] = []
    ev = track_usage(resp, "produce")
    if ev:
        costs.append(ev.to_dict())

    content = getattr(resp, "content", "") or ""
    try:
        obj = parse_json_loose(content)
    except NonRetryableError:
        return [], costs  # 解析失败，交上层处理
    shots = obj.get("shots", []) if isinstance(obj, dict) else []
    if not isinstance(shots, list):
        return [], costs
    # 规整字段
    norm = []
    for i, s in enumerate(shots, 1):
        if not isinstance(s, dict):
            continue
        s.setdefault("shot_id", f"S{i:03d}")
        s.setdefault("index", i)
        s.setdefault("status", "pending")
        s.setdefault("image_attempts", 0)
        s.setdefault("audio_attempts", 0)
        norm.append(s)
    return norm, costs
