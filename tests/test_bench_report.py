"""报告层：三轨分栏、覆盖矩阵、证据构成、缺维不输出总结果与 secret 脱敏。"""
from __future__ import annotations

from eval.bench.dedup import DedupResult
from eval.bench.models import DatasetEntry, Observation, RunMeta, ScoreRecord
from eval.bench.registry import Registry
from eval.bench.report import build_report, mask_secrets, render_markdown, write_report
from eval.bench.scoring import evaluate
from eval.bench.thresholds import load_thresholds

_REQUIRED = ("F1", "F2", "F3", "F4", "F5")


def _entry(name: str, caps: dict[str, float], *, role: str = "primary",
           kind: str = "external", license: str = "MIT") -> DatasetEntry:
    return DatasetEntry(
        name=name, adapter="core", kind=kind, role=role, license=license,
        status="approved", quality_grade="A", capabilities=caps,
        primary_metrics={cap: "f1" for cap in caps},
    )


def _registry(*entries: DatasetEntry) -> Registry:
    return Registry(entries=tuple(entries))


def _full_primary_registry() -> Registry:
    return _registry(_entry("all", {cap: 1.0 for cap in _REQUIRED + ("F6",)}))


def _records(dataset: str = "all", values: dict[str, float] | None = None) -> list[ScoreRecord]:
    values = values or {
        "F1": 0.85, "F2": 0.796, "F3": 0.71, "F4": 0.60, "F5": 0.934, "F6": 0.90,
    }
    return [
        ScoreRecord(
            task_id=f"{dataset}.{cap}", dataset=dataset, capability=cap, metric="f1",
            numerator=value * 10, denominator=10, value=value, sample_size=20,
        )
        for cap, value in values.items()
    ]


def _build(registry: Registry, records: list[ScoreRecord], observations=None,
           patch_source: str = "") -> dict:
    return build_report(
        evaluation=evaluate(records, registry, load_thresholds()),
        records=records, registry=registry,
        run_meta=RunMeta(
            run_id="r1", started_at="2026-11-12T00:00:00", profile="external",
            patch_source=patch_source,
        ),
        dedup=DedupResult(), thresholds=load_thresholds(),
        observations=observations or [],
    )


def test_build_report_and_markdown():
    registry = _full_primary_registry()
    records = _records()
    report = _build(registry, records, observations=[
        Observation(task_id="t", dataset="all", status="skipped", skip_reason="no snapshot"),
    ])
    assert report["summary"]["status"] == "OK"
    assert report["summary"]["overall"] == "PASS"
    assert report["summary"]["total"] == report["summary"]["primary_score"]
    assert report["functions"]["F2"]["score"] == 79.6
    assert report["coverage"]["value"] == 1.0
    assert report["skipped"][0]["task_id"] == "t"
    assert report["tracks"]["primary"]["status"] == "OK"

    text = render_markdown(report)
    assert "# CodeGuard 评测平台报告" in text
    assert "## 外部主分（Primary）" in text
    assert "## 自建补充分（Supplementary）" in text
    assert "## 诊断（Diagnostic）" in text
    assert "## 覆盖矩阵" in text
    assert "## 证据构成（Evidence mix）" in text
    assert "覆盖度" in text
    assert "跳过" in text


def test_missing_dimension_renders_dash_total():
    registry = _registry(_entry("all", {cap: 1.0 for cap in ("F1", "F2", "F3", "F5")}))
    records = _records(values={"F1": 0.85, "F2": 0.796, "F3": 0.71, "F5": 0.934})
    report = _build(registry, records)
    assert report["summary"]["status"] == "PARTIAL"
    assert report["summary"]["total"] is None
    assert report["summary"]["partial"] is not None
    text = render_markdown(report)
    assert "总结果：—" in text
    assert "缺少 F4" in text
    # 已有维度仍然渲染出来
    assert "| F2 | 缺陷检测与定位 | 79.60 |" in text
    assert "| F4 | 自动代码修复 | — |" in text


