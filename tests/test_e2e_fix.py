from __future__ import annotations

import json
from pathlib import Path

from codeguard.config import load_settings
from codeguard.engine.diff import unified_diff
from codeguard.graph import make_checkpointer
from codeguard.models import FileChange, PullRequestContext, SandboxStatus
from codeguard.nodes import ReviewNodes
from codeguard.runtime import CodeGuardRuntime
from codeguard.sandbox.models import SandboxResult
from tests.fakes import (
    PR_BRIEF_BUGFIX_JSON,
    FakeGitHub,
    FakeProvider,
    FakeSandbox,
    init_git,
    make_fixture_repo,
    v2_provider_map,
)


def test_fixer_edits_in_sandbox_then_merges(tmp_path: Path):
    """fixer 用 write_file 改副本 → 节点用 git diff 生成补丁 → 走既有门控。"""
    base, head = make_fixture_repo(tmp_path)
    (head / "calc.py").write_text((base / "calc.py").read_text(encoding="utf-8"), encoding="utf-8")
    init_git(head)
    fixed = (
        "def divide_safe(a: float, b: float) -> float:\n"
        "    if b == 0:\n"
        "        return 0.0\n"
        "    return a / b\n"
    )
    provider = FakeProvider(v2_provider_map(
        pr_brief=PR_BRIEF_BUGFIX_JSON,
        rounds=2,
        overrides={
            "coder": [
                json.dumps({
                    "tool": "write_file",
                    "args": {"path": "calc.py", "content": fixed},
                }, ensure_ascii=False),
                json.dumps({
                    "patch": "", "files": ["calc.py"],
                    "explanation": "补除零保护", "confidence": 0.9,
                }, ensure_ascii=False),
            ],
        },
    ))
    github = FakeGitHub()

    def handler(spec):
        name = spec.workspace.name
        if name in ("base", "head"):
            return SandboxResult(status=SandboxStatus.failed, exit_code=1, stdout="failed")
        return SandboxResult(status=SandboxStatus.passed, exit_code=0, stdout="passed")

    cfg = load_settings(data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ck.db",
                        sandbox_backend="fake")
    nodes = ReviewNodes(cfg, llm_provider=provider, github=github, sandbox=FakeSandbox(handler=handler))
    runtime = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ck.db"))
    pr = PullRequestContext(
        repo="acme/repo", number=10,
        base_ref="main", base_sha="a" * 40, head_ref="fix", head_sha="b" * 40,
        title="fix", body="bug", profile="bugfix",
        changed_files=[FileChange(path="calc.py"), FileChange(path="tests/test_calc.py")],
    )
    snap = runtime.start(pr, "job-fix-tools", repo_root=str(head), base_root=str(base))
    if not snap["done"]:
        snap = runtime.resume("job-fix-tools", {"approved": True, "approver": "reviewer-b"})
    values = snap["values"]
    assert snap["done"], values.get("error")
    assert values["outcome"] == "MERGED", values
    assert values["fix_round"] == 1
    assert values["tool_calls"] >= 1
    assert any(ev.get("tool") == "write_file" for ev in values["tool_events"])
    assert "return 0.0" in values["fix_patch"]
    # 修复后 review 输入必须反映修复树，而不是修复前的原始 diff（D1 回归）
    assert "return 0.0" in values["diff_text"]
    assert values["diff_stats"]["additions"] >= 2
    assert github.merged


def test_invalid_fix_is_repaired_then_merged(tmp_path: Path):
    base, head = make_fixture_repo(tmp_path)
    # 构造“PR 提交了无效修复”场景：head 仍是 buggy 版本。
    (head / "calc.py").write_text((base / "calc.py").read_text(encoding="utf-8"), encoding="utf-8")
    init_git(head)
    buggy = (head / "calc.py").read_text(encoding="utf-8")
    fixed = (
        "def divide_safe(a: float, b: float) -> float:\n"
        "    if b == 0:\n"
        "        return 0.0\n"
        "    return a / b\n"
    )
    patch = unified_diff(buggy, fixed, "calc.py")
    fix_json = json.dumps({
        "patch": patch, "files": ["calc.py"], "explanation": "补除零保护", "confidence": 0.9,
    }, ensure_ascii=False)

    cfg = load_settings(data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ck.db",
                        sandbox_backend="fake")
    provider = FakeProvider(v2_provider_map(
        pr_brief=PR_BRIEF_BUGFIX_JSON,
        rounds=2,
        overrides={"coder": [fix_json]},
    ))
    github = FakeGitHub()

    def handler(spec):
        name = spec.workspace.name
        if name == "base" or name == "head":
            return SandboxResult(status=SandboxStatus.failed, exit_code=1, stdout="failed")
        return SandboxResult(status=SandboxStatus.passed, exit_code=0, stdout="passed")

    nodes = ReviewNodes(cfg, llm_provider=provider, github=github, sandbox=FakeSandbox(handler=handler))
    runtime = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ck.db"))
    pr = PullRequestContext(
        repo="acme/repo", number=9,
        base_ref="main", base_sha="a" * 40, head_ref="fix", head_sha="b" * 40,
        title="fix", body="bug", profile="bugfix",
        changed_files=[FileChange(path="calc.py"), FileChange(path="tests/test_calc.py")],
    )
    snap = runtime.start(pr, "job-fix", repo_root=str(head), base_root=str(base))
    if not snap["done"]:
        snap = runtime.resume("job-fix", {"approved": True, "approver": "reviewer-b"})
    values = snap["values"]
    assert snap["done"], values.get("error")
    assert values["outcome"] == "MERGED", values
    assert values["fix_round"] == 1
    assert github.merged
