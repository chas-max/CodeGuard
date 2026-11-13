"""评测夹具加载与两个离线评测脚本（S4）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval import run_pr_eval, run_reviewer_eval
from eval.loader import load_cases, validate_fixture


def test_default_cases_load_and_validate():
    cases = load_cases()
    names = {case["name"] for case in cases}
    assert {"syntax_error", "secret", "test_weakening", "docs_only"} <= names
    assert all(case["source"] in ("synthetic", "incident") for case in cases)


def test_invalid_fixture_rejected():
    with pytest.raises(ValueError):
        validate_fixture({"name": "x"})
    with pytest.raises(ValueError):
        validate_fixture({"name": "x", "expected": {"block": True}, "head": {"../evil": "x"}})
    with pytest.raises(ValueError):
        validate_fixture({"name": "x", "expected": {"block": True}, "source": "guess"})


def test_pr_eval_has_no_false_merge_or_block(tmp_path: Path):
    report = run_pr_eval.run(tmp_path / "out")
    assert report["false_merge"] == 0
    assert report["false_block"] == 0
    assert report["passed"] == report["total"]
    written = json.loads((tmp_path / "out" / "pr_eval_report.json").read_text(encoding="utf-8"))
    assert written["rows"][0]["scenario"]


def test_reviewer_eval_precision_on_recorded_cases(tmp_path: Path):
    report = run_reviewer_eval.run()
    assert report["total"] == 3
    assert report["precision"] == 1.0
    assert report["recall"] == 1.0
    style = {row["case"]: row for row in report["rows"]}["style_only_noise"]
    assert style["actual"] == []  # style 不阻塞
