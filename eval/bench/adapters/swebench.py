"""SWE-bench 系列适配器：Issue→Patch→测试，主指标 resolve_rate / pass_to_pass_rate。

适配器只做归一化与聚合；真正执行在 `eval.bench.harness`。
`patch_source=gold` 用于证明 harness 正确，`system` 才衡量 CodeGuard 的修复能力。
"""
from __future__ import annotations

from pathlib import Path

from ..metrics import ratio
from ..models import (
    DatasetEntry,
    FixExpected,
    FixInput,
    Observation,
    ScoreRecord,
    TaskEnv,
    TaskIdentity,
    TaskSpec,
)
from ..paths import BENCH_ROOT
from .base import as_list, entry_capabilities, first_present, load_jsonl, rate_record


class SwebenchAdapter:
    name = "swebench"

    # ── 归一化 ──

    def normalize_instance(self, raw: dict) -> dict:
        instance_id = str(first_present(raw, "instance_id", "id", default="")).strip()
        if not instance_id:
            raise ValueError("SWE-bench 记录缺少 instance_id")
        return {
            "instance_id": instance_id,
            "repo": str(first_present(raw, "repo", default="")).strip(),
            "base_commit": str(first_present(raw, "base_commit", "base_sha", default="")).strip(),
            "patch": str(first_present(raw, "patch", "model_patch", default="")),
            "test_patch": str(first_present(raw, "test_patch", default="")),
            "problem_statement": str(
                first_present(raw, "problem_statement", "problem", "title", default="")
            )[:4000],
            "title": str(first_present(raw, "title", default="")),
            "fail_to_pass": [str(item) for item in as_list(
                first_present(raw, "FAIL_TO_PASS", "fail_to_pass", default=[])
            )],
            "pass_to_pass": [str(item) for item in as_list(
                first_present(raw, "PASS_TO_PASS", "pass_to_pass", default=[])
            )],
            "version": str(first_present(raw, "version", default="")),
            "python_version": str(first_present(raw, "python_version", default="")).strip(),
            "language": str(first_present(raw, "language", "lang", default="")).strip().lower(),
            "environment_setup_commit": str(
                first_present(raw, "environment_setup_commit", default="")
            ),
            "test_command": str(first_present(raw, "test_command", default="python -m pytest -q")),
            "image": str(first_present(raw, "image", default="")),
            "prepare_commands": [
                str(item) for item in as_list(
                    first_present(raw, "prepare_commands", "install_commands", default=[])
                )
            ],
        }

    def load_instances(self, entry: DatasetEntry, *, root: Path | str | None = None,
                       limit: int = 0) -> list[dict]:
        path = self._instances_path(entry, root)
        if path is None:
            return []
        rows = [self.normalize_instance(item) for item in load_jsonl(path)]
        return rows[:limit] if limit and limit > 0 else rows

    @staticmethod
    def _instances_path(entry: DatasetEntry, root: Path | str | None) -> Path | None:
        if root is not None:
            candidate = Path(root)
            if candidate.is_file():
                return candidate
            for name in ("instances.jsonl", f"{entry.name}.jsonl"):
                nested = candidate / name
                if nested.is_file():
                    return nested
            return None
        if entry.instances_file:
            candidate = BENCH_ROOT / entry.instances_file
            if candidate.is_file():
                return candidate
        return None

    # ── 任务投影 ──

    def to_tasks(self, entry: DatasetEntry, instances: list[dict]) -> list[TaskSpec]:
        tasks: list[TaskSpec] = []
        capabilities = entry_capabilities(entry, ("F4", "F5"))
        for raw in instances:
            instance_id = str(raw.get("instance_id") or "")
            if not instance_id:
                continue
            language = str(raw.get("language") or "") or (
                "" if entry.name == "multi_swe_bench" else "python"
            )
            tasks.append(TaskSpec(
                task_id=f"{entry.name}.{instance_id}",
                dataset=entry.name,
                capabilities=capabilities,
                identity=TaskIdentity(
                    repo=str(raw.get("repo") or ""),
                    base_commit=str(raw.get("base_commit") or ""),
                    pr_or_issue=f"instance:{instance_id}",
                    instance_id=instance_id,
                ),
                input=FixInput(
                    problem_statement=str(raw.get("problem_statement") or ""),
                    patch=str(raw.get("patch") or ""),
                    test_patch=str(raw.get("test_patch") or ""),
                    title=str(raw.get("title") or ""),
                    profile="bugfix",
                ),
                expected=FixExpected(
                    fail_to_pass=tuple(str(item) for item in (raw.get("fail_to_pass") or [])),
                    pass_to_pass=tuple(str(item) for item in (raw.get("pass_to_pass") or [])),
                ),
                env=TaskEnv(
                    image=(
                        str(raw.get("image") or "")
                        or entry.image
                        or "codeguard-sandbox-python:3.12"
                    ),
                    language=language,
                    version=str(raw.get("version") or ""),
                    python_version=str(raw.get("python_version") or ""),
                    environment_setup_commit=str(raw.get("environment_setup_commit") or ""),
                    prepare_commands=tuple(str(item) for item in (raw.get("prepare_commands") or [])),
                    test_command=str(raw.get("test_command") or entry.test_command),
                ),
                license=entry.license,
                quality_grade=entry.quality_grade,
                source_layer="fix",
                role=entry.role,
            ))
        return tasks

    # ── 聚合 ──

    def aggregate(self, entry: DatasetEntry, tasks: list[TaskSpec],
                  observations: list[Observation]) -> list[ScoreRecord]:
        by_id = {task.task_id: task for task in tasks}
        live = [
            obs for obs in observations
            if obs.status == "ok" and obs.task_id in by_id
        ]
        if not live:
            return []
        anchor = by_id[live[0].task_id]
        records: list[ScoreRecord] = []

        # F1 外部探针：补丁解析路径 vs 实际变更路径，不额外依赖数据集。
        probes = [
            obs.payload.get("f1_probe") for obs in live
            if isinstance(obs.payload.get("f1_probe"), dict)
        ]
        if probes:
            passed = sum(1 for item in probes if item.get("passed"))
            records.append(rate_record(
                task=anchor, capability="F1", metric="probe_accuracy",
                successes=passed, total=len(probes), sample_size=len(probes),
            ))

        judged = [obs for obs in live if "resolved" in obs.payload]
        if judged:
            resolved = sum(1 for obs in judged if bool(obs.payload.get("resolved")))
            records.append(rate_record(
                task=anchor, capability="F4", metric="resolve_rate",
                successes=resolved, total=len(judged), sample_size=len(judged),
            ))
            applied = sum(1 for obs in judged if bool(obs.payload.get("applied")))
            records.append(rate_record(
                task=anchor, capability="F4", metric="patch_apply_rate",
                successes=applied, total=len(judged), sample_size=len(judged),
            ))

        p2p_total = p2p_pass = 0
        for obs in live:
            nodes = obs.payload.get("pass_to_pass") or {}
            if isinstance(nodes, dict):
                for status in nodes.values():
                    if status == "invalid":
                        continue  # 数据集 nodeid 被截断，不计入分母
                    p2p_total += 1
                    if status is True or status == "passed":
                        p2p_pass += 1
        if p2p_total:
            records.append(rate_record(
                task=anchor, capability="F5", metric="pass_to_pass_rate",
                successes=p2p_pass, total=p2p_total, sample_size=len(live),
            ))

        f2p_total = f2p_pass = 0
        for obs in live:
            nodes = obs.payload.get("fail_to_pass") or {}
            if isinstance(nodes, dict):
                for status in nodes.values():
                    if status == "invalid":
                        continue
                    f2p_total += 1
                    if status is True or status == "passed":
                        f2p_pass += 1
        if f2p_total:
            records.append(rate_record(
                task=anchor, capability="F5", metric="fail_to_pass_rate",
                successes=f2p_pass, total=f2p_total, sample_size=len(live),
            ))
        return records


def aggregate_patch_results(observations: list[Observation]) -> dict:
    """给 harness/报告复用的纯汇总：不依赖 registry。"""
    resolved = [bool(obs.payload.get("resolved")) for obs in observations if obs.status == "ok"]
    return {
        "tasks": len(resolved),
        "resolved": sum(resolved),
        "resolve_rate": round(ratio(sum(resolved), len(resolved)), 6),
    }


__all__ = ["SwebenchAdapter", "aggregate_patch_results"]
