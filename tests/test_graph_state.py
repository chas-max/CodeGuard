from __future__ import annotations

from unittest import mock

from langgraph.graph import StateGraph

from codeguard.config import load_settings
from codeguard.graph import REVIEWER_BRANCH_NODES, build_graph
from codeguard.models import FileChange, PullRequestContext
from codeguard.nodes import ReviewNodes
from codeguard.state import ReviewState, initial_state
from tests.fakes import FakeGitHub, FakeProvider, FakeSandbox

EXPECTED = {
    "prepare_context": {"pr_reader", "persist_memory_and_finalize"},
    "pr_reader": {"dispatch_reviewers", "persist_memory_and_finalize"},
    "synthesize_findings": {
        "coder_agent", "verify_gate", "summary_and_submit", "persist_memory_and_finalize",
    },
    "coder_agent": {"qa_checker", "summary_and_submit", "persist_memory_and_finalize"},
    "qa_checker": {
        "verify_gate", "coder_agent", "summary_and_submit", "persist_memory_and_finalize",
    },
    "verify_gate": {
        "dispatch_reviewers", "coder_agent", "summary_and_submit", "persist_memory_and_finalize",
    },
    "summary_and_submit": {"merge_gate", "persist_memory_and_finalize"},
    "merge_gate": {"merge", "human_approval", "persist_memory_and_finalize"},
    "human_approval": {"merge_gate", "human_approval", "persist_memory_and_finalize"},
}

EXPECTED_NODES = {
    "prepare_context",
    "pr_reader",
    "dispatch_reviewers",
    *REVIEWER_BRANCH_NODES,
    "synthesize_findings",
    "coder_agent",
    "qa_checker",
    "verify_gate",
    "summary_and_submit",
    "merge_gate",
    "human_approval",
    "merge",
    "persist_memory_and_finalize",
}


def _nodes(tmp_path):
    cfg = load_settings(
        data_dir=tmp_path / "data",
        checkpoint_path=tmp_path / "ck.db",
        sandbox_backend="fake",
    )
    return ReviewNodes(cfg, llm_provider=FakeProvider({}), github=FakeGitHub(), sandbox=FakeSandbox())


def test_path_maps_match_routes(tmp_path):
    nodes = _nodes(tmp_path)
    with mock.patch.object(StateGraph, "compile", lambda self, **kwargs: self):
        graph = build_graph(nodes, None)
    actual = {src: set(branch["condition"].ends) for src, branch in graph.branches.items()}
    assert set(actual) == set(EXPECTED)
    for src, keys in EXPECTED.items():
        assert actual[src] == keys, (src, actual[src], keys)
    assert EXPECTED_NODES <= set(graph.nodes)
    for branch in REVIEWER_BRANCH_NODES:
        assert ("dispatch_reviewers", branch) in graph.edges
        assert (branch, "synthesize_findings") in graph.edges
    assert ("merge", "persist_memory_and_finalize") in graph.edges


def test_initial_state_contains_declared_keys():
    pr = PullRequestContext(repo="a/b", number=1, changed_files=[FileChange(path="x.py")])
    st = initial_state(pr, "j")
    missing = set(st) - set(ReviewState.__annotations__)
    assert not missing, missing
    assert st["candidate"] == "pr"
    assert st["review_round"] == 0
    assert st["importance_level"] == "medium"
    assert st["tool_events"] == []
    assert st["tool_calls"] == 0
    assert "reviewer_memories" in ReviewState.__annotations__
    assert "fix_directive" in ReviewState.__annotations__
    assert "qa_report" in ReviewState.__annotations__
