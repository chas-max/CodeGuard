"""全图路由与流程测试（mock 全部 LLM 与外部工具）。"""
from __future__ import annotations

from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from manga_drama.graph import build_graph
from manga_drama.nodes import GraphNodes
from manga_drama.state import initial_state


class _Msg:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage_metadata = usage


class _LLM:
    def __init__(self, responses):
        self._r = list(responses)

    def invoke(self, messages):
        return self._r.pop(0)


class _T2I:
    model = "wanx"

    def text_to_image(self, prompt, neg, seed, size, out):
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"PNG" * 500)  # 足够大以通过 observe 检查
        return str(out)

    def supports_reference(self):
        return False

    def supports_edit(self):
        return False


class _TTS:
    def synthesize(self, text, voice, out):
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"MP3" * 200)  # 足够大以通过 observe 检查
        return str(out)


def _provider():
    """返回 (llm_provider, llms)，按角色给预设 JSON 响应（含新架构角色）。"""
    resp = {
        "understand": ['{"action":"brief","brief":{"title":"葬花","genre":"古风","theme":"伤春","tone":"哀","target_duration_sec":30,"audience":"大众","characters":[{"name":"黛玉","role":"主角"}],"plot_summary":"黛玉葬花","plot_points":[],"taboos":[],"style_prefix":"中国古典工笔画风,电影级打光","negative_prompt":"变形,模糊,多余手指"},"assumptions":[]}'],
        "director_plan": ['{"plan":{"scene_count":2,"total_duration_sec":30,"pace":"舒缓","style_prefix":"中国古典工笔画风"},"characters":[{"char_id":"C001","name":"黛玉","role":"主角","appearance_spec":"少女,清瘦,蹙眉,黑长直发,素白长裙,苍白的脸","base_seed":777,"voice_id":"longxiaochun","current_costume":"素白裙"}]}'],
        "write_outline": ['{"outline":"第一场：黛玉荷锄出场，见落花满地。第二场：黛玉葬花泣涕，吟诗。","scene_count":2,"key_turning_points":["见落花伤感","吟诗抒怀"]}'],
        "write": ['{"draft":"黛玉荷锄而来，见落红满地，泣涕葬花。","plot_summary":"黛玉葬花"}'],
        "write_reflect": ['{"passed":true,"issues":[],"revised_draft":""}'],
        "review": ['{"passed":true,"issues":[],"severity":"none","category":"none","feedback":""}'],
        "review_verify": ['{"confirmed":true,"revised_passed":null,"revised_feedback":""}'],
        "director_supervise": ['{"decision":"approve","feedback":""}'],
        "director_supervise_verify": ['{"confirmed":true,"revised_decision":"","revised_feedback":""}'],
        "produce": ['{"shots":[{"index":1,"scene_desc":"黛玉荷锄葬花","dialogue":"花谢花飞花满天","narration":"","characters_in_shot":["C001"],"camera":"中景","emotion":"哀","duration_sec":3.0}]}'],
        "produce_react": ['{"action":"skip","reason":"重试策略分析"}'],
    }
    llms = {role: _LLM([_Msg(c, {"input_tokens": 10, "output_tokens": 20}) for c in contents])
            for role, contents in resp.items()}

    def provider(role):
        return llms[role]
    return provider, llms


def _make_nodes(mock_cfg, t2i=None, tts=None, provider=None):
    p = provider or _provider()[0]
    return GraphNodes(mock_cfg, llm_provider=p, t2i=t2i or _T2I(), tts=tts or _TTS())


def test_full_happy_path(mock_cfg, monkeypatch):
    # mock compose_vertical_video 避免真实调用 ffmpeg
    def fake_compose(shots, output_path, **kw):
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"MP4")
        return str(out)
    monkeypatch.setattr("manga_drama.nodes.produce.compose_vertical_video", fake_compose)

    nodes = _make_nodes(mock_cfg)
    app = build_graph(nodes, checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "t1"}, "recursion_limit": 60}

    # 第一段：跑到 human_review 中断
    app.invoke(initial_state("做一部古风葬花漫剧", "t1"), cfg)
    st = app.get_state(cfg)
    assert st.next, "应在 human_review 处中断"
    assert st.tasks[0].interrupts[0].value["type"] == "human_review"

    # 人工审核通过
    app.invoke(Command(resume={"approved": True, "feedback": ""}), cfg)
    st = app.get_state(cfg)
    assert st.next, "应在 confirm_anchors 处中断"
    assert st.tasks[0].interrupts[0].value["type"] == "confirm_anchors"

    # 锚定确认通过 -> 制作 -> 完成
    app.invoke(Command(resume={"approved": True}), cfg)
    final = app.get_state(cfg).values
    assert final["status"] == "done"
    assert final["video_path"]
    assert final["shots"][0]["status"] == "generated"
    # 成本已记录
    assert len(final["budget_events"]) > 0


