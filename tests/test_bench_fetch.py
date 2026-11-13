"""数据集物化：HF/本地切片、哈希校验、许可证闸门、git 快照与状态。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from eval.bench.fetch import (
    FetchError,
    build_dedup_index,
    resolve_source,
    slice_dataset,
    snapshot_one,
    status,
    verify,
)
from eval.bench.registry import load_registry

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "eval" / "datasets" / "bench" / "registry.json"
FIXTURES = ROOT / "eval" / "datasets" / "bench" / "fixtures"


def test_resolve_hf_source():
    url = resolve_source("hf://princeton-nlp/SWE-bench_Verified/data/test.jsonl")
    assert url == (
        "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified"
        "/resolve/main/data/test.jsonl"
    )
    assert resolve_source("hf://org/name@v2/data/x.jsonl").endswith(
        "/resolve/v2/data/x.jsonl"
    )
    assert resolve_source("https://example.com/x.jsonl") == "https://example.com/x.jsonl"


def test_slice_local_jsonl_and_verify(tmp_path: Path):
    registry = load_registry(REGISTRY)
    source = tmp_path / "test.jsonl"
    rows = [
        json.loads(line) for line in
        (FIXTURES / "swebench_sample.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    source.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows[:2]) + "\n",
        encoding="utf-8",
    )
    out_root = tmp_path / "bench"
    entry = slice_dataset(registry, "swebench_verified", source, limit=2, out_root=out_root)
    assert entry["count"] == 2
    instances = out_root / entry["instances_file"]
    assert len(instances.read_text(encoding="utf-8").splitlines()) == 2
    assert entry["sha256"]
    assert verify(registry, root=out_root) == []

    instances.write_text(instances.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    problems = verify(registry, root=out_root)
    assert any("哈希" in item or "行数" in item for item in problems)


def test_unlicensed_dataset_is_refused(tmp_path: Path):
    registry = load_registry(REGISTRY)
    source = tmp_path / "devign.jsonl"
    rows = [
        json.loads(line) for line in
        (FIXTURES / "security_sample.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    source.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(FetchError, match="许可证"):
        slice_dataset(registry, "devign", source, out_root=tmp_path / "bench")
    entry = slice_dataset(
        registry, "devign", source, out_root=tmp_path / "bench", allow_unlicensed=True,
    )
    assert entry["license_override"] is True
    assert entry["count"] == 3


def test_snapshot_one_local_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    target = tmp_path / "snapshots" / "demo"
    entry = snapshot_one(repo.as_posix(), sha, target)
    assert entry["ok"] is True
    assert (target / "calc.py").is_file()
    assert entry["commit"] == sha

    again = snapshot_one(repo.as_posix(), sha, target)
    assert again["ok"] is True
    assert again.get("reused") is True


def test_status_reads_materialized_counts(tmp_path: Path):
    registry = load_registry(REGISTRY)
    source = FIXTURES / "swebench_sample.jsonl"
    out_root = tmp_path / "bench"
    slice_dataset(registry, "swebench_verified", source, limit=2, out_root=out_root)
    rows = status(registry, root=out_root)
    row = next(item for item in rows if item["name"] == "swebench_verified")
    assert row["materialized"] is True
    assert row["count"] == 2
    assert row["snapshot_ready"] is False


def test_build_dedup_index_marks_overlap(tmp_path: Path):
    registry = load_registry(REGISTRY)
    out_root = tmp_path / "bench"
    slice_dataset(registry, "swebench_verified", FIXTURES / "swebench_sample.jsonl",
                  out_root=out_root)
    slice_dataset(registry, "swebench_lite", FIXTURES / "swebench_sample.jsonl",
                  out_root=out_root)
    index = build_dedup_index(registry, root=out_root, datasets=["swebench_verified", "swebench_lite"])
    assert index["kept"] == 3
    assert index["overlap"].get("swebench_lite|swebench_verified") == 3
