"""审批分级：按 tier 决定人数、审批绑定 commit、非作者约束（S2）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeguard.config import load_settings
from codeguard.engine.policy import Policy
from codeguard.github.merge import merge_blockers, valid_approvals
from codeguard.graph import make_checkpointer
from codeguard.models import (
    Approval,
    FileChange,
    MergeMode,
    PullRequestContext,
    ReviewDecision,
    ReviewVerdict,
    SandboxStatus,
    ValidationReport,
)
from codeguard.nodes import ReviewNodes
from codeguard.runtime import CodeGuardRuntime
from tests.fakes import (
    REVIEW_APPROVE,
    SEMANTIC_EMPTY,
    FakeGitHub,
    FakeProvider,
    FakeSandbox,
    make_fixture_repo,
)

CHANGE_CLAIM = json.dumps({
    "kind": "feature", "statement": "调整认证流程", "expected": "登录成功",
    "actual": "现状", "repro_steps": [], "affected_paths": ["src/auth/login.py"],
    "linked_issue": None, "confidence": 0.8,
}, ensure_ascii=False)

SHA = "b" * 40
TREE = "t" * 16


def _passed_validation() -> ValidationReport:
    return ValidationReport(status=SandboxStatus.passed, repro_passed=True, regression_passed=True)


def test_approval_requires_approver():
    with pytest.raises(Exception):
        Approval(approver="", sha=SHA)


def test_valid_approvals_filters_stale_and_duplicates():
    approvals = [
        {"approver": "alice", "sha": SHA, "tree_hash": TREE},
        {"approver": "alice", "sha": SHA, "tree_hash": TREE},
        {"approver": "bob", "sha": "c" * 40, "tree_hash": TREE},   # 旧 commit
        {"approver": "carol", "sha": SHA, "tree_hash": "x" * 16},  # 旧 tree
    ]
    valid = valid_approvals(approvals, head_sha=SHA, current_tree_hash=TREE)
    assert [item["approver"] for item in valid] == ["alice"]


def test_merge_blockers_counts_approvals_for_tier():
    policy = Policy()
    common = dict(
        pr_open=True, is_draft=False, mergeable=True, head_sha=SHA, reviewed_sha=SHA,
        validation=_passed_validation(),
        review=ReviewDecision(verdict=ReviewVerdict.approve),
        changed_paths=["src/auth/login.py"], policy=policy, merge_mode=MergeMode.auto,
        reviewed_tree_hash=TREE, current_tree_hash=TREE, tier="T2",
        min_approvals=2, require_non_author=True, author="alice",
    )
    one = merge_blockers(approvals=[{"approver": "alice", "sha": SHA, "tree_hash": TREE}], **common)
    assert any("需要 2 名人工批准" in b for b in one)

    two_same_author = merge_blockers(
        approvals=[
            {"approver": "alice", "sha": SHA, "tree_hash": TREE},
            {"approver": "alice", "sha": SHA, "tree_hash": TREE},
        ], **common,
    )
    assert any("需要 2 名" in b for b in two_same_author)

    ok = merge_blockers(
        approvals=[
            {"approver": "alice", "sha": SHA, "tree_hash": TREE},
            {"approver": "bob", "sha": SHA, "tree_hash": TREE},
        ], **common,
    )
    assert not ok, ok


def test_zero_approval_tier_has_no_human_blocker():
    policy = Policy()
    blockers = merge_blockers(
        pr_open=True, is_draft=False, mergeable=True, head_sha=SHA, reviewed_sha=SHA,
        validation=_passed_validation(),
        review=ReviewDecision(verdict=ReviewVerdict.approve),
        changed_paths=["docs/guide.md"], policy=policy, merge_mode=MergeMode.auto,
        reviewed_tree_hash=TREE, current_tree_hash=TREE, tier="T0", min_approvals=0,
        approvals=[],
    )
    assert not any("人工" in b for b in blockers), blockers


def _run_t2(tmp_path: Path):
    base, head = make_fixture_repo(tmp_path)
    cfg = load_settings(
        data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ck.db",
        sandbox_backend="fake", merge_mode="auto",
    )
    provider = FakeProvider({
        "issue_verifier": [CHANGE_CLAIM],
        "semantic_detector": [SEMANTIC_EMPTY],
        "reviewer": [REVIEW_APPROVE],
    })
    github = FakeGitHub()
    nodes = ReviewNodes(cfg, llm_provider=provider, github=github, sandbox=FakeSandbox())
    runtime = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ck.db"))
    pr = PullRequestContext(
        repo="acme/repo", number=31, base_ref="main", base_sha="a" * 40,
        head_ref="feat/auth", head_sha=SHA, title="feat: 认证调整", body="调整登录流程",
        author="alice",
        changed_files=[FileChange(path="src/auth/login.py"), FileChange(path="calc.py")],
    )
    return runtime, github, pr, head, base


def test_t2_requires_two_approvals_including_non_author(tmp_path: Path):
    runtime, github, pr, head, base = _run_t2(tmp_path)
    snap = runtime.start(pr, "job-t2", repo_root=str(head), base_root=str(base))
    assert not snap["done"]
    assert snap["values"]["risk_tier"]["tier"] == "T2"
    # 作者自批：人数与“非作者”条件都不满足，继续停在审批
    snap = runtime.resume("job-t2", {"approved": True, "approver": "alice"})
    assert not snap["done"]
    # 第二人是同事：满足 2 人且含非作者
    snap = runtime.resume("job-t2", {"approved": True, "approver": "bob"})
    assert snap["done"], snap["values"].get("merge_decision")
    assert snap["values"]["outcome"] == "MERGED"
    assert github.merged