def test_supplementary_only_report_is_marked():
    registry = _registry(
        _entry("core", {"F1": 1.0, "F2": 1.0, "F3": 1.0, "F6": 1.0},
               role="supplementary", kind="core", license="internal"),
    )
    records = _records("core", {"F1": 0.9, "F2": 0.95, "F3": 1.0, "F6": 0.96})
    report = _build(registry, records)
    assert report["summary"]["status"] == "SUPPLEMENTARY_ONLY"
    assert report["summary"]["overall"] == "SELF_BUILT_ONLY"
    assert report["summary"]["total"] is None
    assert report["tracks"]["primary"]["status"] == "NO_EXTERNAL_EVIDENCE"
    assert report["tracks"]["supplementary"]["score"] is not None
    assert report["tracks"]["integration"]["score"] is not None
    text = render_markdown(report)
    assert "SELF_BUILT_ONLY" in text
    assert "不得与主分相加" in text


def test_coverage_matrix_lists_sources_and_licenses():
    registry = _full_primary_registry()
    report = _build(registry, _records())
    matrix = {row["capability"]: row for row in report["coverage_matrix"]}
    assert set(matrix) == {"F1", "F2", "F3", "F4", "F5", "F6"}
    assert matrix["F2"]["sources"][0]["dataset"] == "all"
    assert matrix["F2"]["sources"][0]["license"] == "MIT"
    assert matrix["F2"]["samples"] == 20
    assert matrix["F2"]["target"] == 20
    assert matrix["F6"]["track"] == "integration"


def test_coverage_matrix_marks_missing_dimension():
    registry = _registry(_entry("all", {cap: 1.0 for cap in ("F1", "F2")}))
    report = _build(registry, _records(values={"F1": 0.85, "F2": 0.80}))
    matrix = {row["capability"]: row for row in report["coverage_matrix"]}
    assert matrix["F3"]["sources"] == []
    assert matrix["F3"]["status"] == "missing"
    assert matrix["F3"]["licenses"] == []


def test_evidence_mix_splits_external_and_self_built():
    registry = _registry(
        _entry("ext", {"F2": 1.0}),
        _entry("core", {"F2": 1.0}, role="supplementary", kind="core", license="internal"),
    )
    records = _records("ext", {"F2": 0.80}) + _records("core", {"F2": 0.90})
    report = _build(registry, records, observations=[
        Observation(task_id="a", dataset="ext", status="ok"),
        Observation(task_id="b", dataset="core", status="ok"),
    ])
    mix = report["evidence_mix"]
    assert mix["external"]["samples"] == 20
    assert mix["self_built"]["samples"] == 20
    assert mix["observations"] == {"external": 1, "self_built": 1}
    assert mix["external"]["share"] + mix["self_built"]["share"] == 1.0


def test_warnings_include_env_error_count():
    registry = _full_primary_registry()
    observations = [
        Observation(task_id="t1", dataset="all", status="error",
                    payload={"env_error": True}, error="import failed"),
    ]
    report = _build(registry, _records(), observations=observations)
    assert any("env_error" in item for item in report["warnings"])


def test_gold_patch_is_flagged_in_warnings():
    report = _build(_full_primary_registry(), _records(), patch_source="gold")
    assert any("gold patch" in item for item in report["warnings"])
    assert "gold patch" in render_markdown(report)


def test_mask_secrets():
    text = "TOKEN=AKIAABCDEFGHIJKLMNOP ghp_" + "a" * 36 + " sk-" + "b" * 32
    masked = mask_secrets(text)
    assert "AKIAABCDEFGHIJKLMNOP" not in masked
    assert "ghp_" + "a" * 36 not in masked
    assert "sk-" + "b" * 32 not in masked


def test_write_report_is_atomic(tmp_path):
    registry = _full_primary_registry()
    records = _records()
    report = _build(registry, records)
    out = write_report(tmp_path / "out", report)
    assert (out / "report.json").is_file()
    assert (out / "report.md").is_file()
    assert not list((out).glob("*.tmp"))
