"""bench 契约与注册表：字段校验、质量等级、许可证闸门、哈希稳定。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.bench.models import (
    CAPABILITIES,
    DEFAULT_ROLE,
    INTEGRATION,
    PRIMARY_REQUIRED,
    ROLES,
    DatasetEntry,
    Observation,
    ScoreRecord,
    TaskSpec,
)
from eval.bench.registry import RegistryError, load_registry


def _entry(**overrides) -> dict:
    base = {
        "name": "demo",
        "adapter": "core",
        "kind": "external",
        "license": "MIT",
        "status": "approved",
        "quality_grade": "A",
        "capabilities": {"F2": 1.0},
        "primary_metrics": {"F2": "f1"},
    }
    base.update(overrides)
    return base


def test_capabilities_are_six():
    assert CAPABILITIES == ("F1", "F2", "F3", "F4", "F5", "F6")


def test_dataset_entry_grade_weight_and_coverage():
    entry = DatasetEntry(
        name="core", adapter="core", kind="core", license="internal",
        status="approved", quality_grade="A",
        capabilities={"F2": 1.0, "F6": 0.5},
        primary_metrics={"F2": "f1", "F6": "task_success_rate"},
    )
    assert entry.grade_weight == 1.0
    assert entry.coverage("F2") == 1.0
    assert entry.coverage("F6") == 0.5
    assert entry.coverage("F3") == 0.0

    grade_b = DatasetEntry(**{**entry.to_dict(), "quality_grade": "B"})
    grade_c = DatasetEntry(**{**entry.to_dict(), "quality_grade": "C"})
    assert grade_b.grade_weight == 0.7
    assert grade_c.grade_weight == 0.0


def test_dataset_entry_rejects_unknown_capability():
    with pytest.raises(ValueError):
        DatasetEntry(
            name="x", adapter="core", kind="core", license="internal",
            status="approved", quality_grade="A",
            capabilities={"F9": 1.0}, primary_metrics={},
        )


def test_task_spec_validates_capabilities_and_grade():
    with pytest.raises(ValueError):
        TaskSpec(
            task_id="t", dataset="d", capabilities=("F9",),
            identity={}, input={}, expected={}, env={},
            license="MIT", quality_grade="A",
        )
    with pytest.raises(ValueError):
        TaskSpec(
            task_id="t", dataset="d", capabilities=("F2",),
            identity={}, input={}, expected={}, env={},
            license="MIT", quality_grade="Z",
        )


def test_task_spec_roundtrip():
    task = TaskSpec(
        task_id="core.det.syntax", dataset="core", capabilities=("F2",),
        identity={"repo": "demo", "base_commit": "a" * 40},
        input={"case": {"id": "core.det.syntax"}}, expected={"block": True},
        env={}, license="internal", quality_grade="A", source_layer="det",
    )
    payload = task.to_dict()
    restored = TaskSpec.from_dict(payload)
    assert restored == task


def test_observation_status_validated():
    with pytest.raises(ValueError):
        Observation(task_id="t", dataset="d", status="maybe", payload={})


def test_score_record_bounds():
    with pytest.raises(ValueError):
        ScoreRecord(
            task_id="t", dataset="d", capability="F2", metric="f1",
            numerator=2, denominator=1, value=1.5,
        )
    record = ScoreRecord(
        task_id="t", dataset="d", capability="F2", metric="f1",
        numerator=2, denominator=3, value=2 / 3, sample_size=2,
    )
    assert record.value == pytest.approx(0.6667, abs=1e-3)


def test_default_registry_has_core_and_candidates():
    registry = load_registry()
    assert registry.get("core").status == "approved"
    assert registry.get("reviewbench").status == "candidate"
    assert registry.get("swebench_verified").license.upper() == "MIT"
    assert registry.sha256


def test_registry_rejects_approved_without_license(tmp_path: Path):
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "datasets": [_entry(license="UNKNOWN")],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="许可证"):
        load_registry(path)


def test_registry_candidate_may_have_unknown_license(tmp_path: Path):
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "datasets": [_entry(status="candidate", license="UNKNOWN")],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    registry = load_registry(path)
    assert registry.get("demo").is_scorable is False


def test_registry_rejects_unknown_adapter(tmp_path: Path):
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps({"schema_version": 1, "datasets": [_entry(adapter="nope")]}),
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="adapter"):
        load_registry(path)


def test_registry_rejects_duplicate_names(tmp_path: Path):
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps({"schema_version": 1, "datasets": [_entry(), _entry()]}),
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="重复"):
        load_registry(path)


def test_registry_sha256_is_stable():
    first = load_registry()
    second = load_registry()
    assert first.sha256 == second.sha256
    assert len(first.sha256) == 64


def test_role_contract():
    assert ROLES == ("primary", "supplementary", "diagnostic")
    assert DEFAULT_ROLE == "supplementary"
    assert PRIMARY_REQUIRED == ("F1", "F2", "F3", "F4", "F5")
    assert INTEGRATION == ("F6",)


def test_dataset_entry_defaults_to_supplementary():
    entry = DatasetEntry(**_entry())
    assert entry.role == "supplementary"
    assert entry.is_primary is False
    assert entry.is_supplementary is True
    assert entry.is_diagnostic is False


def test_primary_requires_approved_clear_license_and_grade():
    primary = DatasetEntry(**_entry(role="primary"))
    assert primary.is_primary is True
    assert primary.is_supplementary is False

    candidate = DatasetEntry(**_entry(role="primary", status="candidate", license="UNKNOWN"))
    assert candidate.is_primary is False

    grade_c = DatasetEntry(**_entry(role="primary", quality_grade="C"))
    assert grade_c.is_primary is False


def test_dataset_entry_rejects_unknown_role():
    with pytest.raises(ValueError, match="role"):
        DatasetEntry(**_entry(role="nope"))


def test_task_spec_rejects_unknown_role():
    with pytest.raises(ValueError, match="role"):
        TaskSpec(
            task_id="t", dataset="d", capabilities=("F2",),
            identity={}, input={}, expected={}, env={},
            license="MIT", quality_grade="A", role="nope",
        )


def test_default_registry_roles():
    registry = load_registry()
    assert registry.get("core").role == "supplementary"
    assert registry.get("core").is_primary is False
    assert registry.get("swebench_verified").is_primary is True
    assert registry.get("swebench_lite").is_primary is True
    # candidate 数据集即便声明 primary 也不能进入主分
    assert registry.get("swe_care").role == "primary"
    assert registry.get("swe_care").is_primary is False
    assert registry.get("bugsinpy").is_primary is False
    assert {entry.name for entry in registry.primary} == {
        "swebench_verified", "swebench_lite",
    }
    assert {entry.name for entry in registry.supplementary} == {"core"}


def test_registry_rejects_core_as_primary(tmp_path: Path):
    path = tmp_path / "registry.json"
    payload = _entry(kind="core", license="internal", role="primary")
    path.write_text(
        json.dumps({"schema_version": 1, "datasets": [payload]}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="supplementary"):
        load_registry(path)


def test_swebench_declares_f1_probe_capability():
    registry = load_registry()
    for name in ("swebench_verified", "swebench_lite"):
        entry = registry.get(name)
        assert entry.capabilities.get("F1") == 1.0
        assert entry.primary_metric("F1") == "probe_accuracy"
