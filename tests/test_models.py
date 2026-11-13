from __future__ import annotations

import pytest
from pydantic import ValidationError

from codeguard.models import (
    DeterministicReport,
    Finding,
    FindingCategory,
    FindingSource,
    HybridScanReport,
    IssueStatus,
    IssueVerification,
    MergeDecision,
    Outcome,
    PullRequestContext,
    ReproEvidence,
    ReviewDecision,
    ReviewVerdict,
    SemanticFinding,
    SemanticReport,
    Severity,
    validate_repo_path,
)


def test_repo_path_validation():
    assert validate_repo_path("src/a.py") == "src/a.py"
    assert validate_repo_path("src\\a.py") == "src/a.py"
    for bad in ("/etc/passwd", "C:\\x", "../x", "a/../../b"):
        with pytest.raises(ValueError):
            validate_repo_path(bad)


def test_pull_request_context_rejects_bad_repo_and_sha():
    with pytest.raises(ValidationError):
        PullRequestContext(repo="bad", number=1)
    with pytest.raises(ValidationError):
        PullRequestContext(repo="a/b", number=1, head_sha="zzzz")


def test_blocking_finding_requires_evidence():
    with pytest.raises(ValidationError):
        Finding(rule_id="X", blocking=True, message="bad")


def test_semantic_finding_requires_grounding():
    with pytest.raises(ValidationError):
        SemanticFinding(paths=[], evidence="x", message="bad")
    with pytest.raises(ValidationError):
        SemanticFinding(paths=["a.py", "b.py"], evidence="x", message="bad")
    f = SemanticFinding(paths=["a.py"], evidence="a.py:1", message="x", confidence=0.9)
    assert f.source == FindingSource.semantic


def test_issue_verification_requires_evidence():
    with pytest.raises(ValidationError):
        IssueVerification(status=IssueStatus.confirmed, reason="x")
    ev = ReproEvidence(base_exit_code=1, head_exit_code=0, base_failed=True, head_passed=True)
    v = IssueVerification(status=IssueStatus.confirmed, evidence=[ev], reason="ok")
    assert v.status == IssueStatus.confirmed
    with pytest.raises(ValidationError):
        IssueVerification(status=IssueStatus.not_reproduced, evidence=[], reason="x")


def test_review_approve_cannot_have_major_blocking():
    bad = Finding(rule_id="X", severity=Severity.major, blocking=True,
                  message="bad", evidence="x")
    with pytest.raises(ValidationError):
        ReviewDecision(verdict=ReviewVerdict.approve, findings=[bad])


def test_merge_decision_requires_sha_for_merged():
    with pytest.raises(ValidationError):
        MergeDecision(outcome=Outcome.merged)
    assert MergeDecision(outcome=Outcome.merged, merge_commit_sha="abc").outcome == Outcome.merged


def test_hybrid_combine_keeps_deterministic_and_downgrades_low_confidence():
    det = DeterministicReport(findings=[
        Finding(rule_id="SYN-001", severity=Severity.critical, blocking=True,
                message="syntax", evidence="x"),
    ])
    sem = SemanticReport(findings=[
        SemanticFinding(category=FindingCategory.cross_file, severity=Severity.major,
                        confidence=0.4, paths=["a.py"], evidence="a.py:1", message="low"),
        SemanticFinding(category=FindingCategory.cross_file, severity=Severity.major,
                        confidence=0.95, paths=["a.py"], evidence="a.py:1", message="high"),
    ])
    report = HybridScanReport.combine(det, sem)
    assert len(report.findings) == 3
    low = [f for f in report.findings if f.message == "low"][0]
    high = [f for f in report.findings if f.message == "high"][0]
    assert low.blocking is False
    assert high.blocking is True
