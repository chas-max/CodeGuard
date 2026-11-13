"""执行器契约：隔离、失败隔离、断点续跑、预算熔断、结果字段完整。"""
from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path

from eval.loader import CaseSpec
from eval.run_matrix import CaseResult, Runner

CLEAN = {"src/calc.py": "def divide(a, b):\n    return a / b\n"}


def _det_block_case() -> CaseSpec:
    return CaseSpec(
        id="unit.det.syntax", layer="det",
        base=CLEAN, head={"src/broken.py": "def f(a:\n    return a\n"},
        expect_block=True, expect_rules=("SYN-001",),
    )


def _det_clean_case() -> CaseSpec:
    return CaseSpec(
        id="unit.det.clean", layer="det",
        base=CLEAN, head={"src/feature.py": "def f(a):\n    return a\n"},
    )


def test_det_case_result_contract(tmp_path: Path):
    runner = Runner(out_dir=tmp_path / "out")
    result = runner.run_case(_det_block_case(), 0)
    assert result.passed is True
    assert result.blocked is True
    assert "SYN-001" in result.all_rules
    assert result.expected_block is True
    payload = asdict(result)
    for item in fields(CaseResult):
        assert item.name in payload


def test_per_case_workdir_is_removed(tmp_path: Path):
    runner = Runner(out_dir=tmp_path / "out")
    case = _det_clean_case()
    runner.run_case(case, 0)
    assert not (runner.work_root / case.id / "r0").exists()


def test_failed_case_does_not_break_batch(tmp_path: Path):
    runner = Runner(out_dir=tmp_path / "out")
    broken = CaseSpec(id="unit.bad.layer", layer="unknown")
    results = runner.run([broken, _det_clean_case()])
    assert len(results) == 2
    assert any("未知 layer" in item.error for item in results)
    assert any(item.passed for item in results)


def test_resume_does_not_duplicate(tmp_path: Path):
    out = tmp_path / "out"
    runner = Runner(out_dir=out)
    cases = [_det_clean_case()]
    first = runner.run(cases)
    assert len(first) == 1
    second = Runner(out_dir=out).run(cases, resume=True)
    assert len(second) == 1
    lines = [
        json.loads(line) for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 1


def test_budget_stops_before_first_case(tmp_path: Path):
    runner = Runner(out_dir=tmp_path / "out", budget_cny=0.0)
    assert runner.run([_det_clean_case()]) == []


def test_semantic_without_provider_is_skipped(tmp_path: Path):
    runner = Runner(out_dir=tmp_path / "out")
    case = CaseSpec(id="unit.semantic", layer="semantic", expect_block=True)
    result = runner.run_case(case, 0)
    assert result.skipped is True
    assert "semantic" in result.skip_reason
    assert result.passed is False


def test_security_sandbox_probe(tmp_path: Path):
    runner = Runner(out_dir=tmp_path / "out")
    case = CaseSpec(
        id="unit.security.network_none", layer="security", probe="sandbox_kwargs",
        probe_args={"network": "none"}, expect_block=True,
    )
    assert runner.run_case(case, 0).passed is True


def test_gate_merge_probe(tmp_path: Path):
    runner = Runner(out_dir=tmp_path / "out")
    case = CaseSpec(
        id="unit.gate.merge", layer="gate", probe="merge",
        probe_args={"overrides": {"reviewed_sha": "b" * 40}, "blockers": ["SHA"]},
        expect_block=True,
    )
    result = runner.run_case(case, 0)
    assert result.passed is True
    assert result.blocked is True
