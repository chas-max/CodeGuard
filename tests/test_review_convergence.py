"""审查收敛：增量复审、轮次上限转人工（S3）。"""
from __future__ import annotations

import json
from pathlib import Path

from codeguard.config import load_settings
from codeguard.models import FileChange, PullRequestContext
from codeguard.nodes import ReviewNodes
from codeguard.nodes.pipeline import review_node, route_after_review
from codeguard.state import initial_state
from tests.fakes import FakeGitHub, FakeProvider, FakeSandbox

REQUEST_CHANGES = json.dumps({
    "verdict": "request_changes", "confidence": 0.8, "summary": "1 major",
    "findings": [{
        "category": "correctness", "severity": "major", "blocking": True,
        "path": "calc.py", "line": 2, "message": "仍然有问题", "evidence": "calc.py:2",
    }],
    "fix_instructions": ["改方案"], "residual_risks": [],
}, ensure_ascii=False)

FIX_PATCH = """diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def f(a):
-    return a
+    return a + 1
"""


def _setup(tmp_path: Path):
    cfg = load_settings(
        data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ck.db",
        sandbox_backend="fake", max_review_rounds=2,
    )
    provider = FakeProvider({"reviewer": [REQUEST_CHANGES, REQUEST_CHANGES]})
    nodes = ReviewNodes(cfg, llm_provider=provider, github=FakeGitHub(), sandbox=FakeSandbox())
    pr = PullRequestContext(
        repo="a/b", number=1, changed_files=[FileChange(path="calc.py")], author="alice",
    )
    state = initial_state(pr, "j")
    state.update({
        "profile": "change", "diff_text": FIX_PATCH, "hybrid_scan_report": {},
        "validation_report": {"status": "passed"},
    })
    return nodes, state


def test_second_round_reviews_incremental_diff_only(tmp_path: Path):
    nodes, state = _setup(tmp_path)
    state.update({"review_round": 1, "fix_patch": FIX_PATCH})
    update = review_node(state, nodes)
    assert update["review_round_mode"] == "incremental"
    prompt = nodes.llm("reviewer").prompts[0]
    assert "增量复审" in prompt
    # 增量模式只给 fix patch，不再重复整份 PR diff 的说明
    assert "本轮是全量审查" not in prompt


def test_round_cap_escalates_instead_of_fixing_forever(tmp_path: Path):
    nodes, state = _setup(tmp_path)
    state.update({"review_round": 1, "fix_patch": FIX_PATCH, "review_round_mode": "incremental"})
    update = review_node(state, nodes)
    assert update["status"] == "review_escalated"
    assert update["escalation_reason"]
    assert route_after_review({**state, **update}, nodes) == "finalize"


def test_first_round_is_full_review(tmp_path: Path):
    nodes, state = _setup(tmp_path)
    update = review_node(state, nodes)
    assert update["review_round_mode"] == "full"
    assert "本轮是全量审查" in nodes.llm("reviewer").prompts[0]
