"""跨分镜角色一致性。

核心是 prompt_builder 纯函数（确定性、可测）：把每角色冻结 AppearanceSpec verbatim
注入 image prompt，同角色描述词每镜完全一致，消除文本漂移。视觉一致性校验
（face embedding 相似度）为可选重依赖，insightface 未装时返回 None 即降级为
「seed + 文本锚定 + 人工抽检」（由 confirm_anchors 闸门兜底）。
"""
from __future__ import annotations

from typing import Optional


def panel_seed(base_seed: int, panel_index: int) -> int:
    """同一角色的分镜种子：base_seed XOR panel_index，跨镜噪声相关。"""
    return (int(base_seed) ^ int(panel_index)) & 0x7FFFFFFF


def build_image_prompt(
    shot: dict,
    character_bible: dict,
    style_prefix: str,
    negative_prompt_global: str,
    prev_shot_summary: Optional[str] = None,
) -> tuple[str, str, int]:
    """为单镜构造 (image_prompt, negative_prompt, seed)。

    把出场角色的冻结 appearance_spec verbatim 拼到 prompt 开头，叠加场景/镜头/情绪、
    上一镜视觉摘要、全局 style_prefix。同角色描述词每镜完全一致。
    """
    chars = shot.get("characters_in_shot", []) or []
    specs: list[str] = []
    primary_seed = 0
    for cid in chars:
        card = character_bible.get(cid) or character_bible.get(str(cid))
        if not card:
            continue
        name = card.get("name", cid)
        spec = card.get("appearance_spec", "")
        if spec:
            specs.append(f"【{name}】{spec}")
        if not primary_seed:
            primary_seed = int(card.get("base_seed", 0))

    parts: list[str] = []
    if style_prefix:
        parts.append(style_prefix)
    if specs:
        parts.append("角色外观(严格保持一致): " + "；".join(specs))
    if shot.get("scene_desc"):
        parts.append(f"场景: {shot['scene_desc']}")
    if shot.get("camera"):
        parts.append(f"镜头: {shot['camera']}")
    if shot.get("emotion"):
        parts.append(f"情绪: {shot['emotion']}")
    if prev_shot_summary:
        parts.append(f"上一镜视觉延续: {prev_shot_summary}")
    image_prompt = "，".join(p for p in parts if p)

    seed = panel_seed(primary_seed, int(shot.get("index", 0)))
    negative = negative_prompt_global or shot.get("negative_prompt", "")
    return image_prompt, negative, seed


def cosine_similarity(vec_a, vec_b) -> float:
    """余弦相似度（numpy）。"""
    import numpy as np

    a = np.asarray(vec_a, dtype=float)
    b = np.asarray(vec_b, dtype=float)
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def check_face_consistency(
    image_path: str,
    anchor_path: str,
    face_threshold: float = 0.78,
) -> Optional[dict]:
    """与锚定图比对角色一致性。

    需要 insightface（重依赖）。未安装时返回 None，表示降级--调用方应跳过视觉校验，
    依赖文本锚定 + 种子 + confirm_anchors 人工闸门。
    """
    try:
        import numpy as np  # noqa: F401
        from insightface.app import FaceAnalysis  # noqa: F401
    except Exception:
        return None  # 降级

    # 真实实现需加载 FaceAnalysis、提取最大人脸 embedding、与锚定图 embedding 比对。
    # 默认环境无 insightface，此处返回 None；安装后可补全。
    return None
