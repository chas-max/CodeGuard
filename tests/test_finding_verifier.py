"""blocking finding 的两票复核：只降不升、不可用时保持阻塞（S3）。"""
from __future__ import annotations

import json
from pathlib import Path

from codeguard.agents.finding_verifier import verify_findings
from codeguard.config import load_settings
from codeguard.nodes import ReviewNodes
from tests.fakes import FakeGitHub, FakeProvider, FakeSandbox


def _nodes(tmp_path: Path, mapping: dict) -> ReviewNodes:
    cfg = load_settings(
        data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ck.db",
        sandbox_backend="fake",
    )
    return ReviewNodes(cfg, llm_provider=FakeProvider(mapping), github=FakeGitHub(),
                       sandbox=FakeSandbox())


def _finding(key: str = "f1", *, source: str = "semantic", blocking: bool = True) -> dict:
    return {
        "id": key, "source": source, "rule_id": "", "category": "cross_file",
        "severity": "major", "blocking": blocking, "path": "src/a.py", "line": 12,
        "message": "调用方未同步", "evidence": "src/a.py:12 call()",
    }


def _verdict(value: str) -> str:
    return json.dumps({"verdict": value, "reason": "代码事实", "counter_evidence": "src/a.py:12"}, ensure_ascii=False)


def _run(nodes, findings):
    return verify_findings(
        nodes, findings, repo_context="def call(): ...", diff_text="", max_items=5,
    )


def test_two_drops_demote_to_advisory(tmp_path: Path):
    nodes = _nodes(tmp_path, {
        "finding_verifier_a": [_verdict("drop")],
        "finding_verifier_b": [_verdict("drop")],
    })
    findings, costs, issues, _ = _run(nodes, [_finding()])
    assert findings[0]["blocking"] is False
    assert findings[0]["demoted_by"] == "finding_verifier"
    assert issues == []


def test_split_vote_demotes_with_tie_marker(tmp_path: Path):
    nodes = _nodes(tmp_path, {
        "finding_verifier_a": [_verdict("drop")],
        "finding_verifier_b": [_verdict("keep")],
    })
    findings, _, _, _ = _run(nodes, [_finding()])
    assert findings[0]["blocking"] is False
    assert findings[0]["demoted_by"] == "verifier_tie"


def test_unavailable_verifier_keeps_blocking(tmp_path: Path):
    nodes = _nodes(tmp_path, {})
    findings, _, issues, _ = _run(nodes, [_finding()])
    assert findings[0]["blocking"] is True
    assert issues and issues[0]["finding_id"] == "f1"


def test_deterministic_findings_are_not_verified(tmp_path: Path):
    nodes = _nodes(tmp_path, {
        "finding_verifier_a": [_verdict("drop")],
        "finding_verifier_b": [_verdict("drop")],
    })
    findings, costs, issues, _ = _run(nodes, [_finding("d1", source="deterministic")])
    assert findings[0]["blocking"] is True
    assert costs == [] and issues == []


def test_non_blocking_semantic_findings_are_skipped(tmp_path: Path):
    nodes = _nodes(tmp_path, {})
    findings, costs, issues, _ = _run(nodes, [_finding("f2", blocking=False)])
    assert findings[0]["blocking"] is False
    assert costs == [] and issues == []
