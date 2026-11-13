"""三轨评分：主分只吃外部、自建补充分独立、缺失维输出已有 F 且 total=None。"""
from __future__ import annotations

import pytest

from eval.bench.models import DatasetEntry, ScoreRecord
from eval.bench.registry import Registry
from eval.bench.scoring import (
    CAPABILITY_WEIGHTS,
    PRIMARY,
    STATUS_INTEGRATION_ONLY,
    STATUS_NO_EXTERNAL,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_SUPPLEMENTARY_ONLY,
    SUPPLEMENTARY,
    evaluate,
    evaluate_integration,
    evaluate_primary,
    evaluate_supplementary,
    function_scores,
    source_scores,
)
from eval.bench.thresholds import Thresholds, load_thresholds

_REQUIRED = ("F1", "F2", "F3", "F4", "F5")


def _entry(name: str, caps: dict[str, float], *, role: str = "primary",
           grade: str = "A", status: str = "approved", license: str = "MIT",
           kind: str = "external") -> DatasetEntry:
    return DatasetEntry(
        name=name, adapter="core", kind=kind, role=role, license=license,
        status=status, quality_grade=grade, capabilities=caps,
        primary_metrics={cap: "f1" for cap in caps},
    )


def _registry(*entries: DatasetEntry) -> Registry:
    return Registry(entries=tuple(entries))


def _record(dataset: str, capability: str, metric: str, value: float, *,
            sample_size: int = 10, uid: str = "") -> ScoreRecord:
    return ScoreRecord(
        task_id=f"{dataset}.{capability}", dataset=dataset, capability=capability,
        metric=metric, numerator=value * 100, denominator=100, value=value,
        sample_size=sample_size, sample_uid=uid or f"{dataset}-{capability}",
    )


_METRICS = {
    "F1": "probe_accuracy", "F2": "f1", "F3": "f1",
    "F4": "resolve_rate", "F5": "pass_to_pass_rate", "F6": "task_success_rate",
}


def _records(dataset: str, values: dict[str, float], *, sample_size: int = 20):
    return [
        _record(dataset, cap, _METRICS[cap], value, sample_size=sample_size)
        for cap, value in values.items()
    ]


def _primary_registry() -> Registry:
    return _registry(_entry("ext", {cap: 1.0 for cap in _REQUIRED}))


def test_function_weights_match_plan():
    assert CAPABILITY_WEIGHTS == {"F2": 0.25, "F3": 0.25, "F4": 0.25, "F5": 0.15, "F6": 0.10}


def test_source_weight_uses_grade_and_coverage():
    registry = _registry(
        _entry("ds_a", {"F2": 1.0}, grade="A"),
        _entry("ds_b", {"F2": 0.5}, grade="B"),
    )
    records = [
        _record("ds_a", "F2", "f1", 0.8),
        _record("ds_b", "F2", "f1", 0.6),
    ]
    sources = source_scores(records, registry, role=PRIMARY)
    assert len(sources) == 2
    weights = {item.dataset: item.weight for item in sources}
    assert weights["ds_a"] == pytest.approx(1.0)
    assert weights["ds_b"] == pytest.approx(0.35)
    scores = function_scores(records, registry, role=PRIMARY)
    assert scores["F2"].score == pytest.approx(74.81, abs=0.05)
    assert scores["F2"].sample_size == 20


def test_candidate_dataset_is_not_scored():
    registry = _registry(
        _entry("approved", {"F2": 1.0}),
        _entry("candidate", {"F2": 1.0}, status="candidate", license="UNKNOWN"),
    )
    records = [
        _record("approved", "F2", "f1", 0.8),
        _record("candidate", "F2", "f1", 0.1),
    ]
    sources = source_scores(records, registry, role=PRIMARY)
    assert [item.dataset for item in sources] == ["approved"]
    assert function_scores(records, registry, role=PRIMARY)["F2"].score == pytest.approx(80.0)


def test_primary_ignores_supplementary_core():
    registry = _registry(
        _entry("ext", {"F2": 1.0}),
        _entry("core", {"F2": 1.0}, role="supplementary", kind="core", license="internal"),
    )
    records = [
        _record("ext", "F2", "f1", 0.20),
        _record("core", "F2", "f1", 0.90),
    ]
    primary = evaluate_primary(records, registry, load_thresholds())
    assert primary.functions["F2"].score == pytest.approx(20.0)
    supplementary = evaluate_supplementary(records, registry, load_thresholds())
    assert supplementary.functions["F2"].score == pytest.approx(90.0)
    assert supplementary.score == pytest.approx(90.0)


def test_full_primary_is_ok_with_normalized_weights():
    registry = _primary_registry()
    records = _records("ext", {
        "F1": 0.85, "F2": 0.80, "F3": 0.70, "F4": 0.60, "F5": 0.90,
    })
    result = evaluate(records, registry, load_thresholds())
    assert result.status == STATUS_OK
    assert result.composite["coverage"] == pytest.approx(1.0)
    # F1 门禁不占权重；F2/F3/F4/F5 在轨道内归一化为 0.2778/0.2778/0.2778/0.1667
    assert result.composite["total"] == pytest.approx(73.33, abs=0.05)
    assert result.overall == "PASS"
    assert result.composite["supplementary"] is None


