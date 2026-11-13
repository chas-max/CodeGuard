"""安全数据集适配器（Devign / PrimeVul）：函数级漏洞 → 合成 base/head 变更。

函数级样本没有 PR 上下文，不能直接当作 PR 级审查；适配器只把
`vulnerable` 标签映射成检测任务，核验前 registry 保持 candidate。
"""
from __future__ import annotations

import difflib

from ..metrics import macro_f1, normalize_severity, prf, wilson
from ..models import (
    DatasetEntry,
    Observation,
    ScoreRecord,
    SecurityExpected,
    SecurityInput,
    TaskEnv,
    TaskIdentity,
    TaskSpec,
)
from .base import entry_capabilities, first_present, load_jsonl, metric_record


def _synthetic_diff(code: str, fixed_code: str, path: str = "sample.py") -> str:
    """当漏洞代码与修复代码同时存在时生成标准 unified diff 供去重与对照复用。"""
    if not code.strip() or not fixed_code.strip():
        return ""
    return "".join(difflib.unified_diff(
        code.splitlines(keepends=True),
        fixed_code.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    ))


class SecurityAdapter:
    name = "security"

    def normalize_instance(self, raw: dict) -> dict:
        instance_id = str(first_present(raw, "instance_id", "id", default="")).strip()
        if not instance_id:
            raise ValueError("安全数据集记录缺少 instance_id/id")
        vulnerable = first_present(raw, "vulnerable", "label", "target", default=None)
        if isinstance(vulnerable, str):
            vulnerable = vulnerable.strip().lower() in ("1", "true", "yes", "vulnerable")
        code = str(first_present(raw, "code", "function", "snippet", default=""))
        fixed = str(first_present(raw, "fixed_code", "patched_code", default=""))
        return {
            "instance_id": instance_id,
            "repo": str(first_present(raw, "repo", "project", default="")).strip(),
            "commit": str(first_present(raw, "commit", "base_commit", default="")).strip(),
            "vulnerable": bool(vulnerable) if vulnerable is not None else False,
            "code": code,
            "fixed_code": fixed,
            "cwe": str(first_present(raw, "cwe", "cwe_id", default="")),
            "severity": normalize_severity(str(first_present(raw, "severity", default="high"))),
        }

    def load_instances(self, entry: DatasetEntry, *, root=None, limit: int = 0) -> list[dict]:
        from .swebench import SwebenchAdapter

        path = SwebenchAdapter._instances_path(entry, root)
        if path is None:
            return []
        rows = [self.normalize_instance(item) for item in load_jsonl(path)]
        return rows[:limit] if limit and limit > 0 else rows

    def to_tasks(self, entry: DatasetEntry, instances: list[dict]) -> list[TaskSpec]:
        tasks: list[TaskSpec] = []
        capabilities = entry_capabilities(entry, ("F2", "F3"), supported=("F2", "F3"))
        for raw in instances:
            instance_id = str(raw.get("instance_id") or "")
            if not instance_id:
                continue
            code = str(raw.get("code") or "")
            fixed_code = str(raw.get("fixed_code") or "")
            tasks.append(TaskSpec(
                task_id=f"{entry.name}.{instance_id}",
                dataset=entry.name,
                capabilities=capabilities,
                identity=TaskIdentity(
                    repo=str(raw.get("repo") or ""),
                    base_commit=str(raw.get("commit") or ""),
                    pr_or_issue=f"instance:{instance_id}",
                    instance_id=instance_id,
                ),
                input=SecurityInput(
                    code=code,
                    fixed_code=fixed_code,
                    diff=_synthetic_diff(code, fixed_code),
                    path="sample.py",
                    language="python",
                ),
                expected=SecurityExpected(
                    vulnerable=bool(raw.get("vulnerable")),
                    severity=normalize_severity(str(raw.get("severity") or "high")),  # type: ignore[arg-type]
                    cwe=str(raw.get("cwe") or ""),
                ),
                env=TaskEnv(),
                license=entry.license,
                quality_grade=entry.quality_grade,
                source_layer="security",
                role=entry.role,
            ))
        return tasks

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

        tp = fp = fn = 0
        severity_pairs: list[tuple[str, str]] = []
        for obs in live:
            task_exp = by_id[obs.task_id].expected
            expected = bool(task_exp.vulnerable)
            predicted = obs.payload.get("vulnerable")
            if predicted is None:
                predicted = bool(obs.payload.get("findings"))
            predicted = bool(predicted)
            if expected and predicted:
                tp += 1
            elif expected:
                fn += 1
            elif predicted:
                fp += 1
            if expected and predicted:
                severity_pairs.append((
                    normalize_severity(str(task_exp.severity)),
                    normalize_severity(str(obs.payload.get("severity", ""))),
                ))

        stats = prf(tp, fp, fn)
        records = [
            metric_record(
                task=anchor, capability="F2", metric="f1",
                numerator=2 * tp, denominator=2 * tp + fp + fn,
                value=stats["f1"], sample_size=len(live),
            ),
            metric_record(
                task=anchor, capability="F2", metric="precision",
                numerator=tp, denominator=tp + fp, value=stats["precision"],
                sample_size=len(live), interval=wilson(tp, tp + fp),
            ),
            metric_record(
                task=anchor, capability="F2", metric="recall",
                numerator=tp, denominator=tp + fn, value=stats["recall"],
                sample_size=len(live), interval=wilson(tp, tp + fn),
            ),
        ]
        if severity_pairs:
            macro, _, labels = macro_f1(severity_pairs)
            records.append(metric_record(
                task=anchor, capability="F3", metric="severity_macro_f1",
                numerator=macro * labels, denominator=labels,
                value=macro, sample_size=len(live),
            ))
        return records


__all__ = ["SecurityAdapter"]
