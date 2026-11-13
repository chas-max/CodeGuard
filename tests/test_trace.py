"""全链路追踪：快照历史 → 节点时间线。"""
from __future__ import annotations

from pathlib import Path

from codeguard.config import load_settings
from codeguard.graph import make_checkpointer
from codeguard.models import FileChange, PullRequestContext
from codeguard.nodes import ReviewNodes
from codeguard.runtime import CodeGuardRuntime
from codeguard.trace import build_trace, current_node, node_summary
from tests.fakes import (
    CLAIM_JSON,
    REVIEW_APPROVE,
    SEMANTIC_EMPTY,
    FakeGitHub,
    FakeProvider,
    FakeSandbox,
    make_fixture_repo,
)


def _states() -> list[dict]:
    """构造升序快照：ingest → preflight → hybrid_scan（进行中）。"""
    base_values = {"job_id": "j1", "repo": "acme/repo", "pr_number": 3, "pr_title": "fix: x"}
    return [
        {"step": 1, "values": base_values, "next": ["ingest"], "created_at": "2026-01-01T00:00:00+00:00"},
        {
            "step": 2,
            "values": {**base_values, "profile": "change", "changed_files": [{"path": "a.py"}]},
            "next": ["preflight"],
            "created_at": "2026-01-01T00:00:01+00:00",
        },
        {
            "step": 3,
            "values": {
                **base_values,
                "profile": "change",
                "risk_tier": {"tier": "T1"},
                "changed_files": [{"path": "a.py"}],
            },
            "next": ["hybrid_scan"],
            "created_at": "2026-01-01T00:00:03+00:00",
        },
        {
            "step": 4,
            "values": {
                **base_values,
                "profile": "change",
                "risk_tier": {"tier": "T1"},
                "changed_files": [{"path": "a.py"}],
                "findings": [{"id": "f1", "blocking": True}],
            },
            "next": [],
            "created_at": "2026-01-01T00:00:06+00:00",
        },
    ]


def test_build_trace_maps_nodes_and_durations():
    steps = build_trace(_states())
    assert [step["node"] for step in steps] == ["ingest", "preflight", "hybrid_scan"]
    assert steps[0]["duration_ms"] == 1000
    assert steps[1]["duration_ms"] == 2000
    assert steps[2]["started_at"].endswith("00:00:03+00:00")
    assert "findings 1" in steps[2]["summary"]
    assert steps[2]["changed_keys"] == ["findings"]


def test_build_trace_marks_running_node():
    states = _states()
    states[-1] = {**states[-1], "next": ["verify_issue"]}
    steps = build_trace(states)
    assert "verify_issue" in [step["node"] for step in steps]
    assert steps[-1]["node"] == "verify_issue"
    assert steps[-1]["running"] is True
    assert current_node(states) == "verify_issue"


def test_build_trace_handles_empty():
    assert build_trace([]) == []
    assert current_node([]) == ""


def test_node_summary_uses_state():
    assert "T2" in node_summary("preflight", {"risk_tier": {"tier": "T2"}})
    assert node_summary("merge", {"merge_commit_sha": "c" * 40}).startswith("sha=cccc")
    assert "MERGED" in node_summary("finalize", {"outcome": "MERGED"})


def _pr() -> PullRequestContext:
    return PullRequestContext(
        repo="acme/repo", number=7,
        base_ref="main", base_sha="a" * 40,
        head_ref="fix/zero", head_sha="b" * 40,
        title="fix: divide by zero", author="dev", profile="bugfix",
        changed_files=[
            FileChange(path="calc.py", status="modified"),
            FileChange(path="tests/test_calc.py", status="modified"),
        ],
    )


def test_trace_from_real_checkpoints(tmp_path: Path):
    base, head = make_fixture_repo(tmp_path)
    cfg = load_settings(
        data_dir=tmp_path / "data",
        checkpoint_path=tmp_path / "ckpt.db",
        sandbox_backend="fake",
        merge_mode="auto",
    )
    provider = FakeProvider({
        "issue_verifier": [CLAIM_JSON],
        "semantic_detector": [SEMANTIC_EMPTY],
        "reviewer": [REVIEW_APPROVE],
    })
    nodes = ReviewNodes(cfg, llm_provider=provider, github=FakeGitHub(), sandbox=FakeSandbox())
    runtime = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ckpt.db"))
    snap = runtime.start(_pr(), "job-trace-e2e", repo_root=str(head), base_root=str(base))
    if not snap["done"]:
        snap = runtime.resume("job-trace-e2e", {"approved": True, "approver": "reviewer-b"})
    assert snap["done"], snap.get("values", {}).get("error")

    states = [
        {
            "values": dict(state.values or {}),
            "next": list(state.next or []),
            "created_at": state.created_at,
            "step": int((state.metadata or {}).get("step", 0) or 0),
        }
        for state in runtime.graph.get_state_history(runtime._config("job-trace-e2e"), limit=80)
    ]
    steps = build_trace(states)
    nodes_seen = [step["node"] for step in steps]
    assert nodes_seen[0] == "ingest"
    assert "hybrid_scan" in nodes_seen
    assert "validate" in nodes_seen
    assert "review" in nodes_seen
    # 合并路径下 merge 直接连到 END，finalize 不参与
    assert "merge" in nodes_seen
    assert steps[-1]["node"] == "merge"
    assert steps[-1]["finished_at"]
