"""mode: external 兼容性：保持 bugfix 复现门禁的历史行为。"""
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


def test_external_mode_forces_bugfix_profile_and_reproduction(tmp_path: Path):
    base, head = make_fixture_repo(tmp_path)
    (base / ".codeguard").mkdir()
    (base / ".codeguard" / "policy.yml").write_text("mode: external\n", encoding="utf-8")

    cfg = load_settings(
        data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ck.db",
        sandbox_backend="fake", merge_mode="auto",
    )
    provider = FakeProvider(v2_provider_map(pr_brief=PR_BRIEF_BUGFIX_JSON))
    sandbox = FakeSandbox()
    github = FakeGitHub()
    nodes = ReviewNodes(cfg, llm_provider=provider, github=github, sandbox=sandbox)
    runtime = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ck.db"))
    pr = PullRequestContext(
        repo="acme/repo", number=41, base_ref="main", base_sha="a" * 40,
        head_ref="fix", head_sha="b" * 40, title="fix: 除零", body="bug",
        changed_files=[FileChange(path="calc.py"), FileChange(path="tests/test_calc.py")],
    )
    snap = runtime.start(pr, "job-ext", repo_root=str(head), base_root=str(base))
    values = snap["values"]
    assert values["profile"] == "bugfix", values
    assert values["issue_verification"]["status"] == "CONFIRMED"
    assert any(spec.workspace.name == "base" for spec in sandbox.specs)
    if not snap["done"]:
        snap = runtime.resume("job-ext", {"approved": True, "approver": "reviewer-b"})
    assert snap["values"]["outcome"] == "MERGED"