def test_clarify_loop(mock_cfg):
    """understand 先追问一次，回答后再出 brief。"""
    resp = {
        "understand": [
            '{"action":"ask","question":"想要什么题材与基调？","missing":["题材","基调"]}',
            '{"action":"brief","brief":{"title":"x","genre":"古风","theme":"t","tone":"哀","target_duration_sec":30,"audience":"a","characters":[{"name":"A","role":"主角"}],"plot_summary":"s","plot_points":[],"taboos":[],"style_prefix":"画风","negative_prompt":"模糊"},"assumptions":[]}',
        ],
        "director_plan": ['{"plan":{"scene_count":1,"total_duration_sec":30,"pace":"慢","style_prefix":"画风"},"characters":[{"char_id":"C001","name":"A","role":"主角","appearance_spec":"少女","base_seed":1,"voice_id":"longxiaochun","current_costume":""}]}'],
        "write_outline": ['{"outline":"大纲","scene_count":1,"key_turning_points":[]}'],
        "write": ['{"draft":"剧本","plot_summary":"s"}'],
        "write_reflect": ['{"passed":true,"issues":[],"revised_draft":""}'],
        "review": ['{"passed":true,"issues":[],"severity":"none","category":"none","feedback":""}'],
        "review_verify": ['{"confirmed":true,"revised_passed":null,"revised_feedback":""}'],
        "director_supervise": ['{"decision":"approve","feedback":""}'],
        "director_supervise_verify": ['{"confirmed":true,"revised_decision":"","revised_feedback":""}'],
    }
    llms = {r: _LLM([_Msg(c, {"input_tokens": 5, "output_tokens": 5}) for c in v]) for r, v in resp.items()}
    nodes = GraphNodes(mock_cfg, llm_provider=lambda r: llms[r], t2i=_T2I(), tts=_TTS())
    app = build_graph(nodes, checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "c1"}, "recursion_limit": 60}
    app.invoke(initial_state("做个漫剧", "c1"), cfg)
    st = app.get_state(cfg)
    assert st.next and st.tasks[0].interrupts[0].value["type"] == "clarify"
    # 回答
    app.invoke(Command(resume="古风伤春"), cfg)
    st = app.get_state(cfg)
    # 应已过 understand 出 brief，进入后续，停在 human_review
    assert st.next and st.tasks[0].interrupts[0].value["type"] == "human_review"
    vals = app.get_state(cfg).values
    assert vals["brief"] is not None
    assert len(vals["clarification_qa"]) == 1
    assert vals["clarification_qa"][0]["answer"] == "古风伤春"


def test_machine_reject_bypass_to_human(mock_cfg):
    """机器审核始终不通过 -> 达 max_machine 后强制放行到导演监督 -> 人工审核中断。

    机器审核不再硬卡终止，而是把把关权交给人工，确保流程能推进到画面生成。
    """
    from dataclasses import replace
    cfg = replace(mock_cfg, max_machine=2, max_human=3)
    resp = {
        "understand": ['{"action":"brief","brief":{"title":"x","genre":"g","theme":"t","tone":"哀","target_duration_sec":30,"audience":"a","characters":[{"name":"A","role":"主角"}],"plot_summary":"s","plot_points":[],"taboos":[],"style_prefix":"画风","negative_prompt":"模糊"},"assumptions":[]}'],
        "director_plan": ['{"plan":{"scene_count":1,"total_duration_sec":30,"pace":"慢","style_prefix":"画风"},"characters":[{"char_id":"C001","name":"A","role":"主角","appearance_spec":"少女","base_seed":1,"voice_id":"longxiaochun","current_costume":""}]}'],
        "write_outline": ['{"outline":"大纲","scene_count":1,"key_turning_points":[]}'],
        "write": ['{"draft":"剧本","plot_summary":"s"}'],
        "write_reflect": ['{"passed":true,"issues":[],"revised_draft":""}'],
        "review": ['{"passed":false,"issues":["暴力"],"severity":"major","category":"violence","feedback":"第1段过于暴力，请弱化"}'],
        "review_verify": ['{"confirmed":true,"revised_passed":null,"revised_feedback":""}'],
        "director_supervise": ['{"decision":"approve","feedback":""}'],
        "director_supervise_verify": ['{"confirmed":true,"revised_decision":"","revised_feedback":""}'],
    }
    llms = {r: _LLM([_Msg(c, {"input_tokens": 5, "output_tokens": 5}) for c in v]) for r, v in resp.items()}
    # write/review 会循环调用，需多次响应
    llms["write"] = _LLM([_Msg('{"draft":"剧本","plot_summary":"s"}', {"input_tokens": 5, "output_tokens": 5}) for _ in range(10)])
    llms["write_outline"] = _LLM([_Msg('{"outline":"大纲","scene_count":1,"key_turning_points":[]}', {"input_tokens": 5, "output_tokens": 5}) for _ in range(10)])
    llms["write_reflect"] = _LLM([_Msg('{"passed":true,"issues":[],"revised_draft":""}', {"input_tokens": 5, "output_tokens": 5}) for _ in range(10)])
    llms["review"] = _LLM([_Msg('{"passed":false,"issues":["暴力"],"severity":"major","category":"violence","feedback":"第1段过于暴力，请弱化"}', {"input_tokens": 5, "output_tokens": 5}) for _ in range(10)])
    llms["review_verify"] = _LLM([_Msg('{"confirmed":true,"revised_passed":null,"revised_feedback":""}', {"input_tokens": 5, "output_tokens": 5}) for _ in range(10)])
    nodes = GraphNodes(cfg, llm_provider=lambda r: llms[r], t2i=_T2I(), tts=_TTS())
    app = build_graph(nodes, checkpointer=MemorySaver())
    cfg_thread = {"configurable": {"thread_id": "m1"}, "recursion_limit": 80}
    app.invoke(initial_state("做个漫剧", "m1"), cfg_thread)
    st = app.get_state(cfg_thread)
    vals = st.values
    # 到达机器审核上限后强制放行 -> 导演监督(强制approve) -> 人工审核中断
    assert st.next, "应在 human_review 处中断（机器审核上限后放行）"
    assert st.tasks[0].interrupts[0].value["type"] == "human_review"
    assert vals["machine_revision_count"] >= cfg.max_machine


