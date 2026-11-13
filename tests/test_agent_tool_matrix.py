"""ReviewNodes 工具工厂：默认开启、按 role 与 tier 收敛。"""
from __future__ import annotations

import json
from pathlib import Path

from codeguard.config import load_settings
from codeguard.engine.policy import Policy
from codeguard.models import FileChange, PullRequestContext
from codeguard.nodes import ReviewNodes
from codeguard.nodes.pipeline import review_node
from codeguard.state import initial_state
from tests.fakes import FakeGitHub, FakeProvider, FakeSandbox

REVIEW_APPROVE = json.dumps({
    "verdict": "approve", "confidence": 0.9, "summary": "0 issues",
    "findings": [], "fix_instructions": [], "residual_risks": [],
}, ensure_ascii=False)


def _nodes(tmp_path: Path, **overrides) -> ReviewNodes:
    cfg = load_settings(
        data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ck.db",
        sandbox_backend="fake", **overrides,
    )
    return ReviewNodes(cfg, llm_provider=FakeProvider({}), github=FakeGitHub(),
                       sandbox=FakeSandbox())


def _names(nodes: ReviewNodes, role: str, *, policy: Policy | None = None,
           tier: str = "T1", allowed: list[str] | None = None) -> set[str]:
    box = nodes.tools_for(
        role, root=Path("."), job_id="j", policy=policy or Policy(),
        tier=tier, allowed_files=allowed,
    )
    return {spec.name for spec in box.specs()} if box else set()


def test_factory_exposes_role_specific_tools(tmp_path: Path):
    nodes = _nodes(tmp_path)
    assert "grep" in _names(nodes, "semantic_detector")
    assert "run_tests" not in _names(nodes, "semantic_detector")
    assert "run_tests" in _names(nodes, "reviewer")
    assert "write_file" not in _names(nodes, "reviewer")
    assert "write_file" in _names(nodes, "fixer", allowed=["src/a.py"])


def test_factory_respects_tier_and_external_mode(tmp_path: Path):
    nodes = _nodes(tmp_path)
    assert "run_tests" not in _names(nodes, "reviewer", tier="T0")
    external = Policy.model_validate({"mode": "external"})
    assert "run_tests" not in _names(nodes, "reviewer", policy=external)
    assert "write_file" not in _names(nodes, "fixer", policy=external, allowed=["src/a.py"])


def test_factory_respects_env_kill_switch(tmp_path: Path):
    nodes = _nodes(tmp_path, agent_tools_disabled=True)
    assert _names(nodes, "reviewer") == set()
    read_only = _nodes(tmp_path / "ro", agent_tools_max_capability="read")
    names = _names(read_only, "reviewer")
    assert "run_tests" not in names and "grep" in names


def _review_state(root: Path):
    pr = PullRequestContext(
        repo="a/b", number=1, changed_files=[FileChange(path="calc.py")], author="alice",
    )
    state = initial_state(pr, "j")
    state.update({
        "repo_root": str(root),
        "profile": "change",
        "diff_text": "diff --git a/calc.py b/calc.py\n",
        "hybrid_scan_report": {},
        "validation_report": {"status": "passed"},
    })
    return state


def test_disabled_keeps_reviewer_prompt_unchanged(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("x = 1\n", encoding="utf-8")

    nodes = _nodes(tmp_path / "on")
    nodes._llm_provider = FakeProvider({"reviewer": [REVIEW_APPROVE]})
    review_node(_review_state(root), nodes)
    assert "可用工具" in nodes.llm("reviewer").prompts[0]

    off = _nodes(tmp_path / "off", agent_tools_disabled=True)
    off._llm_provider = FakeProvider({"reviewer": [REVIEW_APPROVE]})
    review_node(_review_state(root), off)
    assert "可用工具" not in off.llm("reviewer").prompts[0]
