"""reviewer 归一化：style 不阻塞、nit 上限、pre-existing 标记（S3）。"""
from __future__ import annotations

import json
from pathlib import Path

from codeguard.agents.reviewer import diff_added_lines
from codeguard.config import load_settings
from codeguard.engine.policy import Policy
from codeguard.models import FileChange, PullRequestContext
from codeguard.nodes import ReviewNodes
from codeguard.nodes.pipeline import review_node
from codeguard.state import initial_state
from tests.fakes import FakeGitHub, FakeProvider, FakeSandbox

DIFF = """diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,3 +1,4 @@
 def f(a):
+    b = a + 1
     return b
"""


def _nodes(tmp_path: Path, mapping: dict) -> ReviewNodes:
    cfg = load_settings(
        data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ck.db",
        sandbox_backend="fake",
    )
    return ReviewNodes(cfg, llm_provider=FakeProvider(mapping), github=FakeGitHub(),
                       sandbox=FakeSandbox())


def _state(**overrides):
    pr = PullRequestContext(
        repo="a/b", number=1, changed_files=[FileChange(path="calc.py")], author="alice",
    )
    state = initial_state(pr, "j")
    state.update({
        "profile": "change",
        "diff_text": DIFF,
        "hybrid_scan_report": {},
        "validation_report": {"status": "passed"},
    })
    state.update(overrides)
    return state


def test_diff_added_lines_parses_hunks():
    added = diff_added_lines(DIFF)
    assert added["calc.py"] == {2}


def test_reviewer_demotes_style_findings_and_caps_nits(tmp_path: Path):
    payload = json.dumps({
        "verdict": "request_changes",
        "confidence": 0.7,
        "summary": "0 critical, 1 major, 6 nit",
        "findings": [
            {"category": "style", "severity": "major", "blocking": True, "path": "calc.py",
             "line": 2, "message": "变量命名", "evidence": "calc.py:2 b"},
        ] + [
            {"category": "design", "severity": "minor", "blocking": False, "path": "calc.py",
             "line": 2, "message": f"nit {i}", "evidence": "calc.py:2"}
            for i in range(6)
        ],
        "fix_instructions": [],
        "residual_risks": [],
    }, ensure_ascii=False)
    nodes = _nodes(tmp_path, {"reviewer": [payload]})
    state = _state(policy=Policy.model_validate({"review": {"max_nits": 2}}).model_dump(mode="json"))
    update = review_node(state, nodes)
    findings = update["review_decision"]["findings"]
    assert len(findings) == 2  # nit 上限 2，style 不阻塞也不再占 nit 名额之外
    assert all(not item["blocking"] for item in findings)
    assert findings[0]["demoted_by"] in ("style", "severity")


def test_reviewer_marks_preexisting_lines(tmp_path: Path):
    payload = json.dumps({
        "verdict": "request_changes", "confidence": 0.8, "summary": "1 major",
        "findings": [
            {"category": "correctness", "severity": "major", "blocking": True,
             "path": "calc.py", "line": 2, "message": "新引入", "evidence": "calc.py:2"},
            {"category": "correctness", "severity": "major", "blocking": True,
             "path": "calc.py", "line": 99, "message": "老代码里就有", "evidence": "calc.py:99"},
        ],
        "fix_instructions": [], "residual_risks": [],
    }, ensure_ascii=False)
    nodes = _nodes(tmp_path, {"reviewer": [payload]})
    update = review_node(_state(), nodes)
    findings = {item["message"]: item for item in update["review_decision"]["findings"]}
    assert findings["新引入"]["introduced_by_pr"] is True
    assert findings["老代码里就有"]["introduced_by_pr"] is False


def test_reviewer_prompt_contains_guidance_and_profile(tmp_path: Path):
    payload = json.dumps({
        "verdict": "approve", "confidence": 0.9, "summary": "0 issues",
        "findings": [], "fix_instructions": [], "residual_risks": [],
    }, ensure_ascii=False)
    nodes = _nodes(tmp_path, {"reviewer": [payload]})
    review_node(_state(review_guidance="金额算错就是失败", risk_tier={"tier": "T2", "depth": "deep"}), nodes)
    prompt = nodes.llm("reviewer").prompts[0]
    assert "金额算错就是失败" in prompt
    assert "T2" in prompt and "deep" in prompt
