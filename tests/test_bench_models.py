"""bench 契约与注册表：字段校验、质量等级、许可证闸门、哈希稳定。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from eval.bench.models import (
    CAPABILITIES,
    CORE_LAYERS,
    DEFAULT_ROLE,
    EXTERNAL_LAYERS,
    INTEGRATION,
    PRIMARY_REQUIRED,
    ROLES,
    SOURCE_LAYERS,
    TASK_SCHEMA_VERSION,
    DatasetEntry,
    FixExpected,
    FixInput,
    GoldFinding,
    Observation,
    ReviewerReplayInput,
    ScoreRecord,
    TaskEnv,
    TaskIdentity,
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
            license="MIT", quality_grade="A", source_layer="fix",
        )
    with pytest.raises(ValueError):
        TaskSpec(
            task_id="t", dataset="d", capabilities=("F2",),
            identity={}, input={}, expected={}, env={},
            license="MIT", quality_grade="Z", source_layer="fix",
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
            license="MIT", quality_grade="A", source_layer="fix", role="nope",
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


def test_task_spec_rejects_empty_or_unknown_source_layer():
    with pytest.raises(ValueError, match="source_layer"):
        TaskSpec(
            task_id="t", dataset="d", capabilities=("F2",),
            identity={"repo": "demo/repo"},
            input={"case": {"id": "t"}},
            expected={"block": True},
            env={}, license="MIT", quality_grade="A",
        )
    with pytest.raises(ValueError, match="source_layer"):
        TaskSpec(
            task_id="t", dataset="d", capabilities=("F2",),
            identity={"repo": "demo/repo"},
            input={"case": {"id": "t"}},
            expected={"block": True},
            env={}, license="MIT", quality_grade="A",
            source_layer="unknown_layer",
        )


def test_task_spec_submodels_are_frozen_and_forbid_extra():
    task = TaskSpec(
        task_id="fix.1", dataset="swebench_verified", capabilities=("F4", "F5"),
        identity={"repo": "django/django", "commit": "a" * 40, "instance_id": "django-1"},
        input={"problem_statement": "bug", "patch": "diff", "profile": "bugfix"},
        expected={"fail_to_pass": ["test_a"]},
        env={"image": "img:1", "version": "4.2", "language": "python", "test_command": ""},
        license="MIT", quality_grade="A", source_layer="fix",
    )
    assert isinstance(task.identity, TaskIdentity)
    assert isinstance(task.input, FixInput)
    assert isinstance(task.expected, FixExpected)
    assert isinstance(task.env, TaskEnv)
    assert task.schema_version == TASK_SCHEMA_VERSION == 2
    assert task.identity.base_commit == "a" * 40
    assert task.identity.pr_or_issue == "instance:django-1"
    assert task.env.test_command == "python -m pytest -q"

    with pytest.raises(ValidationError):
        task.identity.repo = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        task.input.patch = "x"  # type: ignore[misc]

    with pytest.raises(ValueError):
        TaskSpec(
            task_id="fix.2", dataset="swebench_verified", capabilities=("F4",),
            identity={"repo": "a/b"},
            input={"instance": {"raw": 1}, "patch": "p"},
            expected={}, env={}, license="MIT", quality_grade="A", source_layer="fix",
        )
    with pytest.raises(ValueError):
        TaskSpec(
            task_id="rev.1", dataset="reviewbench", capabilities=("F2",),
            identity={"repo": "a/b"},
            input={"diff": ""}, expected={}, env={"window": 3},
            license="MIT", quality_grade="B", source_layer="review",
        )


def test_task_spec_covers_all_source_layers_and_roundtrips():
    samples = [
        ("probe", ("F1",), {"probe": "deleted", "diff": "d"}, {"paths": ["a.py"]}),
        ("reviewer", ("F3",), {"case_name": "c1", "response": {"verdict": "pass"}}, {"blocking": []}),
        ("det", ("F2",), {"case": {"id": "core.det.1"}}, {"block": True, "rules": ["R1"]}),
        ("semantic", ("F2",), {"case": {"id": "core.sem.1"}}, {"block": False, "semantic": True}),
        ("gate", ("F6",), {"case": {"id": "core.gate.1"}}, {"block": True, "approvals": 2}),
        ("core_security", ("F6",), {"case": {"id": "core.sec.1", "layer": "security"}}, {"block": True}),
        ("e2e", ("F5", "F6"), {"case": {"id": "core.e2e.1"}}, {"block": False}),
        ("external", ("F2",), {"case": {"id": "core.ext.1"}}, {"block": True}),
        ("fix", ("F4", "F5"), {"patch": "diff", "problem_statement": "p"}, {"fail_to_pass": ["t1"]}),
        (
            "review", ("F2", "F3"), {"diff": "d", "title": "t"},
            {"gold_findings": [{"path": "a.py", "line": 3, "severity": "critical", "category": "bug"}]},
        ),
        ("security", ("F2", "F3"), {"code": "x = 1", "fixed_code": "x = 2"}, {"vulnerable": True, "cwe": "CWE-78"}),
    ]
    assert tuple(row[0] for row in samples) == SOURCE_LAYERS
    assert set(CORE_LAYERS) | set(EXTERNAL_LAYERS) == set(SOURCE_LAYERS)
    for layer, caps, inp, exp in samples:
        task = TaskSpec(
            task_id=f"t.{layer}", dataset="demo", capabilities=caps,
            identity={"repo": "demo/repo"}, input=inp, expected=exp, env={},
            license="MIT", quality_grade="A", source_layer=layer,
        )
        dumped = task.to_dict()
        assert dumped["schema_version"] == 2
        assert "instance" not in dumped["input"]
        restored = TaskSpec.from_dict(dumped)
        assert restored == task
    review_task = TaskSpec.from_dict({
        "task_id": "t.rev", "dataset": "demo", "capabilities": ["F2"],
        "identity": {"repo": "demo/repo"},
        "input": {"diff": "d"},
        "expected": {"gold_findings": [{"path": "a.py", "line": 1, "severity": "critical"}]},
        "env": {}, "license": "MIT", "quality_grade": "A", "source_layer": "review",
        "schema_version": 2,
    })
    assert review_task.expected.gold_findings[0] == GoldFinding(path="a.py", line=1, severity="high")


def test_task_spec_from_dict_migrates_v1_payloads():
    v1_fix = {
        "task_id": "swe.1", "dataset": "swebench_verified", "capabilities": ["F4", "F5"],
        "identity": {"repo": "django/django", "base_commit": "a" * 40, "pr_or_issue": "instance:1", "instance_id": "1"},
        "input": {
            "instance": {
                "instance_id": "1", "repo": "django/django", "base_commit": "a" * 40,
                "problem_statement": "fix bug", "title": "Fix crash",
                "version": "4.2", "environment_setup_commit": "b" * 40,
                "language": "python", "python_version": "3.11",
            },
            "patch": "diff --git a/a.py b/a.py", "test_patch": "tpatch",
        },
        "expected": {"fail_to_pass": ["test_1"], "pass_to_pass": []},
        "env": {
            "image": "img:1", "test_command": "pytest",
            "snapshot_root": "/tmp/snap", "prepare_commands": ["pip install -e ."],
        },
        "license": "MIT", "quality_grade": "A", "source_layer": "fix",
    }
    fix_task = TaskSpec.from_dict(v1_fix)
    assert fix_task.schema_version == 2
    assert fix_task.input.problem_statement == "fix bug"
    assert fix_task.input.title == "Fix crash"
    assert fix_task.env.version == "4.2"
    assert fix_task.env.environment_setup_commit == "b" * 40
    assert fix_task.env.python_version == "3.11"

    v1_review = {
        "task_id": "rb.1", "dataset": "reviewbench", "capabilities": ["F2", "F3"],
        "identity": {"repo": "org/r", "base_commit": "c" * 40, "pr_or_issue": "pr:9", "instance_id": "9"},
        "input": {
            "instance": {"head_commit": "d" * 40, "problem_statement": "review body", "title": "PR title"},
            "diff": "diff --git a/x.py b/x.py", "title": "Outer title",
        },
        "expected": {"gold_findings": [{"path": "x.py", "line": 4, "severity": "major", "category": "sec"}]},
        "env": {"window": 3},
        "license": "MIT", "quality_grade": "B", "source_layer": "review",
    }
    rev_task = TaskSpec.from_dict(v1_review)
    assert rev_task.identity.head_commit == "d" * 40
    assert rev_task.input.title == "Outer title"
    assert rev_task.input.problem_statement == "review body"
    assert rev_task.expected.gold_findings[0].severity == "high"

    v1_security = {
        "task_id": "devign.1", "dataset": "devign", "capabilities": ["F2", "F3"],
        "identity": {"repo": "qemu", "base_commit": "e" * 40, "pr_or_issue": "instance:1", "instance_id": "1"},
        "input": {"instance": {"code": "system(x);", "fixed_code": "safe(x);"}, "code": "system(x);"},
        "expected": {"vulnerable": True, "severity": "critical", "cwe": "CWE-78"},
        "env": {}, "license": "MIT", "quality_grade": "B", "source_layer": "security",
    }
    sec_task = TaskSpec.from_dict(v1_security)
    assert sec_task.source_layer == "security"
    assert sec_task.input.fixed_code == "safe(x);"
    assert sec_task.expected.severity == "high"

    v1_core_sec = {
        "task_id": "core.sec.1", "dataset": "core", "capabilities": ["F6"],
        "identity": {"repo": "core", "base_commit": "", "pr_or_issue": "sec.1"},
        "input": {"case": {"id": "sec.1", "layer": "security"}},
        "expected": {"block": True, "rules": [], "tier": ""},
        "env": {}, "license": "internal", "quality_grade": "A", "source_layer": "security",
    }
    core_sec = TaskSpec.from_dict(v1_core_sec)
    assert core_sec.source_layer == "core_security"
    assert core_sec.input.case["layer"] == "security"

    v1_core_reviewer = {
        "task_id": "core.reviewer.bad", "dataset": "core", "capabilities": ["F3"],
        "identity": {"repo": "core", "base_commit": "", "pr_or_issue": "core.reviewer.bad"},
        "input": {"reviewer": {"id": "core.reviewer.bad", "case_name": "bad", "response": {"verdict": "fail"}}},
        "expected": {"blocking": ["R1"]},
        "env": {}, "license": "internal", "quality_grade": "A", "source_layer": "reviewer",
    }
    rev_replay = TaskSpec.from_dict(v1_core_reviewer)
    assert isinstance(rev_replay.input, ReviewerReplayInput)
    assert rev_replay.input.case_name == "bad"
    assert rev_replay.input.response == {"verdict": "fail"}
