"""适配器契约：层→能力映射、主指标聚合、外部数据集归一化。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.bench.adapters import get_adapter
from eval.bench.models import Observation, TaskSpec
from eval.bench.registry import load_registry

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "eval" / "datasets" / "bench" / "fixtures"


def _task(task_id: str, caps: tuple[str, ...], *, dataset: str = "demo",
          layer: str = "", expected: dict | None = None) -> TaskSpec:
    resolved = layer or "det"
    inputs = {
        "probe": {"probe": "diff_paths", "diff": ""},
        "reviewer": {"case_name": task_id, "response": {}},
        "review": {"diff": ""},
        "security": {"code": ""},
        "fix": {},
    }
    payload = inputs.get(resolved, {"case": {"id": task_id}})
    return TaskSpec(
        task_id=task_id, dataset=dataset, capabilities=caps,
        identity={"repo": "demo/repo", "base_commit": "a" * 40, "pr_or_issue": task_id},
        input=payload, expected=expected or {}, env={},
        license="MIT", quality_grade="A", source_layer=resolved,
    )


def _obs(task: TaskSpec, payload: dict, *, status: str = "ok") -> Observation:
    return Observation(
        task_id=task.task_id, dataset=task.dataset, status=status,
        payload=payload, capabilities=task.capabilities,
    )


def test_core_adapter_maps_layers_to_capabilities():
    entry = load_registry().get("core")
    adapter = get_adapter("core")
    instances = adapter.load_instances(entry, root=FIXTURES, limit=6)
    tasks = adapter.to_tasks(entry, instances)
    caps = {cap for task in tasks for cap in task.capabilities}
    assert {"F1", "F2", "F3", "F6"} <= caps
    assert all(task.dataset == "core" for task in tasks)
    assert all(task.quality_grade == "A" for task in tasks)


def test_core_adapter_f2_micro_f1():
    entry = load_registry().get("core")
    adapter = get_adapter("core")
    t1 = _task("det.a", ("F2",), layer="det", expected={"rules": ["SYN-001"]})
    t2 = _task("det.b", ("F2",), layer="det", expected={"rules": ["SEC-001"]})
    observations = [
        _obs(t1, {"layer": "det", "expected_rules": ["SYN-001"],
                  "all_rules": ["SYN-001"], "expected_block": True, "blocked": True}),
        _obs(t2, {"layer": "det", "expected_rules": ["SEC-001"],
                  "all_rules": [], "expected_block": True, "blocked": False}),
    ]
    records = adapter.aggregate(entry, [t1, t2], observations)
    f2 = next(item for item in records if item.capability == "F2")
    assert f2.metric == "f1"
    assert f2.numerator == pytest.approx(2.0)
    assert f2.denominator == pytest.approx(3.0)
    assert f2.value == pytest.approx(2 / 3, abs=1e-4)
    assert f2.sample_size == 2


def test_core_adapter_f3_reviewer_cassette():
    entry = load_registry().get("core")
    adapter = get_adapter("core")
    t1 = _task("review.a", ("F3",), layer="reviewer")
    t2 = _task("review.b", ("F3",), layer="reviewer")
    observations = [
        _obs(t1, {"expected": ["a.py:1"], "actual": ["a.py:1"]}),
        _obs(t2, {"expected": ["b.py:2"], "actual": ["b.py:3", "c.py:4"]}),
    ]
    records = adapter.aggregate(entry, [t1, t2], observations)
    f3 = next(item for item in records if item.capability == "F3")
    assert f3.metric == "f1"
    assert f3.value == pytest.approx(0.4, abs=1e-4)


def test_core_adapter_f6_and_f1():
    entry = load_registry().get("core")
    adapter = get_adapter("core")
    gate = _task("gate.tier", ("F6",), layer="gate")
    probe = _task("probe.diff", ("F1",), layer="probe")
    observations = [
        _obs(gate, {"layer": "gate", "passed": True}),
        _obs(probe, {"probe": "diff_paths", "passed": False}),
    ]
    records = adapter.aggregate(entry, [gate, probe], observations)
    f6 = next(item for item in records if item.capability == "F6")
    f1 = next(item for item in records if item.capability == "F1")
    assert f6.value == 1.0
    assert f1.value == 0.0


def test_swebench_adapter_resolve_and_p2p():
    entry = load_registry().get("swebench_verified")
    adapter = get_adapter("swebench")
    rows = [
        json.loads(line) for line in
        (FIXTURES / "swebench_sample.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    normalized = [adapter.normalize_instance(row) for row in rows]
    normalized[0]["image"] = "codeguard-sandbox-pytest8:3.12"
    tasks = adapter.to_tasks(entry, normalized)
    assert len(tasks) == 3
    assert all({"F4", "F5"} <= set(task.capabilities) for task in tasks)
    assert tasks[0].identity.base_commit == "a" * 40
    assert tasks[0].env.image == "codeguard-sandbox-pytest8:3.12"
    assert tasks[1].env.image == entry.image

    observations = [
        _obs(tasks[0], {"resolved": True, "applied": True,
                        "fail_to_pass": {"t1": True}, "pass_to_pass": {"p1": True, "p2": True}}),
        _obs(tasks[1], {"resolved": False, "applied": True,
                        "fail_to_pass": {"t2": False}, "pass_to_pass": {"p1": True}}),
        _obs(tasks[2], {"resolved": False, "applied": False,
                        "fail_to_pass": {"t3": False}, "pass_to_pass": {"p3": False}}),
    ]
    records = adapter.aggregate(entry, tasks, observations)
    f4 = next(item for item in records if item.capability == "F4")
    f5 = next(item for item in records if item.capability == "F5")
    assert f4.metric == "resolve_rate"
    assert f4.value == pytest.approx(1 / 3, abs=1e-4)
    assert f5.metric == "pass_to_pass_rate"
    assert f5.value == pytest.approx(3 / 4, abs=1e-4)
    assert f5.sample_size == 3


def test_reviewbench_adapter_localization_and_severity():
    entry = load_registry().get("reviewbench")
    adapter = get_adapter("reviewbench")
    rows = [
        json.loads(line) for line in
        (FIXTURES / "reviewbench_sample.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tasks = adapter.to_tasks(entry, [adapter.normalize_instance(row) for row in rows])
    assert len(tasks) == 3

    observations = [
        _obs(tasks[0], {"findings": [{"path": "src/a.py", "line": 12, "severity": "high"}]}),
        _obs(tasks[1], {"findings": []}),
        _obs(tasks[2], {"findings": [
            {"path": "src/c.py", "line": 50, "severity": "low"},
            {"path": "src/d.py", "line": 3, "severity": "high"},
        ]}),
    ]
    records = adapter.aggregate(entry, tasks, observations)
    f2 = next(item for item in records if item.capability == "F2")
    f3 = next(item for item in records if item.capability == "F3")
    assert f2.metric == "localization_accuracy"
    assert f2.value == pytest.approx(1 / 3, abs=1e-4)
    assert f3.metric == "f1"
    assert f3.value == pytest.approx(1 / 3, abs=1e-4)
    severity = [item for item in records if item.metric == "severity_macro_f1"]
    assert severity and 0.0 <= severity[0].value <= 1.0


def test_security_adapter_precision_recall():
    entry = load_registry().get("devign")
    adapter = get_adapter("security")
    tasks = [
        _task(f"sec.{index}", ("F2",), dataset="devign", layer="security",
              expected={"vulnerable": vulnerable})
        for index, vulnerable in enumerate([True, False, True])
    ]
    observations = [
        _obs(tasks[0], {"vulnerable": True}),
        _obs(tasks[1], {"vulnerable": True}),
        _obs(tasks[2], {"vulnerable": False}),
    ]
    records = adapter.aggregate(entry, tasks, observations)
    f2 = next(item for item in records if item.capability == "F2")
    assert f2.value == pytest.approx(0.5, abs=1e-4)


def test_adapter_without_gold_is_not_scored():
    entry = load_registry().get("reviewbench")
    adapter = get_adapter("reviewbench")
    task = _task("review.empty", ("F2", "F3"), dataset="reviewbench",
                 layer="review", expected={"gold_findings": []})
    records = adapter.aggregate(entry, [task], [_obs(task, {"findings": []})])
    assert all(record.denominator == 0 for record in records)
    assert all(record.value == 0.0 for record in records)
    assert isinstance(records, list) and records


def test_swebench_capabilities_come_from_registry():
    registry = load_registry()
    for name in ("swebench_verified", "swebench_lite"):
        entry = registry.get(name)
        adapter = get_adapter(entry.adapter)
        normalized = [adapter.normalize_instance(row) for row in _swebench_rows()]
        task = adapter.to_tasks(entry, normalized)[0]
        assert {"F1", "F4", "F5"} <= set(task.capabilities)
        assert task.role == "primary"


def test_swebench_adapter_emits_f1_probe_accuracy():
    entry = load_registry().get("swebench_verified")
    adapter = get_adapter("swebench")
    normalized = [adapter.normalize_instance(row) for row in _swebench_rows()]
    tasks = adapter.to_tasks(entry, normalized)
    observations = [
        _obs(tasks[0], {
            "resolved": True, "applied": True,
            "f1_probe": {"expected": ["src/a.py"], "actual": ["src/a.py"], "passed": True},
            "fail_to_pass": {"t1": True}, "pass_to_pass": {"p1": True},
        }),
        _obs(tasks[1], {
            "resolved": False, "applied": True,
            "f1_probe": {"expected": ["src/b.py"], "actual": [], "passed": False},
            "fail_to_pass": {"t2": False}, "pass_to_pass": {"p2": True},
        }),
    ]
    records = adapter.aggregate(entry, tasks, observations)
    f1 = next(item for item in records if item.capability == "F1")
    assert f1.metric == "probe_accuracy"
    assert f1.value == pytest.approx(0.5, abs=1e-4)
    assert f1.sample_size == 2


def test_swe_care_reference_comments_are_gold():
    entry = load_registry().get("swe_care")
    adapter = get_adapter(entry.adapter)
    raw = {
        "repo": "django/django",
        "number": 1234,
        "patch": (
            "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
            "@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        ),
        "reference_comments": [
            {"filename": "src/a.py", "original_line": 12, "body": "这里可能空指针",
             "severity": "high"},
        ],
    }
    normalized = adapter.normalize_instance(raw)
    assert normalized["instance_id"] == "django/django#1234"
    assert normalized["pr_number"] == 1234
    assert normalized["gold_findings"][0]["path"] == "src/a.py"
    assert normalized["gold_findings"][0]["line"] == 12
    assert normalized["gold_findings"][0]["severity"] == "high"

    tasks = adapter.to_tasks(entry, [normalized])
    assert tasks[0].capabilities == ("F2", "F3")
    assert tasks[0].role == "primary"


def test_bugsinpy_adds_f2_localization():
    entry = load_registry().get("bugsinpy")
    adapter = get_adapter("bugsinpy")
    patch = (
        "diff --git a/calc.py b/calc.py\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b\n"
    )
    normalized = adapter.normalize_instance({
        "project": "demo/repo", "bug_id": 1, "patch": patch,
        "tests": ["tests/test_calc.py::test_add"], "pass_to_pass": [],
    })
    tasks = adapter.to_tasks(entry, [normalized])
    assert {"F2", "F4", "F5"} <= set(tasks[0].capabilities)

    observations = [
        _obs(tasks[0], {
            "resolved": True, "applied": True,
            "f1_probe": {"expected": ["calc.py"], "actual": ["calc.py"], "passed": True},
            "fail_to_pass": {"tests/test_calc.py::test_add": True},
            "pass_to_pass": {},
        }),
    ]
    records = adapter.aggregate(entry, tasks, observations)
    f2 = next(item for item in records if item.capability == "F2")
    assert f2.metric == "localization_accuracy"
    assert f2.value == pytest.approx(1.0)
    assert any(item.capability == "F4" for item in records)


def _swebench_rows() -> list[dict]:
    return [
        json.loads(line) for line in
        (FIXTURES / "swebench_sample.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_swebench_and_multi_swebench_fields_and_no_instance_dump():
    adapter = get_adapter("swebench")
    reg = load_registry()
    swe = reg.get("swebench_verified")
    mswe = reg.get("multi_swe_bench")
    inst = adapter.normalize_instance({
        "instance_id": "repo-1",
        "repo": "org/repo",
        "base_commit": "a" * 40,
        "patch": "diff --git a/a.py b/a.py\n+x = 1\n",
        "test_patch": "tpatch",
        "problem_statement": "Crash on empty input",
        "title": "Fix empty",
        "version": "2.1",
        "environment_setup_commit": "b" * 40,
        "language": "go",
        "python_version": "3.11",
    })
    tasks_swe = adapter.to_tasks(swe, [inst])
    t = tasks_swe[0]
    assert "instance" not in t.to_dict()["input"]
    assert "snapshot_root" not in t.to_dict()["env"]
    assert t.env.version == "2.1"
    assert t.env.environment_setup_commit == "b" * 40
    assert t.env.python_version == "3.11"
    assert t.input.problem_statement == "Crash on empty input"
    assert t.input.title == "Fix empty"
    assert t.input.profile == "bugfix"

    inst_no_lang = adapter.normalize_instance({
        "instance_id": "go-1", "repo": "org/go", "base_commit": "a" * 40, "patch": "p",
    })
    assert adapter.to_tasks(swe, [inst_no_lang])[0].env.language == "python"
    assert adapter.to_tasks(mswe, [inst_no_lang])[0].env.language == ""
    assert adapter.to_tasks(mswe, [inst])[0].env.language == "go"


def test_bugsinpy_preserves_python_version_and_language():
    adapter = get_adapter("bugsinpy")
    entry = load_registry().get("bugsinpy")
    inst = adapter.normalize_instance({
        "project": "tqdm", "bug_id": 1, "buggy_commit": "c" * 40,
        "python_version": "3.8.5", "patch": "diff --git a/a.py b/a.py\n+pass\n",
        "tests": ["tests/test_a.py::test_1"],
    })
    tasks = adapter.to_tasks(entry, [inst])
    assert tasks[0].env.python_version == "3.8.5"
    assert tasks[0].env.language == "python"


def test_reviewbench_head_commit_no_window_and_supported_capabilities():
    from eval.bench.models import DatasetEntry

    adapter = get_adapter("reviewbench")
    entry = DatasetEntry(
        name="custom_rb", kind="external", adapter="reviewbench",
        role="supplementary", capabilities={"F1": 0.5, "F2": 1.0},
        license="MIT", quality_grade="B", status="approved",
    )
    inst = adapter.normalize_instance({
        "repo": "org/r", "pr_number": 12, "base_commit": "a" * 40,
        "head_commit": "d" * 40, "diff": "diff --git a/a.py b/a.py\n+x = 1\n",
        "title": "PR title", "problem_statement": "PR description",
        "gold_findings": [{"path": "a.py", "line": 1, "severity": "major"}],
    })
    tasks = adapter.to_tasks(entry, [inst])
    t = tasks[0]
    assert t.capabilities == ("F2",)
    assert t.identity.head_commit == "d" * 40
    assert t.input.problem_statement == "PR description"
    assert t.input.profile == "change"
    assert "window" not in t.to_dict()["env"]


def test_security_generates_synthetic_diff_and_uses_supported_caps():
    from eval.bench.models import DatasetEntry

    adapter = get_adapter("security")
    entry = DatasetEntry(
        name="sec_sub", kind="external", adapter="security",
        role="supplementary", capabilities={"F2": 1.0},
        license="MIT", quality_grade="B", status="approved",
    )
    inst = adapter.normalize_instance({
        "id": "s1", "repo": "org/sec", "commit": "e" * 40,
        "code": "def f(x):\n    return os.system(x)\n",
        "fixed_code": "def f(x):\n    return safe_run(x)\n",
        "vulnerable": True, "severity": "critical", "cwe": "CWE-78",
    })
    tasks = adapter.to_tasks(entry, [inst])
    t = tasks[0]
    assert t.capabilities == ("F2",)
    assert t.source_layer == "security"
    assert "-    return os.system(x)" in t.input.diff
    assert "+    return safe_run(x)" in t.input.diff


def test_core_adapter_disambiguates_core_security_and_preserves_expected_flags():
    from eval.bench.models import DatasetEntry

    adapter = get_adapter("core")
    entry = DatasetEntry(
        name="core", kind="core", adapter="core",
        role="primary", capabilities={"F2": 1.0, "F6": 1.0},
        license="internal", quality_grade="A", status="approved",
    )
    t_sec = adapter._to_task(entry, {
        "id": "security.no_new_priv", "layer": "security",
        "expect": {"block": True, "rules": [], "tier": ""},
    })
    t_gate = adapter._to_task(entry, {
        "id": "gate.approvals", "layer": "gate",
        "expect": {"block": False, "approvals": 2, "semantic": True},
    })
    assert t_sec is not None and t_sec.source_layer == "core_security"
    assert t_sec.input.case["layer"] == "security"
    assert t_gate is not None
    assert t_gate.expected.approvals == 2
    assert t_gate.expected.semantic is True
