"""合并后归因与 CLI 指标/左移入口（S5）。"""
from __future__ import annotations

import json
from pathlib import Path

from codeguard import cli
from codeguard.engine.incidents import (
    build_fixture_stub,
    collect_incident,
    find_revert_pr,
    write_incident,
)
from tests.fakes import FakeGitHub, init_git

SHA = "d" * 40


def test_find_revert_pr_by_title_or_body():
    pulls = [
        {"number": 1, "title": "feat: something", "body": ""},
        {"number": 2, "title": f"Revert \"land {SHA[:8]}\"", "body": ""},
    ]
    found = find_revert_pr(pulls, SHA)
    assert found and found["number"] == 2
    assert find_revert_pr(pulls[:1], SHA) is None


def test_collect_incident_red_ci():
    github = FakeGitHub()
    github.ci_state = "failure"
    incident = collect_incident(github, repo="acme/repo", merged_sha=SHA)
    assert incident["incident"] is True
    assert incident["ci_state"] == "failure"


def test_collect_incident_revert_pr():
    github = FakeGitHub()
    github.pulls = [{"number": 3, "title": f"Revert {SHA[:8]}", "body": "", "html_url": ""}]
    incident = collect_incident(github, repo="acme/repo", merged_sha=SHA)
    assert incident["incident"] is True
    assert incident["revert_pr"]["number"] == 3


def test_incident_artifacts(tmp_path: Path):
    github = FakeGitHub()
    incident = collect_incident(github, repo="acme/repo", merged_sha=SHA)
    assert incident["incident"] is False
    path = write_incident(tmp_path / "incidents", incident)
    assert path.is_file()
    stub = build_fixture_stub(incident)
    assert stub["source"] == "incident"
    assert stub["expected"]["block"] is True


def test_cli_stats(tmp_path: Path, capsys):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "a.json").write_text(json.dumps({
        "outcome": "MERGED", "approvals": 1, "required_approvals": 1,
        "approval_latency_sec": 60, "review_rounds": 1, "cost_cny": 0.2,
        "finding_outcomes": {"f1": "fixed"},
    }), encoding="utf-8")
    code = cli.main(["stats", "--dir", str(jobs), "--out", str(tmp_path / "metrics")])
    assert code == 0
    assert (tmp_path / "metrics" / "metrics.json").is_file()
    assert (tmp_path / "metrics" / "metrics.md").is_file()


def test_changed_from_git(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    init_git(root)
    (root / "calc.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    changed = cli._changed_from_git(root, "HEAD")
    assert changed == ["calc.py"]
    assert cli._changed_from_git(root, "no-such-ref") == []
