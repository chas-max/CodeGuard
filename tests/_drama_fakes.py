"""测试用假对象：假 LLM / T2I / TTS 与预设响应 provider。"""

from pathlib import Path


class Msg:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage_metadata = usage


class FakeLLM:
    def __init__(self, responses):
        self._r = list(responses)

    def invoke(self, messages):
        return self._r.pop(0)


class FakeT2I:
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


class FakeTTS:
    def synthesize(self, text, voice, out):
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"MP3" * 200)  # 足够大以通过 observe 检查
        return str(out)


def happy_provider():
    """全流程通过的预设响应（含 Plan-Execute-Reflection + ReAct 新角色）。"""
    resp = {
        "understand": ['{"action":"brief","brief":{"title":"葬花","genre":"古风","theme":"伤春","tone":"哀","target_duration_sec":30,"audience":"大众","characters":[{"name":"黛玉","role":"主角"}],"plot_summary":"黛玉葬花","plot_points":[],"taboos":[],"style_prefix":"中国古典工笔画风","negative_prompt":"变形"},"assumptions":[]}'],
        "director_plan": ['{"plan":{"scene_count":2,"total_duration_sec":30,"pace":"舒缓","style_prefix":"中国古典工笔画风"},"characters":[{"char_id":"C001","name":"黛玉","role":"主角","appearance_spec":"少女,蹙眉,黑长直发,素白裙","base_seed":777,"voice_id":"longxiaochun","current_costume":"素白裙"}]}'],
        "write_outline": ['{"outline":"第一场：黛玉荷锄出场，见落花满地。第二场：黛玉葬花泣涕。","scene_count":2,"key_turning_points":["见落花伤感"]}'],
        "write": ['{"draft":"黛玉荷锄葬花，泣涕。","plot_summary":"葬花"}'],
        "write_reflect": ['{"passed":true,"issues":[],"revised_draft":""}'],
        "review": ['{"passed":true,"issues":[],"severity":"none","category":"none","feedback":""}'],
        "review_verify": ['{"confirmed":true,"revised_passed":null,"revised_feedback":""}'],
        "director_supervise": ['{"decision":"approve","feedback":""}'],
        "director_supervise_verify": ['{"confirmed":true,"revised_decision":"","revised_feedback":""}'],
        "produce": ['{"shots":[{"index":1,"scene_desc":"黛玉葬花","dialogue":"花谢花飞花满天","narration":"","characters_in_shot":["C001"],"camera":"中景","emotion":"哀","duration_sec":3.0}]}'],
        "produce_react": ['{"action":"skip","reason":"重试策略分析"}'],
    }
    llms = {r: FakeLLM([Msg(c, {"input_tokens": 10, "output_tokens": 20}) for c in v])
            for r, v in resp.items()}
    return lambda role: llms[role]


def fake_compose(shots, output_path, **kw):
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"MP4")
    return str(out)
