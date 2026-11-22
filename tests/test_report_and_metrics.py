"""review 发布幂等、job record 与指标聚合（S4）。"""
from __future__ import annotations

import json
from pathlib import Path

from codeguard.config import load_settings
from codeguard.engine.metrics import render_markdown, summarize
from codeguard.github.report import build_summary, publish_review
from codeguard.graph import make_checkpointer
from codeguard.models import FileChange, PullRequestContext
from codeguard.nodes import ReviewNodes
from codeguard.runtime import CodeGuardRuntime
from tests.fakes import (
    PR_BRIEF_MEDIUM_JSON,
    FakeGitHub,
    FakeProvider,
    FakeSandbox,
    make_fixture_repo,
    v2_provider_map,
)

CLAIM = json.dumps({
    "kind": "feature", "statement": "导出 CSV", "expected": "有表头",
    "actual": "无", "repro_steps": [], "affected_paths": ["calc.py"],
    "linked_issue": None, "confidence": 0.8,
}, ensure_ascii=False)

BLOCKER = {
    "id": "f1", "source": "semantic", "category": "correctness", "severity": "major",
    "blocking": True, "path": "src/a.py", "line": 12, "message": "空指针",
    "evidence": "src/a.py:12", "suggestion": "加判空", "introduced_by_pr": True,
}


def test_publish_review_is_idempotent():
    github = FakeGitHub()
    kwargs = dict(
        repo="acme/repo", number=7, head_sha="b" * 40, job_id="j1",
        findings=[BLOCKER], outcome="CHANGES_REQUESTED", tier="T1", required_approvals=1,
        verdict="request_changes",
    )
    first = publish_review(github, **kwargs)
    assert first == {"created": 1, "updated": 0, "skipped": 0, "summary_posted": True}
    second = publish_review(github, **kwargs)
    assert second["created"] == 0 and second["skipped"] == 1

    changed = {**BLOCKER, "suggestion": "改成 raise 或返回默认值"}
    third = publish_review(github, **{**kwargs, "findings": [changed]})
    assert third["updated"] == 1
    assert len(github.review_comments) == 1
    # 机器人只发 COMMENT，不冒充人工审批
    assert all(review["event"] == "COMMENT" for review in github.reviews)


def test_summary_counts_and_no_issue_message():
    summary = build_summary(
        job_id="j1", outcome="MERGED", profile="change", tier="T0", required_approvals=1,
        findings=[{**BLOCKER, "blocking": False, "severity": "minor"}],
        verdict="approve",
    )
    assert "0 critical, 1 major" not in summary  # 该 finding 是 minor
    assert "1 minor" in summary
    assert "未发现 blocking 问题" in summary


def test_metrics_aggregate(tmp_path: Path):
    records = [
        {"outcome": "MERGED", "approvals": 0, "required_approvals": 0,
         "approval_latency_sec": None, "review_rounds": 1, "cost_cny": 0.1,
         "finding_outcomes": {"f1": "fixed"}},
        {"outcome": "MERGED", "approvals": 1, "required_approvals": 1,
         "approval_latency_sec": 120.0, "review_rounds": 2, "cost_cny": 0.3,
         "finding_outcomes": {"f2": "dismissed"}},
        {"outcome": "CHANGES_REQUESTED", "approvals": 1, "required_approvals": 1,
         "approval_latency_sec": 240.0, "review_rounds": 2, "cost_cny": 0.2,
         "finding_outcomes": {"f3": "fixed"}},
    ]
    report = summarize(records)
    assert report["total"] == 3
    assert report["merged"] == 2
    assert report["auto_merge_share"] == 0.5
    assert report["findings_precision"] == 0.667
    assert report["human_actions_per_pr"]["median"] == 1
    assert "CodeGuard 指标" in render_markdown(report)


def test_finalize_writes_job_record(tmp_path: Path):
    base, head = make_fixture_repo(tmp_path)
    reports = tmp_path / "reports"
    cfg = load_settings(
        data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ck.db",
        sandbox_backend="fake", merge_mode="auto", reports_dir=reports,
    )
    provider = FakeProvider(v2_provider_map(pr_brief=PR_BRIEF_MEDIUM_JSON))
    nodes = ReviewNodes(cfg, llm_provider=provider, github=FakeGitHub(), sandbox=FakeSandbox())
    runtime = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ck.db"))
    pr = PullRequestContext(
        repo="acme/repo", number=51, base_ref="main", base_sha="a" * 40,
        head_ref="feat", head_sha="b" * 40, title="feat", body="导出", author="dev",
        changed_files=[FileChange(path="calc.py")],
    )
    snap = runtime.start(pr, "job-record", repo_root=str(head), base_root=str(base))
    if not snap["done"]:
        snap = runtime.resume("job-record", {"approved": True, "approver": "bob"})
    record_path = reports / "jobs" / "job-record.json"
    assert record_path.is_file()
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["outcome"] == "MERGED"
    assert record["profile"] == "change"
    assert record["tier"] == "T1"
    assert record["approvals"] == 1
    assert record["approvers"] == ["bob"]
