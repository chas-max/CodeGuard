"""findings 生命周期、聚合与噪声截断（S1）。"""
from __future__ import annotations

from codeguard.engine.findings import (
    apply_nit_cap,
    blocking_findings,
    finding_key,
    group_by_class,
    open_findings,
    reconcile,
)


def _f(fid: str, *, blocking: bool = True, severity: str = "major", **extra) -> dict:
    item = {
        "id": fid, "rule_id": "", "category": "cross_file", "severity": severity,
        "blocking": blocking, "path": "src/a.py", "line": 1, "message": f"问题 {fid}",
        "evidence": "evidence",
    }
    item.update(extra)
    return item


def test_reconcile_marks_disappeared_findings_resolved():
    previous = [_f("a"), _f("b")]
    current = [_f("a")]
    findings, resolved = reconcile(previous, current)
    by_id = {item["id"]: item for item in findings}
    assert by_id["a"]["lifecycle"] == "open"
    assert by_id["b"]["lifecycle"] == "resolved"
    assert [item["id"] for item in resolved] == ["b"]
    assert blocking_findings(findings) == [by_id["a"]]


def test_reconcile_is_idempotent_and_does_not_rerecord_history():
    previous = [_f("a"), _f("b")]
    current = [_f("a")]
    findings, _ = reconcile(previous, current)
    findings2, resolved2 = reconcile(findings, current)
    assert resolved2 == []
    assert {item["id"] for item in findings2} == {"a"}


def test_resolved_findings_are_not_open():
    findings, _ = reconcile([_f("a")], [])
    assert findings[0]["lifecycle"] == "resolved"
    assert open_findings(findings) == []
    assert blocking_findings(findings) == []


def test_group_by_class_aggregates_instances():
    items = [
        _f("a1", rule_id="SEC-002", path="src/a.py", line=10),
        _f("a2", rule_id="SEC-002", path="src/b.py", line=20, severity="critical"),
        _f("c1", rule_id="TEST-001", severity="major"),
    ]
    groups = {group["class_id"]: group for group in group_by_class(items)}
    assert set(groups) == {"SEC-002", "TEST-001"}
    sec = groups["SEC-002"]
    assert sec["instance_count"] == 2
    assert sec["paths"] == ["src/a.py", "src/b.py"]
    assert sec["severity"] == "critical"  # 取最严重
    assert sec["path"] == "src/a.py"


def test_group_by_class_ignores_resolved_and_falls_back_to_category():
    items = [_f("a"), {**_f("b"), "lifecycle": "resolved"}, _f("c", rule_id="")]
    groups = group_by_class(items)
    ids = {group["class_id"] for group in groups}
    assert "b" not in ids
    assert ids  # 无 rule_id 时按 category 聚合


def test_apply_nit_cap_keeps_blocking_and_limits_advisory():
    items = [
        _f("block", blocking=True, severity="critical"),
        _f("n1", blocking=False, severity="major"),
        _f("n2", blocking=False, severity="minor"),
        _f("n3", blocking=False, severity="info"),
    ]
    kept = {finding_key(item) for item in apply_nit_cap(items, 2)}
    assert "block" in kept
    assert len(kept) == 3  # blocking + 2 个最严重的 advisory
    assert "n3" not in kept


def test_finding_key_prefers_id():
    assert finding_key({"id": "x", "rule_id": "R"}) == "x"
    assert "R" in finding_key({"rule_id": "R", "path": "p", "line": 1})
