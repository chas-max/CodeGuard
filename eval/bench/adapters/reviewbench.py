"""审查类适配器：PR → gold findings，衡量定位准确率、finding P/R/F1 与严重度。

同时兼容 SWE-CARE / CodeFuse-CR-Bench 的 `reference_comments` 字段形状：
gold 既可以显式写在 `gold_findings` / `findings`，也可以是人工评审留下的
`reference_comments` / `review_comments` / `comments`（按 path + 行号匹配）。

数据集仓库与许可证尚未核验时 registry 保持 `candidate`；核验通过后只需把
registry 改成 `approved`，评分链路不变。
"""
from __future__ import annotations

from ..metrics import macro_f1, match_findings, normalize_severity, prf, ratio, wilson
from ..models import DatasetEntry, Observation, ScoreRecord, TaskSpec
from .base import as_list, first_present, metric_record

_GOLD_KEYS = (
    "gold_findings", "findings", "expected_findings",
    "reference_comments", "review_comments", "comments", "reviewComments",
)


def _normalize_finding(item) -> dict | None:
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return None
        path, _, line = text.rpartition(":")
        if not path:
            path, line = text, "0"
        try:
            number = int(line)
        except ValueError:
            number = 0
        return {"path": path, "line": number, "severity": "medium", "category": ""}
    if not isinstance(item, dict):
        return None
    path = str(first_present(item, "path", "file", "filename", default="")).strip()
    if not path:
        return None
    try:
        line = int(first_present(
            item, "line", "start_line", "new_line", "original_line", "end_line", default=0
        ) or 0)
    except (TypeError, ValueError):
        line = 0
    return {
        "path": path,
        "line": line,
        "severity": normalize_severity(
            str(first_present(item, "severity", "priority", "level", default="medium"))
        ),
        "category": str(first_present(
            item, "category", "type", "kind", "label", default=""
        )),
    }


def _gold_findings(raw: dict) -> list[dict]:
    findings: list[dict] = []
    for key in _GOLD_KEYS:
        for item in as_list(raw.get(key)):
            normalized = _normalize_finding(item)
            if normalized:
                findings.append(normalized)
        if findings:
            return findings
    return findings


class ReviewBenchAdapter:
    name = "reviewbench"

    def normalize_instance(self, raw: dict) -> dict:
        pr_number = first_present(raw, "pr_number", "pull_number", "pr", "number", default=0)
        repo = str(first_present(raw, "repo", "org_repo", default="")).strip()
        instance_id = str(first_present(raw, "instance_id", "id", default="")).strip()
        if not instance_id and repo and pr_number:
            instance_id = f"{repo}#{pr_number}"
        if not instance_id:
            raise ValueError("ReviewBench/SWE-CARE 记录缺少 instance_id/id 或 repo+pr_number")
        return {
            "instance_id": instance_id,
            "repo": repo,
            "pr_number": pr_number,
            "base_commit": str(first_present(
                raw, "base_commit", "base_sha", default=""
            )).strip(),
            "head_commit": str(first_present(
                raw, "head_commit", "commit_sha", default=""
            )).strip(),
            "diff": str(first_present(raw, "diff", "patch", "pr_diff", default="")),
            "title": str(first_present(raw, "title", default="")),
            "problem_statement": str(
                first_present(raw, "problem_statement", "body", "description", "issue",
                              default="")
            )[:4000],
            "gold_findings": _gold_findings(raw),
        }

    def load_instances(self, entry: DatasetEntry, *, root=None, limit: int = 0) -> list[dict]:
        from .base import load_jsonl
        from .swebench import SwebenchAdapter

        path = SwebenchAdapter._instances_path(entry, root)
        if path is None:
            return []
        rows = [self.normalize_instance(item) for item in load_jsonl(path)]
        return rows[:limit] if limit and limit > 0 else rows

    def to_tasks(self, entry: DatasetEntry, instances: list[dict]) -> list[TaskSpec]:
        tasks: list[TaskSpec] = []
        for raw in instances:
            instance_id = str(raw.get("instance_id") or "")
            if not instance_id:
                continue
            pr_number = raw.get("pr_number") or 0
            tasks.append(TaskSpec(
                task_id=f"{entry.name}.{instance_id}",
                dataset=entry.name,
                capabilities=("F2", "F3"),
                identity={
                    "repo": str(raw.get("repo") or ""),
                    "base_commit": str(raw.get("base_commit") or ""),
                    "pr_or_issue": f"pr:{pr_number}" if pr_number else f"instance:{instance_id}",
                    "instance_id": instance_id,
                },
                input={
                    "instance": raw,
                    "diff": str(raw.get("diff") or ""),
                    "title": str(raw.get("title") or ""),
                },
                expected={"gold_findings": list(raw.get("gold_findings") or [])},
                env={"window": 3},
                license=entry.license,
                quality_grade=entry.quality_grade,
                source_layer="review",
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
        records: list[ScoreRecord] = []

        tp = fp = fn = matched_gold = gold_total = 0
        severity_pairs: list[tuple[str, str]] = []
        for obs in live:
            gold = list(by_id[obs.task_id].expected.get("gold_findings") or [])
            predicted = list(obs.payload.get("findings") or [])
            result = match_findings(predicted, gold, window=3)
            tp += result.tp
            fp += result.fp
            fn += result.fn
            matched_gold += result.matched_gold
            gold_total += len(gold)
            for gold_item, pred_item in result.pairs:
                severity_pairs.append((
                    normalize_severity(str(gold_item.get("severity", ""))),
                    normalize_severity(str(pred_item.get("severity", ""))),
                ))

        records.append(metric_record(
            task=anchor, capability="F2", metric="localization_accuracy",
            numerator=matched_gold, denominator=gold_total,
            value=ratio(matched_gold, gold_total), sample_size=len(live),
            interval=wilson(matched_gold, gold_total) if gold_total else None,
        ))

        stats = prf(tp, fp, fn)
        records.append(metric_record(
            task=anchor, capability="F3", metric="f1",
            numerator=2 * tp, denominator=2 * tp + fp + fn,
            value=stats["f1"], sample_size=len(live),
        ))
        records.append(metric_record(
            task=anchor, capability="F3", metric="precision",
            numerator=tp, denominator=tp + fp, value=stats["precision"],
            sample_size=len(live), interval=wilson(tp, tp + fp),
        ))
        records.append(metric_record(
            task=anchor, capability="F3", metric="recall",
            numerator=tp, denominator=tp + fn, value=stats["recall"],
            sample_size=len(live), interval=wilson(tp, tp + fn),
        ))

        macro, per_label, labels = macro_f1(severity_pairs)
        records.append(metric_record(
            task=anchor, capability="F3", metric="severity_macro_f1",
            numerator=macro * labels, denominator=labels,
            value=macro, sample_size=len(live),
        ))
        return records


__all__ = ["ReviewBenchAdapter"]
