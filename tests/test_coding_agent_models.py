"""CodeGuard 强类型模型与 Keyed Reducers 契约测试（PATH_A Task 1）。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from codeguard.coding_agent.models import (
    PatchUnit,
    SandboxTestResult,
    StaticAuditVerdict,
    SymbolContract,
    merge_patch_units,
    merge_symbol_contracts,
)


def test_merge_patch_units_idempotent_by_unit_id():
    left = [
        {"unit_id": "U001", "target_file": "src/service.py", "status": "pending", "attempts": 1},
        {"unit_id": "U002", "target_file": "src/utils.py", "status": "passed", "attempts": 1},
    ]
    right = [
        {"unit_id": "U001", "status": "passed", "attempts": 2, "f2p_passed": True},
    ]
    merged = merge_patch_units(left, right)
    assert len(merged) == 2
    u1 = next(item for item in merged if item["unit_id"] == "U001")
    assert u1["target_file"] == "src/service.py"
    assert u1["status"] == "passed"
    assert u1["attempts"] == 2
    assert u1["f2p_passed"] is True
    # 幂等：同一批再合并一次，结果不变
    assert merge_patch_units(merged, right) == merged


def test_merge_patch_units_empty_and_missing_id():
    assert merge_patch_units([], []) == []
    assert merge_patch_units(None, None) == []
    # 无 unit_id 的条目被忽略
    assert merge_patch_units([], [{"status": "pending"}]) == []


def test_symbol_contract_validation():
    contract = SymbolContract(
        symbol_id="SYM_001",
        file_path="src/budget.py",
        symbol_name="total_cost_cny",
        kind="function",
        arg_names=["state"],
        return_annotation="float",
        callers=["src/budget.py:refresh_budget_exceeded"],
        frozen_signature=True,
    )
    assert contract.symbol_name == "total_cost_cny"
    assert contract.frozen_signature is True
    # 必填字段缺失应报校验错误
    with pytest.raises(ValidationError):
        SymbolContract(symbol_id="SYM_002", file_path="a.py")


def test_patch_unit_defaults():
    unit = PatchUnit(unit_id="U001", target_file="pkg/calc.py", test_file="test_calc.py")
    assert unit.status == "pending"
    assert unit.attempts == 0
    assert unit.f2p_passed is False
    assert unit.p2p_passed is False
    assert unit.patched_code == ""


def test_static_audit_verdict_categories():
    v = StaticAuditVerdict(passed=False, category="syntax_error", severity="critical")
    assert v.passed is False
    assert v.issues == []
    v2 = StaticAuditVerdict(passed=True)
    assert v2.category == "none"
    assert v2.severity == "none"


def test_sandbox_test_result():
    r = SandboxTestResult(f2p_passed=True, p2p_passed=True, resolved=True)
    assert r.branch_coverage_pct == 0.0
    assert r.error_summary == ""


def test_merge_symbol_contracts_partial_update():
    left = {
        "SYM_001": {"symbol_id": "SYM_001", "symbol_name": "foo", "arg_names": ["a"]},
        "SYM_002": {"symbol_id": "SYM_002", "symbol_name": "bar", "arg_names": ["b"]},
    }
    right = {"SYM_001": {"callers": ["x.py:main"]}}
    merged = merge_symbol_contracts(left, right)
    assert merged["SYM_001"]["symbol_name"] == "foo"
    assert merged["SYM_001"]["callers"] == ["x.py:main"]
    assert merged["SYM_002"]["symbol_name"] == "bar"
    # 原字典不被修改（不可变语义）
    assert "callers" not in left["SYM_001"]
