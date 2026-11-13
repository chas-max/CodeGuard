"""评分卡与优化排序口径：P/R、false merge/block、Wilson、跳过不计分。"""
from __future__ import annotations

from eval.report_optimization import build as build_optimization
from eval.scorecard import compare, render_markdown, scorecard, wilson


def _row(**overrides) -> dict:
    base = {
        "case_id": "case", "layer": "det", "repeat": 0, "done": True, "passed": True,
        "skipped": False, "skip_reason": "", "blocked": False,
        "blocking_rules": [], "all_rules": [], "semantic_blocked": False,
        "tier": "", "approvals": 0, "expected_approvals": None,
        "outcome": "", "merge_called": False, "validation_status": "",
        "review_rounds": 0, "llm_calls": 0, "tool_calls": 0, "cost_cny": 0.0,
        "wall_ms": 1, "expected_block": False, "expected_rules": [],
        "expected_tier": "", "expected_semantic": False, "probe": "",
        "findings_total": 0, "detail": "", "error": "", "tags": [], "weight": 1.0,
    }
    base.update(overrides)
    return base


def test_wilson_interval():
    assert wilson(0, 0) == (0.0, 0.0)
    low, high = wilson(10, 10)
    assert 0.6 < low <= 1.0 and high == 1.0
    low, high = wilson(5, 10)
    assert low < 0.5 < high


def test_det_metrics_count_false_merge_and_block():
    rows = [
        _row(case_id="a", expected_block=True, blocked=True,
             expected_rules=["SYN-001"], all_rules=["SYN-001"]),
        _row(case_id="b", expected_block=True, blocked=False,
             expected_rules=["API-001"], all_rules=[]),
        _row(case_id="c", expected_block=False, blocked=True, all_rules=["API-001"]),
    ]
    card = scorecard(rows)
    det = card["dimensions"]["det"]
    assert det["false_merge"] == 1
    assert det["false_block"] == 1
    assert det["cases"] == 3
    assert det["per_rule"]["API-001"] == {"tp": 0, "fp": 1, "fn": 1,
                                          "precision": 0.0, "recall": 0.0, "f1": 0.0}
    assert det["per_rule"]["SYN-001"]["f1"] == 1.0


def test_semantic_skipped_is_not_scored():
    skipped_only = scorecard([
        _row(case_id="s1", layer="semantic", skipped=True,
             skip_reason="no provider", expected_block=True),
    ])
    assert skipped_only["dimensions"]["semantic"]["score"] is None
    assert "semantic" not in skipped_only["composite"]["weights"]

    with_live_case = scorecard([
        _row(case_id="s1", layer="semantic", skipped=True,
             skip_reason="no provider", expected_block=True),
        _row(case_id="s2", layer="semantic", expected_block=True, semantic_blocked=False,
             passed=False),
    ])
    semantic = with_live_case["dimensions"]["semantic"]
    assert semantic["skipped"] == 1
    assert semantic["score"] == 0.0
    assert "semantic" in with_live_case["composite"]["weights"]


def test_gate_and_security_metrics():
    rows = [
        _row(case_id="t1", layer="gate", probe="tier", tier="T2",
             expected_tier="T2", approvals=2, expected_approvals=2),
        _row(case_id="t2", layer="gate", probe="tier", tier="T1",
             expected_tier="T2", approvals=1, expected_approvals=2),
        _row(case_id="m1", layer="gate", probe="merge", blocked=True, expected_block=True),
        _row(case_id="s1", layer="security", probe="sandbox_kwargs", passed=False),
    ]
    card = scorecard(rows)
    assert card["dimensions"]["gate"]["tier_accuracy"]["success"] == 1
    assert card["dimensions"]["gate"]["merge_gate_accuracy"]["success"] == 1
    assert card["dimensions"]["security"]["score"] == 0.0
    assert card["dimensions"]["security"]["failures"][0]["case_id"] == "s1"


def test_e2e_cost_and_rounds():
    rows = [
        _row(case_id="e1", layer="e2e", merge_called=True, outcome="MERGED",
             cost_cny=0.2, wall_ms=1200, review_rounds=2),
        _row(case_id="e2", layer="e2e", expected_block=True, merge_called=True,
             blocked=False, passed=False, cost_cny=0.4, wall_ms=800),
    ]
    card = scorecard(rows)
    e2e = card["dimensions"]["e2e"]
    assert e2e["false_merge"] == 1
    assert e2e["cost_cny"]["total"] == 0.6
    assert e2e["review_rounds"]["max"] == 2


def test_compare_reports_delta():
    base = scorecard([_row(case_id="a", expected_block=True, blocked=True,
                           expected_rules=["SYN-001"], all_rules=["SYN-001"])])
    current = scorecard([_row(case_id="a", expected_block=True, blocked=False,
                              expected_rules=["SYN-001"], all_rules=[])])
    delta = compare(current, base)
    assert delta["det"]["delta"] < 0
    assert "composite" in delta


def test_markdown_renders_key_sections():
    card = scorecard([_row(case_id="a")])
    text = render_markdown(card)
    assert "# CodeGuard 基线评分卡" in text
    assert "每规则 P/R" in text


def test_optimization_prioritizes_security_gap():
    rows = [
        _row(case_id="security.merge.bot_approval_not_counted", layer="security",
             expected_block=True, blocked=False, passed=False,
             tags=["security", "gap"], detail="valid=1"),
        _row(case_id="det.clean.typing_only", passed=False, expected_block=False,
             blocked=True, all_rules=["API-001"], tags=["gap"], detail="fp"),
    ]
    card = scorecard(rows)
    report = build_optimization(card, rows)
    assert report["items"][0]["priority"] == "P0"
    assert any(item["id"] == "api_precision" for item in report["items"])
