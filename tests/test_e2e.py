"""端到端 fake 流程：问题存在 + 代码可接受 → 合并。"""
from __future__ import annotations

from pathlib import Path

from codeguard.config import load_settings
from codeguard.graph import make_checkpointer
from codeguard.models import FileChange, PullRequestContext
from codeguard.nodes import ReviewNodes
from codeguard.runtime import CodeGuardRuntime
from tests.fakes import (
    PR_BRIEF_BUGFIX_JSON,
    FakeGitHub,
    FakeProvider,
    FakeSandbox,
    make_fixture_repo,
    v2_provider_map,
)


def _pr() -> PullRequestContext:
    return PullRequestContext(
        repo="acme/repo", number=7,
        base_ref="main", base_sha="a" * 40,
        head_ref="fix/zero", head_sha="b" * 40,
        title="fix: divide by zero",
        body="divide_safe(10,0) 抛 ZeroDivisionError，期望返回 0.0",
        author="dev",
        profile="bugfix",
        changed_files=[
            FileChange(path="calc.py", status="modified"),
            FileChange(path="tests/test_calc.py", status="modified"),
        ],
    )


def test_valid_fix_merges(tmp_path: Path):
    base, head = make_fixture_repo(tmp_path)
    cfg = load_settings(
        data_dir=tmp_path / "data",
        checkpoint_path=tmp_path / "ckpt.db",
        sandbox_backend="fake",
        merge_mode="auto",
    )
    provider = FakeProvider(v2_provider_map(pr_brief=PR_BRIEF_BUGFIX_JSON))
    github = FakeGitHub()
    nodes = ReviewNodes(cfg, llm_provider=provider, github=github, sandbox=FakeSandbox())
    runtime = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ckpt.db"))
    snap = runtime.start(_pr(), "job-e2e", repo_root=str(head), base_root=str(base))
    if not snap["done"]:
        snap = runtime.resume("job-e2e", {"approved": True, "approver": "reviewer-b"})
    values = snap["values"]
    assert snap["done"], values.get("error")
    assert values.get("outcome") == "MERGED", values
    assert github.merged
