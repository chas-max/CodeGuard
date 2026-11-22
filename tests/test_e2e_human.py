from __future__ import annotations

from pathlib import Path

from codeguard.config import load_settings
from codeguard.graph import make_checkpointer
from codeguard.models import FileChange, PullRequestContext, SandboxStatus
from codeguard.nodes import ReviewNodes
from codeguard.runtime import CodeGuardRuntime
from codeguard.sandbox.models import SandboxResult
from tests.fakes import (
    PR_BRIEF_CRITICAL_JSON,
    FakeGitHub,
    FakeProvider,
    FakeSandbox,
    make_fixture_repo,
    v2_provider_map,
)


def test_high_risk_requires_human_then_merges(tmp_path: Path):
    base, head = make_fixture_repo(tmp_path)
    cfg = load_settings(data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ck.db",
                        sandbox_backend="fake", merge_mode="auto")
    provider = FakeProvider(v2_provider_map(pr_brief=PR_BRIEF_CRITICAL_JSON))
    github = FakeGitHub()

    def handler(spec):
        if spec.workspace.name == "base":
            return SandboxResult(status=SandboxStatus.failed, exit_code=1, stdout="base failed")
        return SandboxResult(status=SandboxStatus.passed, exit_code=0, stdout="passed")

    nodes = ReviewNodes(cfg, llm_provider=provider, github=github, sandbox=FakeSandbox(handler=handler))
    runtime = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ck.db"))
    pr = PullRequestContext(
        repo="acme/repo", number=11,
        base_ref="main", base_sha="a" * 40, head_ref="fix", head_sha="b" * 40,
        title="fix", body="bug",
        changed_files=[FileChange(path="src/auth/login.py"), FileChange(path="tests/test_calc.py")],
    )
    snap = runtime.start(pr, "job-human", repo_root=str(head), base_root=str(base))
    assert not snap["done"], {
        k: snap["values"].get(k)
        for k in ("outcome", "status", "error", "profile", "risk_tier", "merge_decision")
    }
    assert (snap.get("interrupt") or {}).get("type") == "human_approval"
    snap2 = runtime.resume("job-human", {"approved": True, "feedback": "", "approver": "alice"})
    # T2 需要 2 名审批（含非作者）
    assert not snap2["done"], snap2
    snap3 = runtime.resume("job-human", {"approved": True, "feedback": "", "approver": "bob"})
    assert snap3["done"]
    assert snap3["values"]["outcome"] == "MERGED"
    assert github.merged
