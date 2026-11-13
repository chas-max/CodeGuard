"""外部 harness：工作区、补丁应用、pytest 解析、修复/审查/安全任务。"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from codeguard.models import SandboxStatus
from codeguard.sandbox.models import SandboxResult
from eval.bench.harness.fix_task import build_f1_probe, run_fix_task, workspace_changed_paths
from eval.bench.harness.patch_source import resolve_patch
from eval.bench.harness.pytest_runner import prepare_environment, run_tests
from eval.bench.harness.review_task import run_review_task
from eval.bench.harness.security_task import run_security_task
from eval.bench.harness.workspace import apply_candidate_patch, prepare_workspace
from eval.bench.models import TaskSpec
from tests.fakes import FakeLLM, FakeProvider, FakeSandbox, init_git

BASE = "def add(a, b):\n    return a - b\n"
PATCH = (
    "diff --git a/calc.py b/calc.py\n"
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
)


def _fix_task(commit: str = "a" * 40) -> TaskSpec:
    return TaskSpec(
        task_id="swebench_verified.demo__repo-1", dataset="swebench_verified",
        capabilities=("F4", "F5"),
        identity={"repo": "demo/repo", "base_commit": commit,
                  "pr_or_issue": "instance:demo__repo-1"},
        input={"patch": PATCH, "test_patch": ""},
        expected={
            "fail_to_pass": ["tests/test_calc.py::test_add"],
            "pass_to_pass": ["tests/test_calc.py::test_ok"],
        },
        env={"image": "img", "test_command": "python -m pytest -q"},
        license="MIT", quality_grade="A", source_layer="fix",
    )


def _snapshot(tmp_path: Path, commit: str = "a" * 40) -> Path:
    snapshot = tmp_path / "snapshots" / f"demo__repo@{commit[:12]}"
    snapshot.mkdir(parents=True)
    (snapshot / "calc.py").write_text(BASE, encoding="utf-8")
    init_git(snapshot)
    return snapshot


def test_workspace_apply_patch(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    workspace = prepare_workspace(snapshot, tmp_path / "work")
    ok, output = apply_candidate_patch(workspace, PATCH)
    assert ok, output
    assert "a + b" in (workspace.root / "calc.py").read_text(encoding="utf-8")
    workspace.cleanup()
    assert not workspace.root.exists()


def test_prepare_workspace_tracks_snapshot_baseline(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    workspace = prepare_workspace(snapshot, tmp_path / "work")
    # 快照内容已进索引，未改动的文件不应出现在 git status 里。
    assert workspace_changed_paths(workspace.root) == []


def test_build_f1_probe_matches_applied_paths(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    workspace = prepare_workspace(snapshot, tmp_path / "work")
    ok, output = apply_candidate_patch(workspace, PATCH)
    assert ok, output
    probe = build_f1_probe(workspace.root, PATCH)
    assert probe["expected"] == ["calc.py"]
    assert probe["actual"] == ["calc.py"]
    assert probe["passed"] is True


def test_build_f1_probe_detects_path_mismatch(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    workspace = prepare_workspace(snapshot, tmp_path / "work")
    apply_candidate_patch(workspace, PATCH)
    other = PATCH.replace("calc.py", "docs/guide.md")
    probe = build_f1_probe(workspace.root, other)
    assert probe["expected"] == ["docs/guide.md"]
    assert probe["actual"] == ["calc.py"]
    assert probe["passed"] is False


def test_build_f1_probe_reports_invalid_patch(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    workspace = prepare_workspace(snapshot, tmp_path / "work")
    probe = build_f1_probe(
        workspace.root,
        "diff --git a/x.py b/x.py\n--- a/../../etc/passwd\n+++ b/../../etc/passwd\n",
    )
    assert probe["passed"] is False
    assert probe["error"]


def test_run_fix_task_records_f1_probe(tmp_path: Path):
    _snapshot(tmp_path)

    def handler(spec):
        return SandboxResult(
            status=SandboxStatus.passed, exit_code=0,
            stdout=(
                "PASSED tests/test_calc.py::test_add\n"
                "PASSED tests/test_calc.py::test_ok\n"
            ),
        )

    status, payload = run_fix_task(
        _fix_task(), snapshot_root=tmp_path / "snapshots",
        sandbox=FakeSandbox(handler), work_root=tmp_path / "work",
        patch_source="gold",
    )
    assert status == "ok"
    assert payload["f1_probe"]["passed"] is True
    assert payload["f1_probe"]["expected"] == ["calc.py"]
    assert payload["f1_probe"]["actual"] == ["calc.py"]


def test_pytest_runner_with_fake_sandbox(tmp_path: Path):
    def handler(spec):
        assert spec.network == "none"
        return SandboxResult(
            status=SandboxStatus.passed, exit_code=0,
            stdout=(
                "PASSED tests/test_calc.py::test_add\n"
                "FAILED tests/test_calc.py::test_ok - AssertionError\n"
            ),
        )

    run = run_tests(
        FakeSandbox(handler), workspace=tmp_path, image="img",
        nodes=["tests/test_calc.py::test_add", "tests/test_calc.py::test_ok"],
    )
    assert run.infra_error == ""
    assert run.statuses["tests/test_calc.py::test_add"] == "passed"
    assert run.statuses["tests/test_calc.py::test_ok"] == "failed"
    assert "PASSED tests/test_calc.py::test_add" in run.logs


def test_pytest_runner_marks_truncated_nodeids_invalid(tmp_path: Path):
    calls: list[str] = []

    def handler(spec):
        calls.append(spec.commands[0])
        return SandboxResult(
            status=SandboxStatus.passed, exit_code=0,
            stdout="PASSED tests/test_a.py::test_ok\n",
        )

    run = run_tests(
        FakeSandbox(handler), workspace=tmp_path, image="img",
        nodes=["tests/test_a.py::test_ok", "tests/test_a.py::test_param[assert"],
    )
    assert run.statuses["tests/test_a.py::test_ok"] == "passed"
    assert run.statuses["tests/test_a.py::test_param[assert"] == "invalid"
    assert "test_param" not in calls[0]
    assert "test_ok" in calls[0]


def test_pytest_runner_falls_back_to_files_on_usage_error(tmp_path: Path):
    calls: list[str] = []

    def handler(spec):
        command = spec.commands[0]
        calls.append(command)
        if "::" in command:
            return SandboxResult(
                status=SandboxStatus.failed, exit_code=4,
                stdout="ERROR: not found: tests/test_a.py::test_gone\n",
            )
        return SandboxResult(
            status=SandboxStatus.passed, exit_code=0,
            stdout="PASSED tests/test_a.py::test_ok\n",
        )

    run = run_tests(
        FakeSandbox(handler), workspace=tmp_path, image="img",
        nodes=["tests/test_a.py::test_ok", "tests/test_a.py::test_gone"],
    )
    assert run.statuses["tests/test_a.py::test_ok"] == "passed"
    assert run.statuses["tests/test_a.py::test_gone"] == "missing"
    assert any("::" not in command for command in calls)


def test_run_fix_task_marks_import_failure_as_env_error(tmp_path: Path):
    _snapshot(tmp_path)

    def handler(spec):
        return SandboxResult(
            status=SandboxStatus.failed, exit_code=1,
            stdout=(
                "Traceback (most recent call last):\n"
                "  File \"src/pytest/__init__.py\", line 3, in <module>\n"
                "ModuleNotFoundError: No module named 'x'\n"
            ),
        )

    status, payload = run_fix_task(
        _fix_task(), snapshot_root=tmp_path / "snapshots",
        sandbox=FakeSandbox(handler), work_root=tmp_path / "work",
        patch_source="gold",
    )
    assert status == "error"
    assert payload["stage"] == "tests"
    assert payload["env_error"] is True
    # 环境失败不计入分母，但 F1 探针结论仍然保留
    assert payload["f1_probe"]["passed"] is True


def test_prepare_environment_uses_prepare_network(tmp_path: Path):
    def handler(spec):
        assert spec.network == "prepare"
        return SandboxResult(
            status=SandboxStatus.passed, exit_code=0, stdout="prepared version file",
        )

    run = prepare_environment(
        FakeSandbox(handler), workspace=tmp_path, image="img",
        commands=["printf 'version = 1\\n' > version.py"],
    )
    assert run.infra_error == ""
    assert run.exit_code == 0
    assert "prepared version file" in run.logs


def test_run_fix_task_runs_prepare_commands(tmp_path: Path):
    _snapshot(tmp_path)
    calls: list[str] = []

    def handler(spec):
        calls.append(spec.network)
        if spec.network == "prepare":
            return SandboxResult(
                status=SandboxStatus.passed, exit_code=0, stdout="prepared",
            )
        return SandboxResult(
            status=SandboxStatus.passed, exit_code=0,
            stdout=(
                "PASSED tests/test_calc.py::test_add\n"
                "PASSED tests/test_calc.py::test_ok\n"
            ),
        )

    base_task = _fix_task()
    task = replace(base_task, env={
        **base_task.env.model_dump(), "prepare_commands": ["printf 'v=1\\n' > prepared.txt"],
    })
    status, payload = run_fix_task(
        task, snapshot_root=tmp_path / "snapshots",
        sandbox=FakeSandbox(handler), work_root=tmp_path / "work",
        patch_source="gold",
    )
    assert status == "ok"
    assert payload["resolved"] is True
    assert "prepare" in calls
    assert "PASSED tests/test_calc.py::test_add" in payload["test_logs"]


def test_resolve_gold_patch_and_missing_system_provider():
    task = _fix_task()
    gold = resolve_patch(task, "gold")
    assert gold.ok and gold.patch == PATCH
    system = resolve_patch(task, "system", provider=None)
    assert system.ok is False
    assert "provider" in system.error


def test_resolve_system_patch_with_fake_provider():
    response = json.dumps({
        "patch": PATCH, "files": ["calc.py"], "explanation": "fix add", "confidence": 0.8,
    })
    provider = FakeProvider({"fixer": [response]})
    result = resolve_patch(_fix_task(), "system", provider=provider)
    assert result.ok is True
    assert result.patch == PATCH
    assert result.source == "system"


def test_run_fix_task_resolved(tmp_path: Path):
    _snapshot(tmp_path)

    def handler(spec):
        return SandboxResult(
            status=SandboxStatus.passed, exit_code=0,
            stdout=(
                "PASSED tests/test_calc.py::test_add\n"
                "PASSED tests/test_calc.py::test_ok\n"
            ),
        )

    status, payload = run_fix_task(
        _fix_task(), snapshot_root=tmp_path / "snapshots",
        sandbox=FakeSandbox(handler), work_root=tmp_path / "work",
        patch_source="gold",
    )
    assert status == "ok"
    assert payload["applied"] is True
    assert payload["resolved"] is True
    assert payload["fail_to_pass"]["tests/test_calc.py::test_add"] == "passed"
    assert payload["pass_to_pass"]["tests/test_calc.py::test_ok"] == "passed"


def test_run_fix_task_unresolved_mask(tmp_path: Path):
    _snapshot(tmp_path)

    def handler(spec):
        return SandboxResult(
            status=SandboxStatus.passed, exit_code=1,
            stdout="FAILED tests/test_calc.py::test_add - AssertionError\n",
        )

    status, payload = run_fix_task(
        _fix_task(), snapshot_root=tmp_path / "snapshots",
        sandbox=FakeSandbox(handler), work_root=tmp_path / "work",
        patch_source="gold",
    )
    assert status == "ok"
    assert payload["resolved"] is False
    assert payload["fail_to_pass"]["tests/test_calc.py::test_add"] == "failed"


def test_run_fix_task_missing_snapshot_is_skipped(tmp_path: Path):
    status, payload = run_fix_task(
        _fix_task(), snapshot_root=tmp_path / "snapshots",
        sandbox=FakeSandbox(), work_root=tmp_path / "work",
    )
    assert status == "skipped"
    assert payload["stage"] == "snapshot"


def test_run_review_task_with_fake_provider(tmp_path: Path):
    response = json.dumps({
        "verdict": "request_changes", "confidence": 0.9, "summary": "null deref",
        "findings": [{
            "category": "correctness", "severity": "critical", "blocking": True,
            "path": "src/a.py", "line": 10, "message": "user 可能为 None",
            "evidence": "src/a.py:10 user.name", "suggestion": "判空",
            "introduced_by_pr": True,
        }],
        "fix_instructions": [], "residual_risks": [],
    })
    provider = FakeProvider({"reviewer": [response]})
    task = TaskSpec(
        task_id="reviewbench.rb-1", dataset="reviewbench",
        capabilities=("F2", "F3"),
        identity={"repo": "demo/repo", "base_commit": "b" * 40, "pr_or_issue": "pr:101"},
        input={"diff": (
            "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
            "@@ -8,3 +8,4 @@\n def f(user):\n+    name = user.name\n     return name\n"
        )},
        expected={}, env={}, license="UNKNOWN", quality_grade="A",
        source_layer="review",
    )
    status, payload = run_review_task(task, provider=provider, work_root=tmp_path / "review")
    assert status == "ok"
    assert payload["verdict"] == "request_changes"
    assert payload["findings"][0]["path"] == "src/a.py"


def test_run_security_task_detects_dangerous_call(tmp_path: Path):
    task = TaskSpec(
        task_id="devign.sec-1", dataset="devign", capabilities=("F2",),
        identity={"repo": "demo/repo", "base_commit": "c" * 40, "pr_or_issue": "x"},
        input={"code": "import os\n\ndef run(cmd):\n    return os.system(cmd)\n"},
        expected={"vulnerable": True}, env={},
        license="UNKNOWN", quality_grade="B", source_layer="security",
    )
    status, payload = run_security_task(task, work_root=tmp_path / "security")
    assert status == "ok"
    assert payload["vulnerable"] is True
    assert payload["findings"]


def test_run_review_task_forwards_claim_context(tmp_path: Path):
    response = json.dumps({
        "verdict": "approve", "confidence": 0.9, "summary": "looks good",
        "findings": [], "fix_instructions": [], "residual_risks": [],
    })
    llm = FakeLLM([response])
    task = TaskSpec(
        task_id="reviewbench.rb-ctx", dataset="reviewbench",
        capabilities=("F2", "F3"),
        identity={"repo": "demo/repo", "base_commit": "b" * 40, "pr_or_issue": "pr:102"},
        input={
            "diff": "diff --git a/app.py b/app.py\n@@ -1 +1 @@\n-return 1\n+return 2\n",
            "title": "Fix division guard",
            "problem_statement": "Reject zero denominator before dividing",
            "profile": "bugfix",
        },
        expected={"gold_findings": []}, env={},
        license="MIT", quality_grade="B", source_layer="review",
    )
    status, _ = run_review_task(task, provider=lambda _role: llm, work_root=tmp_path / "review_ctx")
    assert status == "ok"
    prompt = llm.prompts[0]
    assert "Fix division guard" in prompt
    assert "Reject zero denominator before dividing" in prompt


def test_resolve_patch_system_reads_problem_statement_without_instance():
    fix_json = json.dumps({"patch": PATCH, "rationale": "add", "touched_files": ["calc.py"]})
    llm = FakeLLM([fix_json])
    task = TaskSpec(
        task_id="swebench_verified.demo__repo-2", dataset="swebench_verified",
        capabilities=("F4", "F5"),
        identity={"repo": "demo/repo", "base_commit": "a" * 40, "instance_id": "demo__repo-2"},
        input={"problem_statement": "Fix addition off-by-one bug"},
        expected={"fail_to_pass": ["tests/test_calc.py::test_add"]},
        env={}, license="MIT", quality_grade="A", source_layer="fix",
    )
    result = resolve_patch(task, source="system", provider=lambda _role: llm)
    assert result.ok is True
    assert "Fix addition off-by-one bug" in llm.prompts[0]


def test_run_security_task_respects_custom_path(tmp_path: Path):
    task = TaskSpec(
        task_id="devign.sec-path", dataset="devign", capabilities=("F2",),
        identity={"repo": "demo/repo", "base_commit": "c" * 40, "pr_or_issue": "x"},
        input={
            "code": "import os\n\ndef run(cmd):\n    return os.system(cmd)\n",
            "path": "sub/mod.py",
        },
        expected={"vulnerable": True}, env={},
        license="MIT", quality_grade="B", source_layer="security",
    )
    status, payload = run_security_task(task, work_root=tmp_path / "security_path")
    assert status == "ok"
    assert payload["vulnerable"] is True
    assert (tmp_path / "security_path" / "devign.sec-path" / "sub" / "mod.py").is_file()
