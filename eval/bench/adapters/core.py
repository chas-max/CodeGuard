"""自建核心集适配器：把分层 CaseSpec / reviewer 回放 / F1 探针投影成统一任务。

核心集是唯一默认离线可跑的证据源；它不覆盖自动修复（F4），F4 由 SWE-bench /
BugsInPy 等外部基准提供，缺失时报告会显式标记 INCOMPLETE。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..metrics import prf, wilson
from ..models import DatasetEntry, Observation, ScoreRecord, TaskSpec
from ..paths import CORE_SUITE, REVIEWER_CASSETTE
from ..probes import probe_instances
from .base import load_jsonl, metric_record, rate_record

_LAYER_CAPS = {
    "det": ("F2",),
    "semantic": ("F2",),
    "gate": ("F6",),
    "security": ("F6",),
    "e2e": ("F5", "F6"),
    "external": ("F2",),
}


class CoreAdapter:
    name = "core"

    # ── 加载 ──

    def load_instances(self, entry: DatasetEntry, *, root: Path | str | None = None,
                       limit: int = 0) -> list[dict]:
        base: list[dict] = []
        source = self._source_path(entry, root)
        if source is not None:
            base = load_jsonl(source)
        else:
            from eval.loader import load_suite

            base = [case.to_dict() for case in load_suite(CORE_SUITE)]
        if limit and limit > 0:
            base = base[:limit]
        return base + probe_instances() + self._reviewer_instances()

    @staticmethod
    def _source_path(entry: DatasetEntry, root: Path | str | None) -> Path | None:
        # 只有显式传 fixtures 目录时才用内置小样本；默认读取完整核心集。
        if root is None:
            return None
        candidate = Path(root) / "core_mini.jsonl"
        return candidate if candidate.is_file() else None

    @staticmethod
    def _reviewer_instances() -> list[dict]:
        if not REVIEWER_CASSETTE.is_file():
            return []
        rows = json.loads(REVIEWER_CASSETTE.read_text(encoding="utf-8"))
        return [
            {
                "id": f"core.reviewer.{row.get('name', index)}",
                "mode": "reviewer",
                "case_name": str(row.get("name", index)),
                "response": row.get("response") or {},
                "expected_blocking": list(row.get("expected_blocking") or []),
            }
            for index, row in enumerate(rows)
            if isinstance(row, dict)
        ]

    # ── 任务投影 ──

    def to_tasks(self, entry: DatasetEntry, instances: list[dict]) -> list[TaskSpec]:
        tasks: list[TaskSpec] = []
        for raw in instances:
            task = self._to_task(entry, raw)
            if task is not None:
                tasks.append(task)
        return tasks

    def _to_task(self, entry: DatasetEntry, raw: dict) -> TaskSpec | None:
        if raw.get("layer") == "probe":
            return TaskSpec(
                task_id=str(raw["id"]), dataset=entry.name, capabilities=("F1",),
                identity={"repo": "core", "base_commit": "", "pr_or_issue": str(raw["id"])},
                input={"probe": raw["probe"], "diff": raw.get("diff", "")},
                expected={"paths": list(raw.get("expected_paths") or [])},
                env={}, license=entry.license, quality_grade=entry.quality_grade,
                source_layer="probe", role=entry.role,
            )
        if raw.get("mode") == "reviewer":
            return TaskSpec(
                task_id=str(raw["id"]), dataset=entry.name, capabilities=("F3",),
                identity={"repo": "core", "base_commit": "", "pr_or_issue": str(raw["id"])},
                input={"reviewer": raw},
                expected={"blocking": list(raw.get("expected_blocking") or [])},
                env={}, license=entry.license, quality_grade=entry.quality_grade,
                source_layer="reviewer", role=entry.role,
            )
        case_id = str(raw.get("id") or "")
        if not case_id:
            return None
        layer = str(raw.get("layer") or "")
        caps = _LAYER_CAPS.get(layer)
        if not caps:
            return None
        expect = raw.get("expect") or {}
        return TaskSpec(
            task_id=f"core.{case_id}" if not case_id.startswith("core.") else case_id,
            dataset=entry.name, capabilities=caps,
            identity={"repo": "core", "base_commit": "", "pr_or_issue": case_id},
            input={"case": raw},
            expected={
                "block": bool(expect.get("block")),
                "rules": list(expect.get("rules") or []),
                "tier": str(expect.get("tier") or ""),
            },
            env={}, license=entry.license, quality_grade=entry.quality_grade,
            source_layer=layer, role=entry.role,
        )

    # ── 聚合 ──

    def aggregate(self, entry: DatasetEntry, tasks: list[TaskSpec],
                  observations: list[Observation]) -> list[ScoreRecord]:
        by_id = {task.task_id: task for task in tasks}
        live = [
            obs for obs in observations
            if obs.status == "ok" and obs.task_id in by_id
        ]
        records: list[ScoreRecord] = []

        f1_obs = [obs for obs in live if _has_cap(by_id[obs.task_id], obs, "F1")]
        if f1_obs:
            passed = sum(1 for obs in f1_obs if bool(obs.payload.get("passed")))
            records.append(rate_record(
                task=by_id[f1_obs[0].task_id],
                capability="F1", metric="probe_accuracy",
                successes=passed, total=len(f1_obs), sample_size=len(f1_obs),
            ))

        f2_obs = [obs for obs in live if _has_cap(by_id[obs.task_id], obs, "F2")]
        if f2_obs:
            records.extend(self._f2_records(f2_obs, by_id))

        f3_obs = [obs for obs in live if _has_cap(by_id[obs.task_id], obs, "F3")]
        if f3_obs:
            records.extend(self._f3_records(f3_obs, by_id))

        f5_obs = [
            obs for obs in live
            if _has_cap(by_id[obs.task_id], obs, "F5")
            and str(obs.payload.get("validation_status") or "")
        ]
        if f5_obs:
            passed = sum(
                1 for obs in f5_obs if str(obs.payload.get("validation_status")) == "passed"
            )
            records.append(rate_record(
                task=by_id[f5_obs[0].task_id], capability="F5",
                metric="validation_pass_rate", successes=passed,
                total=len(f5_obs), sample_size=len(f5_obs),
            ))

        f6_obs = [
            obs for obs in live
            if _has_cap(by_id[obs.task_id], obs, "F6") and "passed" in obs.payload
        ]
        if f6_obs:
            passed = sum(1 for obs in f6_obs if bool(obs.payload.get("passed")))
            records.append(rate_record(
                task=by_id[f6_obs[0].task_id], capability="F6",
                metric="task_success_rate", successes=passed,
                total=len(f6_obs), sample_size=len(f6_obs),
            ))
        return records

    def _f2_records(self, observations: list[Observation],
                    by_id: dict[str, TaskSpec]) -> list[ScoreRecord]:
        tp = fp = fn = 0.0
        for obs in observations:
            payload = obs.payload
            layer = str(payload.get("layer") or by_id[obs.task_id].source_layer)
            if layer == "semantic":
                expected = bool(payload.get("expected_block"))
                actual = bool(payload.get("semantic_blocked"))
                if expected and actual:
                    tp += 1
                elif expected:
                    fn += 1
                elif actual:
                    fp += 1
                continue
            expected_rules = set(payload.get("expected_rules") or [])
            actual_rules = set(payload.get("all_rules") or [])
            tp += len(expected_rules & actual_rules)
            fp += len(actual_rules - expected_rules)
            fn += len(expected_rules - actual_rules)
        if tp + fp + fn <= 0:
            return []
        stats = prf(tp, fp, fn)
        anchor = by_id[observations[0].task_id]
        size = len(observations)
        return [
            metric_record(
                task=anchor, capability="F2", metric="f1",
                numerator=2 * tp, denominator=2 * tp + fp + fn,
                value=stats["f1"], sample_size=size,
            ),
            metric_record(
                task=anchor, capability="F2", metric="precision",
                numerator=tp, denominator=tp + fp, value=stats["precision"],
                sample_size=size, interval=wilson(tp, tp + fp),
            ),
            metric_record(
                task=anchor, capability="F2", metric="recall",
                numerator=tp, denominator=tp + fn, value=stats["recall"],
                sample_size=size, interval=wilson(tp, tp + fn),
            ),
        ]

    def _f3_records(self, observations: list[Observation],
                    by_id: dict[str, TaskSpec]) -> list[ScoreRecord]:
        tp = fp = fn = 0.0
        for obs in observations:
            expected = {str(item) for item in (obs.payload.get("expected") or [])}
            actual = {str(item) for item in (obs.payload.get("actual") or [])}
            tp += len(expected & actual)
            fp += len(actual - expected)
            fn += len(expected - actual)
        if tp + fp + fn <= 0:
            return []
        stats = prf(tp, fp, fn)
        anchor = by_id[observations[0].task_id]
        size = len(observations)
        return [
            metric_record(
                task=anchor, capability="F3", metric="f1",
                numerator=2 * tp, denominator=2 * tp + fp + fn,
                value=stats["f1"], sample_size=size,
            ),
            metric_record(
                task=anchor, capability="F3", metric="precision",
                numerator=tp, denominator=tp + fp, value=stats["precision"],
                sample_size=size, interval=wilson(tp, tp + fp),
            ),
            metric_record(
                task=anchor, capability="F3", metric="recall",
                numerator=tp, denominator=tp + fn, value=stats["recall"],
                sample_size=size, interval=wilson(tp, tp + fn),
            ),
        ]


def _has_cap(task: TaskSpec, observation: Observation, capability: str) -> bool:
    caps = observation.capabilities or task.capabilities
    return capability in caps


__all__ = ["CoreAdapter"]
