"""consistency 工具测试。"""
from manga_drama.tools.consistency import (
    build_image_prompt,
    check_face_consistency,
    cosine_similarity,
    panel_seed,
)


def test_panel_seed_deterministic():
    assert panel_seed(100, 0) == 100
    assert panel_seed(100, 1) == 100 ^ 1
    assert panel_seed(100, 1) == panel_seed(100, 1)


def test_build_image_prompt_injects_frozen_spec():
    cb = {
        "C001": {
            "name": "黛玉", "role": "主角", "base_seed": 777,
            "appearance_spec": "少女,清瘦,黑长直发,素白长裙,蹙眉",
        }
    }
    shot = {
        "index": 2, "scene_desc": "黛玉荷锄葬花",
        "camera": "中景", "emotion": "哀愁",
        "characters_in_shot": ["C001"],
    }
    ip, neg, seed = build_image_prompt(shot, cb, "中国古典工笔画风", "变形,模糊", None)
    assert "黛玉" in ip
    assert "素白长裙" in ip  # 冻结规格 verbatim
    assert "中国古典工笔画风" in ip  # style_prefix
    assert "黛玉荷锄葬花" in ip  # 场景
    assert "中景" in ip and "哀愁" in ip
    assert seed == panel_seed(777, 2)
    assert neg == "变形,模糊"


def test_build_image_prompt_deterministic():
    cb = {"C001": {"name": "A", "appearance_spec": "spec", "base_seed": 1}}
    shot = {"index": 1, "scene_desc": "s", "characters_in_shot": ["C001"]}
    a = build_image_prompt(shot, cb, "style", "neg", "prev")
    b = build_image_prompt(shot, cb, "style", "neg", "prev")
    assert a == b


def test_build_image_prompt_prev_summary():
    cb = {"C001": {"name": "A", "appearance_spec": "x", "base_seed": 1}}
    shot = {"index": 1, "scene_desc": "s", "characters_in_shot": ["C001"]}
    ip, _, _ = build_image_prompt(shot, cb, "style", "neg", "黛玉立于花畔")
    assert "上一镜视觉延续" in ip and "黛玉立于花畔" in ip


def test_build_image_prompt_multi_char():
    cb = {
        "C001": {"name": "黛", "appearance_spec": "spec1", "base_seed": 10},
        "C002": {"name": "宝", "appearance_spec": "spec2", "base_seed": 20},
    }
    shot = {"index": 1, "scene_desc": "s", "characters_in_shot": ["C001", "C002"]}
    ip, _, seed = build_image_prompt(shot, cb, "style", "neg", None)
    assert "spec1" in ip and "spec2" in ip
    # 主角种子取第一个角色
    assert seed == panel_seed(10, 1)


def test_cosine_similarity():
    assert abs(cosine_similarity([1, 0], [1, 0]) - 1.0) < 1e-9
    assert abs(cosine_similarity([1, 0], [0, 1]) - 0.0) < 1e-9
    assert cosine_similarity([0, 0], [1, 1]) == 0.0


def test_check_face_consistency_degrades_without_insightface():
    # 默认环境无 insightface，应降级返回 None
    assert check_face_consistency("a.png", "b.png") is None
