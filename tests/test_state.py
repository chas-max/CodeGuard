"""reducers 与 initial_state 测试。"""
from manga_drama.state import (
    append_list,
    initial_state,
    merge_character_bible,
    merge_shots,
)


def test_merge_shots_new_key():
    out = merge_shots(
        [{"shot_id": "S1", "status": "pending"}],
        [{"shot_id": "S2", "status": "pending"}],
    )
    assert {s["shot_id"] for s in out} == {"S1", "S2"}


def test_merge_shots_overwrite_same_id():
    out = merge_shots(
        [{"shot_id": "S1", "status": "pending", "scene_desc": "a"}],
        [{"shot_id": "S1", "status": "generated", "image_path": "x.png"}],
    )
    assert len(out) == 1
    s = out[0]
    assert s["status"] == "generated"
    assert s["image_path"] == "x.png"
    # 未被覆盖的字段保留
    assert s["scene_desc"] == "a"


def test_merge_shots_empty():
    assert merge_shots([], [{"shot_id": "S1"}]) == [{"shot_id": "S1"}]
    assert merge_shots([{"shot_id": "S1"}], []) == [{"shot_id": "S1"}]


def test_merge_character_bible_per_key():
    out = merge_character_bible(
        {"C1": {"name": "黛玉", "voice_id": "v1"}},
        {"C1": {"current_costume": "素白"}, "C2": {"name": "宝玉"}},
    )
    assert out["C1"] == {"name": "黛玉", "voice_id": "v1", "current_costume": "素白"}
    assert out["C2"]["name"] == "宝玉"


def test_append_list_monotonic():
    assert append_list([1, 2], [3]) == [1, 2, 3]
    assert append_list(None, [1]) == [1]
    assert append_list([1], None) == [1]


def test_initial_state_defaults():
    st = initial_state("做一部古风漫剧", "job-1")
    assert st["job_id"] == "job-1"
    assert st["user_input"] == "做一部古风漫剧"
    assert st["status"] == "clarifying"
    assert st["shots"] == []
    assert st["character_bible"] == {}
    assert st["budget_exceeded"] is False
    assert st["machine_revision_count"] == 0
    assert st["human_revision_count"] == 0
