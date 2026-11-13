"""bench 执行器与 CLI：离线跑批、产物、断点、跳过语义与退出码。"""
from __future__ import annotations

import json
from pathlib import Path

from eval.bench.cli import main
from eval.bench.registry import load_registry
from eval.bench.runner import BenchRunner, load_tasks

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "eval" / "datasets" / "bench" / "fixtures"
REGISTRY = ROOT / "eval" / "datasets" / "bench" / "registry.json"


def test_offline_core_run_writes_artifacts(tmp_path: Path):
    registry = load_registry(REGISTRY)
    tasks = load_tasks(registry, ["core"], limit=4, fixtures_dir=FIXTURES)
    assert len(tasks) >= 10  # 4 基础 case + 5 探针 + 3 reviewer 回放
    out = tmp_path / "out"
    result = BenchRunner(registry=registry, out_dir=out, offline=True).run(tasks)
    assert len(result.observations) == len(result.dedup.kept)
    for name in ("tasks.jsonl", "observations.jsonl", "dedup.json", "run_meta.json", "scores.json"):
        assert (out / name).is_file(), name
    capabilities = {record.capability for record in result.records}
    assert {"F1", "F2", "F3", "F6"} <= capabilities
    assert all(obs.status in ("ok", "skipped", "error") for obs in result.observations)


def test_external_without_live_is_skipped(tmp_path: Path):
    registry = load_registry(REGISTRY)
    tasks = load_tasks(registry, ["swebench_verified"], limit=3, fixtures_dir=FIXTURES)
    assert len(tasks) == 3
    result = BenchRunner(registry=registry, out_dir=tmp_path / "out", offline=True).run(tasks)
    assert all(obs.status == "skipped" for obs in result.observations)
    assert result.records == []


def test_cli_run_score_report_roundtrip(tmp_path: Path):
    out = tmp_path / "out"
    code = main([
        "run", "--dataset", "core", "--fixtures", "--limit", "4",
        "--out", str(out), "--registry", str(REGISTRY), "--quiet",
    ])
    assert code == 0
    assert (out / "report.json").is_file()
    assert (out / "report.md").is_file()
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["status"] in ("OK", "PARTIAL", "SUPPLEMENTARY_ONLY")
    assert report["summary"]["total"] is None
    assert report["functions"]["F2"]["score"] is not None
    assert report["run"]["profile"] == "supplement"
    assert "覆盖度" in (out / "report.md").read_text(encoding="utf-8")

    code = main(["score", "--input", str(out), "--out", str(out),
                 "--registry", str(REGISTRY), "--quiet"])
    assert code == 0
    rescored = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert rescored["functions"]["F2"]["score"] == report["functions"]["F2"]["score"]

    code = main(["report", "--input", str(out), "--quiet"])
    assert code == 0