def test_missing_dimension_keeps_other_scores_without_total():
    registry = _primary_registry()
    records = _records("ext", {"F1": 0.85, "F2": 0.796, "F3": 0.71, "F5": 0.934})
    result = evaluate(records, registry, load_thresholds())
    assert result.status == STATUS_PARTIAL
    assert result.composite["total"] is None
    assert result.composite["partial"] is not None
    assert result.composite["missing"] == ["F4"]
    for cap in ("F1", "F2", "F3", "F5"):
        assert result.tracks[PRIMARY].functions[cap].score is not None
    assert result.tracks[PRIMARY].functions["F4"].status == "missing"
    assert result.overall == STATUS_PARTIAL


def test_any_single_missing_dimension_still_reports_others():
    for missing in _REQUIRED:
        registry = _primary_registry()
        values = {cap: 0.80 for cap in _REQUIRED if cap != missing}
        result = evaluate(_records("ext", values), registry, load_thresholds())
        assert result.status == STATUS_PARTIAL, missing
        assert result.composite["total"] is None, missing
        assert missing in result.composite["missing"], missing
        for cap in values:
            assert result.tracks[PRIMARY].functions[cap].score is not None, cap


def test_f1_missing_withholds_total_but_keeps_other_scores():
    registry = _primary_registry()
    records = _records("ext", {"F2": 0.80, "F3": 0.70, "F4": 0.60, "F5": 0.90})
    result = evaluate(records, registry, load_thresholds())
    assert result.status == STATUS_PARTIAL
    assert result.composite["total"] is None
    assert result.composite["coverage"] == pytest.approx(1.0)
    assert result.verdicts["F1"] == "MISSING"
    assert any("F1" in reason for reason in result.invalid_reasons)


def test_f1_below_floor_is_fail_without_total():
    registry = _primary_registry()
    records = _records("ext", {
        "F1": 0.60, "F2": 0.80, "F3": 0.70, "F4": 0.60, "F5": 0.90,
    })
    result = evaluate(records, registry, load_thresholds())
    assert result.status == STATUS_PARTIAL
    assert result.composite["total"] is None
    assert result.verdicts["F1"] == "FAIL"
    assert result.overall == "FAIL"
    assert any("F1" in reason for reason in result.invalid_reasons)


def test_core_only_is_supplementary_only():
    registry = _registry(
        _entry("core", {"F1": 1.0, "F2": 1.0, "F3": 1.0, "F6": 1.0},
               role="supplementary", kind="core", license="internal"),
    )
    records = _records("core", {"F1": 0.9, "F2": 0.95, "F3": 1.0, "F6": 0.96})
    result = evaluate(records, registry, load_thresholds())
    assert result.status == STATUS_SUPPLEMENTARY_ONLY
    assert result.overall == "SELF_BUILT_ONLY"
    assert result.composite["total"] is None
    assert result.composite["supplementary"] is not None
    assert result.tracks[PRIMARY].score is None
    # 自建分数永远不出现在主分里
    assert result.tracks[PRIMARY].functions["F2"].score is None
    assert result.tracks[SUPPLEMENTARY].functions["F2"].score == pytest.approx(95.0)


def test_supplementary_never_mixes_into_primary():
    registry = _registry(
        _entry("ext", {"F2": 1.0}),
        _entry("core", {"F2": 1.0}, role="supplementary", kind="core", license="internal"),
    )
    records = [
        _record("ext", "F2", "f1", 0.40),
        _record("core", "F2", "f1", 0.95),
    ]
    result = evaluate(records, registry, load_thresholds())
    assert result.tracks[PRIMARY].functions["F2"].score == pytest.approx(40.0)
    assert result.composite["supplementary"] == pytest.approx(95.0)


def test_no_evidence_is_no_external_evidence():
    result = evaluate([], _primary_registry(), load_thresholds())
    assert result.status == STATUS_NO_EXTERNAL
    assert result.composite["total"] is None
    assert result.evidence_mix["total_weight"] == 0.0


def test_f6_is_reported_as_integration_only():
    registry = _registry(
        _entry("core", {"F6": 1.0}, role="supplementary", kind="core", license="internal"),
    )
    records = _records("core", {"F6": 0.96})
    result = evaluate(records, registry, load_thresholds())
    integration = evaluate_integration(records, registry, load_thresholds())
    assert integration.score == pytest.approx(96.0)
    assert result.status == STATUS_INTEGRATION_ONLY
    assert result.composite["integration"] == pytest.approx(96.0)
    assert result.composite["total"] is None
    assert "F6" not in (result.composite["weights"] or {})


def test_insufficient_sample_is_not_scored():
    registry = _registry(_entry("all", {cap: 1.0 for cap in _REQUIRED}))
    records = [
        _record("all", cap, _METRICS[cap], 1.0, sample_size=1) for cap in _REQUIRED
    ]
    result = evaluate(records, registry, Thresholds(min_samples=5))
    assert result.functions["F2"].score is None
    assert result.functions["F2"].status == "insufficient"
    assert result.composite["coverage"] == pytest.approx(0.0)
    assert result.composite["total"] is None


def test_regression_detection():
    registry = _primary_registry()
    thresholds = load_thresholds()
    records = _records("ext", {
        "F1": 0.85, "F2": 0.80, "F3": 0.70, "F4": 0.60, "F5": 0.90,
    })
    current = evaluate(records, registry, thresholds, baseline={
        "composite": {"total": 80.0, "partial": 80.0},
        "functions": {"F4": {"score": 70.0}},
    })
    assert any("退化" in reason for reason in current.regressions)
    assert current.overall == "FAIL"
