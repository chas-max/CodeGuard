"""运行时 MangaDramaRuntime 集成测试（mock 全部外部依赖）。"""
from langgraph.checkpoint.memory import MemorySaver

from manga_drama.nodes import GraphNodes
from manga_drama.runtime import MangaDramaRuntime

from ._drama_fakes import FakeT2I, FakeTTS, fake_compose, happy_provider


def _rt(mock_cfg, monkeypatch):
    monkeypatch.setattr("manga_drama.nodes.produce.compose_vertical_video", fake_compose)
    nodes = GraphNodes(mock_cfg, llm_provider=happy_provider(),
                      t2i=FakeT2I(), tts=FakeTTS())
    return MangaDramaRuntime(mock_cfg, nodes=nodes, checkpointer=MemorySaver())


def test_runtime_start_resume_done(mock_cfg, monkeypatch):
    rt = _rt(mock_cfg, monkeypatch)
    snap = rt.start("做一部黛玉葬花古风漫剧", "rt1")
    # 跑到人工审核中断
    assert snap["interrupt"]["type"] == "human_review"
    assert not snap["done"]

    snap = rt.resume("rt1", {"approved": True, "feedback": ""})
    assert snap["interrupt"]["type"] == "confirm_anchors"

    snap = rt.resume("rt1", {"approved": True})
    assert snap["done"]
    assert snap["values"]["status"] == "done"
    assert snap["values"]["video_path"]


def test_runtime_clarify_resume_with_string(mock_cfg, monkeypatch):
    """澄清中断用字符串恢复。"""
    rt = _rt(mock_cfg, monkeypatch)
    # 用一个先追问的 provider
    from ._drama_fakes import FakeLLM, Msg
    p = happy_provider()
    # 替换 understand 的 LLM 为先 ask 后 brief
    ask = '{"action":"ask","question":"想要什么基调？","missing":["基调"]}'
    brief = '{"action":"brief","brief":{"title":"x","genre":"g","theme":"t","tone":"哀","target_duration_sec":30,"audience":"a","characters":[{"name":"A","role":"主角"}],"plot_summary":"s","plot_points":[],"taboos":[],"style_prefix":"画风","negative_prompt":"模糊"},"assumptions":[]}'
    llms = {
        "understand": FakeLLM([Msg(ask, {"input_tokens": 5, "output_tokens": 5}),
                               Msg(brief, {"input_tokens": 5, "output_tokens": 5})]),
    }
    # 复用 happy provider 的其余角色，但 understand 用上面的
    base = happy_provider()

    def provider(role):
        if role == "understand":
            return llms["understand"]
        return base(role)

    nodes = GraphNodes(mock_cfg, llm_provider=provider, t2i=FakeT2I(), tts=FakeTTS())
    rt2 = MangaDramaRuntime(mock_cfg, nodes=nodes, checkpointer=MemorySaver())
    snap = rt2.start("做个漫剧", "rt2")
    assert snap["interrupt"]["type"] == "clarify"
    snap = rt2.resume("rt2", "古风伤春")
    assert snap["interrupt"]["type"] == "human_review"
    assert snap["values"]["clarification_qa"][0]["answer"] == "古风伤春"
