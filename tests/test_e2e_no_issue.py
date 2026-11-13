from __future__ import annotations

from pathlib import Path

from codeguard.config import load_settings
from codeguard.graph import make_checkpointer
from codeguard.models import FileChange, PullRequestContext, SandboxStatus
from codeguard.nodes import ReviewNodes
from codeguard.runtime import CodeGuardRuntime
from tests.fakes import CLAIM_JSON, SEMANTIC_EMPTY, FakeGitHub, FakeProvider, FakeSandbox, make_fixture_repo


def test_no_issue_rejected_with_evidence(tmp_path: Path):
    base, head = make_fixture_repo(tmp_path)
    cfg = load_settings(data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ck.db",
                        sandbox_backend="fake")
    provider = FakeProvider({
        "issue_verifier": [CLAIM_JSON],
        "semantic_detector": [SEMANTIC_EMPTY],
    })
    github = FakeGitHub()

    def handler(spec):
        return type("R", (), {"status": SandboxStatus.passed, "exit_code": 0, "stdout": "passed"})()

    nodes = ReviewNodes(cfg, llm_provider=provider, github=github, sandbox=FakeSandbox(handler=handler))
    runtime = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ck.db"))
    pr = PullRequestContext(
        repo="acme/repo", number=8,
        base_ref="main", base_sha="a" * 40, head_ref="fix", head_sha="b" * 40,
        title="fix", body="bug", profile="bugfix",
        changed_files=[FileChange(path="calc.py")],
    )
    snap = runtime.start(pr, "job-noissue", repo_root=str(head), base_root=str(base))
    values = snap["values"]
    assert snap["done"]
    assert values["outcome"] == "REJECTED_NO_ISSUE", values
    # 结论通过 review 发布；没有 review 能力时退回 issue 评论
    assert github.reviews or github.comments
