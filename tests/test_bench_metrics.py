"""指标库：P/R/F1、Wilson、Macro-F1、定位匹配、pytest 解析与稳定性。"""
from __future__ import annotations

import pytest

from eval.bench.metrics import (
    bootstrap_ci,
    localization_accuracy,
    macro_f1,
    match_findings,
    median_iqr,
    normalize_severity,
    parse_pytest_rA,
    prf,
    ratio,
    wilson,
)


def test_ratio_and_prf():
    assert ratio(0, 0) == 0.0
    assert ratio(1, 4) == 0.25
    stats = prf(1, 1, 1)
    assert stats["precision"] == 0.5
    assert stats["recall"] == 0.5
    assert stats["f1"] == 0.5
    assert stats["support"] == 2


def test_wilson_bounds():
    assert wilson(0, 0) == (0.0, 0.0)
    low, high = wilson(10, 10)
    assert 0.6 < low <= 1.0 and high == 1.0
    low, high = wilson(5, 10)
    assert low < 0.5 < high


def test_normalize_severity_mapping():
    assert normalize_severity("CRITICAL") == "high"
    assert normalize_severity("major") == "high"
    assert normalize_severity("nit") == "low"
    assert normalize_severity("unknown") == "low"


def test_macro_f1_with_extra_predicted_label():
    macro, per_label, labels = macro_f1([("high", "high"), ("low", "medium")])
    assert labels == 3
    assert per_label["high"]["f1"] == 1.0
    assert macro == pytest.approx(1 / 3, abs=1e-4)


def test_match_findings_window_and_false_positives():
    gold = [{"path": "src/a.py", "line": 10}]
    predicted = [{"path": "a/src/a.py", "line": 12}, {"path": "src/a.py", "line": 40}]
    result = match_findings(predicted, gold, window=3)
    assert result.tp == 1
    assert result.fp == 1
    assert result.fn == 0
    assert result.matched_gold == 1


def test_match_findings_miss_outside_window():
    result = match_findings(
        [{"path": "src/c.py", "line": 50}],
        [{"path": "src/c.py", "line": 1}],
        window=3,
    )
    assert result.tp == 0 and result.fn == 1 and result.fp == 1


def test_localization_accuracy_empty_gold():
    assert localization_accuracy([{"path": "a.py", "line": 1}], []) == 0.0
    assert localization_accuracy(
        [{"path": "src/a.py", "line": 12}],
        [{"path": "src/a.py", "line": 10}],
    ) == 1.0


def test_parse_pytest_rA_status_first_and_node_first():
    text = (
        "PASSED tests/test_a.py::test_one\n"
        "FAILED tests/test_b.py::test_two - AssertionError\n"
        "tests/test_c.py::test_three PASSED\n"
        "SKIPPED tests/test_d.py::test_four\n"
    )
    statuses = parse_pytest_rA(text)
    assert statuses["tests/test_a.py::test_one"] == "passed"
    assert statuses["tests/test_b.py::test_two"] == "failed"
    assert statuses["tests/test_c.py::test_three"] == "passed"
    assert statuses["tests/test_d.py::test_four"] == "skipped"


def test_median_iqr():
    assert median_iqr([])["n"] == 0
    stats = median_iqr([1, 2, 3, 4])
    assert stats["median"] == 2.5
    assert stats["n"] == 4


def test_bootstrap_ci_is_deterministic():
    values = [0.2, 0.4, 0.6, 0.8, 1.0]
    assert bootstrap_ci(values, seed=7) == bootstrap_ci(values, seed=7)
    assert bootstrap_ci([0.5]) == (0.5, 0.5)