def test_cli_external_fixture_is_skipped(tmp_path: Path):
    out = tmp_path / "external"
    code = main([
        "run", "--dataset", "swebench_verified", "--fixtures", "--limit", "3",
        "--out", str(out), "--registry", str(REGISTRY), "--quiet",
    ])
    assert code == 0
    rows = [
        json.loads(line) for line in
        (out / "observations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 3
    assert all(row["status"] == "skipped" for row in rows)
    assert all("离线" in row["skip_reason"] or "快照" in row["skip_reason"] for row in rows)


def _report(out: Path) -> dict:
    return json.loads((out / "report.json").read_text(encoding="utf-8"))


def test_cli_default_profile_is_external(tmp_path: Path):
    out = tmp_path / "external"
    code = main([
        "run", "--profile", "external", "--fixtures", "--limit", "3",
        "--out", str(out), "--registry", str(REGISTRY), "--quiet",
    ])
    assert code == 0
    report = _report(out)
    assert report["run"]["profile"] == "external"
    assert report["summary"]["status"] == "NO_EXTERNAL_EVIDENCE"
    assert report["summary"]["total"] is None
    assert report["summary"]["supplementary_score"] is None
    assert all(row["role"] == "primary" for row in report["datasets"])
    assert report["tracks"]["supplementary"]["score"] is None


def test_cli_supplement_profile_marks_self_built(tmp_path: Path):
    out = tmp_path / "supplement"
    code = main([
        "run", "--profile", "supplement", "--fixtures", "--limit", "4",
        "--out", str(out), "--registry", str(REGISTRY), "--quiet",
    ])
    assert code == 0
    report = _report(out)
    assert report["run"]["profile"] == "supplement"
    assert report["summary"]["status"] == "SUPPLEMENTARY_ONLY"
    assert report["summary"]["total"] is None
    assert report["tracks"]["primary"]["status"] == "NO_EXTERNAL_EVIDENCE"
    assert report["tracks"]["supplementary"]["score"] is not None
    assert {row["name"] for row in report["datasets"]} == {"core"}


def test_cli_full_profile_keeps_tracks_separate(tmp_path: Path):
    out = tmp_path / "full"
    code = main([
        "run", "--profile", "full", "--fixtures", "--limit", "4",
        "--out", str(out), "--registry", str(REGISTRY), "--quiet",
    ])
    assert code == 0
    report = _report(out)
    assert report["run"]["profile"] == "full"
    assert report["summary"]["status"] == "SUPPLEMENTARY_ONLY"
    assert report["summary"]["total"] is None
    assert report["evidence_mix"]["tasks"]["self_built"] >= 1
    assert report["evidence_mix"]["tasks"]["external"] >= 1
    assert "core" in {row["name"] for row in report["datasets"]}


def test_cli_dataset_overrides_profile(tmp_path: Path):
    out = tmp_path / "override"
    code = main([
        "run", "--profile", "full", "--dataset", "core", "--fixtures", "--limit", "4",
        "--out", str(out), "--registry", str(REGISTRY), "--quiet",
    ])
    assert code == 0
    report = _report(out)
    assert report["run"]["profile"] == "supplement"
    assert {row["name"] for row in report["datasets"]} == {"core"}


def test_cli_unknown_dataset_fails_softly(tmp_path: Path, capsys):
    code = main([
        "run", "--dataset", "does-not-exist", "--out", str(tmp_path / "x"),
        "--registry", str(REGISTRY), "--quiet",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "未登记" in captured.err or "未登记" in captured.out


def test_cli_fetch_list_status_and_verify(tmp_path: Path, capsys):
    assert main(["fetch", "--list", "--registry", str(REGISTRY)]) == 0
    listed = capsys.readouterr().out
    assert "core" in listed and "reviewbench" in listed

    root = tmp_path / "bench"
    assert main([
        "fetch", "--status", "--registry", str(REGISTRY), "--root", str(root),
    ]) == 0
    status_rows = json.loads(capsys.readouterr().out)
    assert any(row["name"] == "core" for row in status_rows)

    assert main([
        "fetch", "--verify", "--registry", str(REGISTRY), "--root", str(root),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_cli_score_migrates_legacy_v1_tasks_jsonl(tmp_path: Path):
    run_dir = tmp_path / "legacy_run"
    run_dir.mkdir()
    v1_tasks = [
        {
            "task_id": "reviewbench.org__r#1",
            "dataset": "reviewbench",
            "capabilities": ["F2", "F3"],
            "identity": {"repo": "org/r", "base_commit": "a" * 40, "pr_or_issue": "pr:1", "instance_id": "1"},
            "input": {
                "instance": {"head_commit": "b" * 40, "problem_statement": "desc"},
                "diff": "diff --git a/x.py b/x.py\n+x = 1\n",
                "title": "Fix",
            },
            "expected": {"gold_findings": [{"path": "x.py", "line": 1, "severity": "major", "category": "bug"}]},
            "env": {"window": 3},
            "license": "MIT",
            "quality_grade": "B",
            "source_layer": "review",
            "sample_uid": "u1",
            "role": "supplementary",
        },
        {
            "task_id": "core.security.net",
            "dataset": "core",
            "capabilities": ["F6"],
            "identity": {"repo": "core", "base_commit": "", "pr_or_issue": "security.net"},
            "input": {"case": {"id": "security.net", "layer": "security"}},
            "expected": {"block": True, "rules": [], "tier": ""},
            "env": {},
            "license": "internal",
            "quality_grade": "A",
            "source_layer": "security",
            "sample_uid": "u2",
            "role": "supplementary",
        },
    ]
    observations = [
        {
            "task_id": "reviewbench.org__r#1",
            "dataset": "reviewbench",
            "status": "ok",
            "payload": {"findings": [{"path": "x.py", "line": 1, "severity": "high"}]},
            "capabilities": ["F2", "F3"],
            "sample_uid": "u1",
            "cost_cny": 0.0,
            "wall_ms": 1.0,
            "skip_reason": "",
            "error": "",
        },
        {
            "task_id": "core.security.net",
            "dataset": "core",
            "status": "ok",
            "payload": {"layer": "security", "passed": True, "blocked": True},
            "capabilities": ["F6"],
            "sample_uid": "u2",
            "cost_cny": 0.0,
            "wall_ms": 1.0,
            "skip_reason": "",
            "error": "",
        },
    ]
    (run_dir / "tasks.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in v1_tasks),
        encoding="utf-8",
    )
    (run_dir / "observations.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in observations),
        encoding="utf-8",
    )
    (run_dir / "run_meta.json").write_text(
        json.dumps({"run_id": "r1", "started_at": "2026-11-13T00:00:00Z"}),
        encoding="utf-8",
    )
    (run_dir / "dedup.json").write_text("{}", encoding="utf-8")

    rc = main(["score", "--input", str(run_dir), "--registry", str(REGISTRY), "--quiet"])
    assert rc == 0
    scores_doc = json.loads((run_dir / "scores.json").read_text(encoding="utf-8"))
    assert any(
        row["dataset"] == "reviewbench" and row["capability"] == "F2" and row["value"] == 1.0
        for row in scores_doc["records"]
    )
    assert any(
        row["dataset"] == "core" and row["capability"] == "F6" and row["value"] == 1.0
        for row in scores_doc["records"]
    )


def test_runner_routes_core_security_to_core_not_external(tmp_path: Path):
    from eval.bench.models import Observation, TaskSpec

    registry = load_registry(REGISTRY)
    runner = BenchRunner(registry=registry, out_dir=tmp_path / "run", offline=True)
    calls: list[str] = []

    runner._run_core = lambda task, repeat_index: (  # type: ignore[method-assign]
        calls.append(task.source_layer)
        or Observation(task_id=task.task_id, dataset=task.dataset, status="ok", payload={"passed": True})
    )
    runner._run_external = lambda task: (  # type: ignore[method-assign]
        calls.append("external")
        or Observation(task_id=task.task_id, dataset=task.dataset, status="ok", payload={})
    )

    task = TaskSpec(
        task_id="core.security.probe1", dataset="core", capabilities=("F6",),
        identity={"repo": "core", "pr_or_issue": "security.probe1"},
        input={"case": {"id": "security.probe1", "layer": "security"}},
        expected={"block": True}, env={},
        license="internal", quality_grade="A", source_layer="core_security",
        role="supplementary",
    )
    obs = runner._run_task(task, 0)
    assert obs.status == "ok"
    assert calls == ["core_security"]
