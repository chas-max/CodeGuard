"""change profile：任意变更无需复现证据，流程可走到合并。"""
from __future__ import annotations

import json
from pathlib import Path

from codeguard.config import load_settings
from codeguard.engine.profile import resolve_profile
from codeguard.engine.policy import Policy
from codeguard.graph import make_checkpointer
from codeguard.models import FileChange, PullRequestContext
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

FEATURE_CLAIM = json.dumps({
    "kind": "feature",
    "statement": "新增导出 CSV 功能",
    "expected": "导出文件包含表头与数据行",
    "actual": "当前没有导出能力",
    "repro_steps": [],
    "affected_paths": ["calc.py"],
    "linked_issue": None,
    "confidence": 0.8,
}, ensure_ascii=False)


def _cfg(tmp_path: Path):
    return load_settings(
        data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ck.db",
        sandbox_backend="fake", merge_mode="auto",
    )


def test_resolve_profile_precedence():
    internal = Policy()
    assert resolve_profile(internal, explicit="bugfix") == "bugfix"
    assert resolve_profile(internal, labels=["codeguard:bugfix"]) == "bugfix"
    assert resolve_profile(internal) == "change"
    external = Policy.model_validate({"mode": "external"})
    assert resolve_profile(external) == "bugfix"
    assert resolve_profile(internal, explicit="", labels=["docs"]) == "change"


def test_feature_pr_skips_reproduction_and_merges(tmp_path: Path):
    base, head = make_fixture_repo(tmp_path)
    cfg = _cfg(tmp_path)
    provider = FakeProvider({
        "issue_verifier": [FEATURE_CLAIM],
        "semantic_detector": [SEMANTIC_EMPTY],
        "reviewer": [REVIEW_APPROVE],
    })
    sandbox = FakeSandbox()
    github = FakeGitHub()
    nodes = ReviewNodes(cfg, llm_provider=provider, github=github, sandbox=sandbox)
    runtime = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ck.db"))
    pr = PullRequestContext(
        repo="acme/repo", number=21,
        base_ref="main", base_sha="a" * 40, head_ref="feat/csv", head_sha="b" * 40,
        title="feat: 导出 CSV", body="新增导出 CSV 功能", author="dev",
        changed_files=[FileChange(path="calc.py")],
    )
    snap = runtime.start(pr, "job-change", repo_root=str(head), base_root=str(base))
    values = snap["values"]
    assert values["profile"] == "change"
    assert values["intent_verification"]["status"] == "SUFFICIENT"
    # change profile 不跑 base 复现
    assert all(spec.workspace.name != "base" for spec in sandbox.specs)
    assert values["risk_tier"]["tier"] == "T1"
    assert not snap["done"], values.get("merge_decision")
    snap = runtime.resume("job-change", {"approved": True, "approver": "reviewer-b"})
    assert snap["values"]["outcome"] == "MERGED"
    assert github.merged


def test_change_profile_without_tests_is_not_blocked(tmp_path: Path):
    base = tmp_path / "base"
    head = tmp_path / "head"
    for root in (base, head):
        (root / "docs").mkdir(parents=True)
        (root / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")
    (base / ".codeguard").mkdir()
    (base / ".codeguard" / "policy.yml").write_text("trusted_authors: [dev]\n", encoding="utf-8")
    cfg = _cfg(tmp_path)
    provider = FakeProvider({
        "issue_verifier": [FEATURE_CLAIM],
        "semantic_detector": [SEMANTIC_EMPTY],
        "reviewer": [REVIEW_APPROVE],
    })
    nodes = ReviewNodes(cfg, llm_provider=provider, github=FakeGitHub(), sandbox=FakeSandbox())
    runtime = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ck.db"))
    pr = PullRequestContext(
        repo="acme/repo", number=22, base_ref="main", base_sha="a" * 40,
        head_ref="docs", head_sha="b" * 40, title="docs: 更新指南", body="补文档",
        author="dev", changed_files=[FileChange(path="docs/guide.md")],
    )
    snap = runtime.start(pr, "job-docs", repo_root=str(head), base_root=str(base))
    values = snap["values"]
    assert values["profile"] == "change"
    assert values["validation_report"]["status"] == "passed", values.get("validation_report")
    assert values["risk_tier"]["tier"] == "T0"

def test_validation_uses_policy_commands(tmp_path: Path):
    from codeguard.nodes.pipeline import _test_command

    template = Policy.model_validate(
        {"sandbox": {"test_command_template": "pytest -q {tests}"}}
    )
    assert _test_command(template, ["tests/test_a.py"]) == "pytest -q tests/test_a.py"
    assert _test_command(Policy(), []) == "python -m pytest -q"

    base, head = make_fixture_repo(tmp_path)
    (base / ".codeguard").mkdir()
    (base / ".codeguard" / "policy.yml").write_text(
        "sandbox:\n"
        "  test_command_template: 'pytest -q {tests}'\n"
        "  checks:\n"
        "    - {name: lint, command: 'ruff check .', required: true}\n",
        encoding="utf-8",
    )
    cfg = _cfg(tmp_path)
    provider = FakeProvider({
        "issue_verifier": [FEATURE_CLAIM],
        "semantic_detector": [SEMANTIC_EMPTY],
        "reviewer": [REVIEW_APPROVE],
    })
    sandbox = FakeSandbox()
    nodes = ReviewNodes(cfg, llm_provider=provider, github=FakeGitHub(), sandbox=sandbox)
    runtime = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ck.db"))
    pr = PullRequestContext(
        repo="acme/repo", number=23, base_ref="main", base_sha="a" * 40,
        head_ref="feat", head_sha="b" * 40, title="feat", body="导出", author="dev",
        changed_files=[FileChange(path="calc.py")],
    )
    runtime.start(pr, "job-cmd", repo_root=str(head), base_root=str(base))
    commands = [spec.commands[0] for spec in sandbox.specs]
    assert any(cmd.startswith("pytest -q") for cmd in commands), commands
    assert "ruff check ." in commands, commands
