from __future__ import annotations

from unittest import mock

from langgraph.graph import StateGraph

from codeguard.config import load_settings
from codeguard.graph import build_graph
from codeguard.models import FileChange, PullRequestContext
from codeguard.nodes import ReviewNodes
from codeguard.state import ReviewState, initial_state
from tests.fakes import FakeGitHub, FakeProvider, FakeSandbox


EXPECTED = {
    "preflight": {"hybrid_scan", "finalize"},
    "hybrid_scan": {"verify_issue", "validate", "finalize"},
    "verify_issue": {"validate", "finalize"},
    "validate": {"review", "fix", "finalize"},
    "review": {"merge_gate", "fix", "finalize"},
    "fix": {"hybrid_scan", "finalize"},
    "merge_gate": {"merge", "human_approval", "finalize"},
    "human_approval": {"merge_gate", "human_approval", "finalize"},
}


def _nodes(tmp_path):
    cfg = load_settings(data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ck.db",
                        sandbox_backend="fake")
    return ReviewNodes(cfg, llm_provider=FakeProvider({}), github=FakeGitHub(), sandbox=FakeSandbox())


def test_path_maps_match_routes(tmp_path):
    nodes = _nodes(tmp_path)
    with mock.patch.object(StateGraph, "compile", lambda self, **kwargs: self):
        graph = build_graph(nodes, None)
    actual = {src: set(branch["condition"].ends) for src, branch in graph.branches.items()}
    assert set(actual) == set(EXPECTED)
    for src, keys in EXPECTED.items():
        assert actual[src] == keys, (src, actual[src], keys)
    assert "ingest" in graph.nodes and "merge" in graph.nodes and "finalize" in graph.nodes


def test_initial_state_contains_declared_keys():
    pr = PullRequestContext(repo="a/b", number=1, changed_files=[FileChange(path="x.py")])
    st = initial_state(pr, "j")
    missing = set(st) - set(ReviewState.__annotations__)
    assert not missing, missing
    assert st["candidate"] == "pr"
    assert st["review_round"] == 0
    assert st["tool_events"] == []
    assert st["tool_calls"] == 0
    assert "tool_events" in ReviewState.__annotations__
    assert "tool_calls" in ReviewState.__annotations__
