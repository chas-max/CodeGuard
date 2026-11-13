from __future__ import annotations

import json
from pathlib import Path

from codeguard.agents.semantic_detector import detect_semantics
from codeguard.engine.repo_map import build_repo_map
from tests.fakes import FakeLLM


class Ctx:
    def __init__(self, mapping):
        self.mapping = mapping

    def llm(self, role):
        return FakeLLM([self.mapping[role]])


def test_semantic_detector_grounds_findings(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    repo_map = build_repo_map(root)
    payload = {
        "findings": [
            {
                "category": "cross_file", "severity": "major", "confidence": 0.9,
                "paths": ["src/a.py"], "symbols": ["f"], "call_chain": ["g -> f"],
                "evidence": [{"path": "src/a.py", "line": 1, "snippet": "def f():"}],
                "explanation": "调用方契约变化", "recommendation": "同步调用方",
            },
            {
                "category": "cross_file", "severity": "major", "confidence": 0.9,
                "paths": ["src/does_not_exist.py"], "symbols": ["x"], "call_chain": ["x -> y"],
                "evidence": [{"path": "src/does_not_exist.py", "line": 1, "snippet": "x"}],
                "explanation": "幻觉路径", "recommendation": "x",
            },
        ],
        "limitations": [],
    }
    report, costs, _tool_events = detect_semantics(
        Ctx({"semantic_detector": json.dumps(payload, ensure_ascii=False)}),
        root=root, repo_map=repo_map, repo_context="", diff_text="",
        changed_files=["src/a.py"], deterministic_findings=[],
    )
    assert len(report.findings) == 1
    assert report.findings[0].paths == ["src/a.py"]
    assert report.findings[0].blocking is True
    assert any("grounding" in x for x in report.limitations)