# ---- 路由函数单元测试 ----


def test_route_after_machine(mock_cfg):
    nodes = _make_nodes(mock_cfg)
    s = {"machine_verdict": {"passed": True}}
    assert nodes.route_after_machine(s) == "director_supervise"
    s = {"machine_verdict": {"passed": False}, "machine_revision_count": 0}
    assert nodes.route_after_machine(s) == "write_draft"
    # 到达重试上限：不再 escalate，强制放行到导演监督，把关权交人工
    s = {"machine_verdict": {"passed": False}, "machine_revision_count": mock_cfg.max_machine}
    assert nodes.route_after_machine(s) == "director_supervise"
    s = {"budget_exceeded": True, "machine_verdict": {"passed": True}}
    assert nodes.route_after_machine(s) == "escalate"


def test_route_after_supervise(mock_cfg):
    nodes = _make_nodes(mock_cfg)
    assert nodes.route_after_supervise({"director_decision": "approve"}) == "human_review"
    assert nodes.route_after_supervise({"director_decision": "revise", "machine_revision_count": 0, "human_revision_count": 0}) == "write_draft"
    s = {"director_decision": "revise", "machine_revision_count": mock_cfg.max_machine, "human_revision_count": mock_cfg.max_human}
    assert nodes.route_after_supervise(s) == "escalate"


def test_route_after_human(mock_cfg):
    nodes = _make_nodes(mock_cfg)
    assert nodes.route_after_human({"human_decision": {"approved": True}}) == "build_anchors"
    assert nodes.route_after_human({"human_decision": {"approved": False}, "human_revision_count": 0}) == "write_draft"
    s = {"human_decision": {"approved": False}, "human_revision_count": mock_cfg.max_human}
    assert nodes.route_after_human(s) == "escalate"


def test_route_after_anchors(mock_cfg):
    nodes = _make_nodes(mock_cfg)
    assert nodes.route_after_anchors({"anchor_decision": {"approved": True}}) == "produce_video"
    assert nodes.route_after_anchors({"anchor_decision": {"approved": False}, "anchor_round": 0}) == "build_anchors"
    s = {"anchor_decision": {"approved": False}, "anchor_round": mock_cfg.max_anchor}
    assert nodes.route_after_anchors(s) == "escalate"


def test_route_after_understand(mock_cfg):
    nodes = _make_nodes(mock_cfg)
    # brief 已产出 + chat_mode=="idle" -> director_plan
    assert nodes.route_after_understand({"brief": {"title": "x"}, "chat_mode": "idle"}) == "director_plan"
    # 无 brief -> clarify_ask
    assert nodes.route_after_understand({}) == "clarify_ask"
    # 有 brief 但 chat_mode 不是 idle -> clarify_ask（仍在澄清）
    assert nodes.route_after_understand({"brief": {"title": "x"}, "chat_mode": "briefing"}) == "clarify_ask"
    # 用户发起修改 -> write_draft
    assert nodes.route_after_understand({"_revision_target": "write_draft"}) == "write_draft"
    # 用户发起大改 -> director_plan
    assert nodes.route_after_understand({"_revision_target": "director_plan"}) == "director_plan"
