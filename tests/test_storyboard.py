"""storyboard 工具测试。"""
from manga_drama.tools.storyboard import generate_storyboard


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return self._responses.pop(0)


def test_generate_storyboard_parses_shots():
    content = (
        '{"shots": [{"index": 1, "scene_desc": "黛玉进府", "dialogue": "我来迟了", '
        '"narration": "", "characters_in_shot": ["C001"], "camera": "中景", '
        '"emotion": "谨慎", "duration_sec": 3.5}, {"index": 2, "scene_desc": "宝玉初见", '
        '"characters_in_shot": ["C001","C002"], "camera": "近景", "emotion": "惊", "duration_sec": 2.5}]}'
    )
    resp = _FakeMsg(content)
    resp.usage_metadata = {"input_tokens": 100, "output_tokens": 200}
    llm = _FakeLLM([resp])
    cb = {"C001": {"name": "黛玉", "role": "主角"}, "C002": {"name": "宝玉", "role": "主角"}}
    shots, costs = generate_storyboard("剧本正文...", cb, llm)
    assert len(shots) == 2
    assert shots[0]["shot_id"] == "S001"
    assert shots[1]["shot_id"] == "S002"
    assert shots[0]["status"] == "pending"
    assert shots[0]["image_attempts"] == 0
    assert len(costs) == 1 and costs[0]["type"] == "llm"
    assert costs[0]["input_tokens"] == 100


def test_generate_storyboard_bad_json_returns_empty():
    resp = _FakeMsg("这不是 JSON")
    resp.usage_metadata = None
    llm = _FakeLLM([resp])
    shots, costs = generate_storyboard("剧本", {}, llm)
    assert shots == []
    assert costs == []  # 无 usage


def test_generate_storyboard_normalizes_missing_fields():
    content = '{"shots": [{"scene_desc": "x"}]}'  # 缺很多字段
    resp = _FakeMsg(content)
    resp.usage_metadata = None
    llm = _FakeLLM([resp])
    shots, _ = generate_storyboard("剧本", {}, llm)
    assert len(shots) == 1
    s = shots[0]
    assert s["shot_id"] == "S001"
    assert s["index"] == 1
    assert s["status"] == "pending"
