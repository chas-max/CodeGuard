"""BugsInPy 适配器：Python 真实缺陷，字段与 SWE-bench 不同但评测形状一致。

BugsInPy 的 `project/bug_id`、`buggy_commit`、`fixed_commit`、`test_file` 统一
映射成 SWE-bench 风格，F4/F5 聚合直接复用 `SwebenchAdapter`，避免两套指标口径；
F2 用「候选补丁实际变更路径 ∩ gold 补丁路径」衡量缺陷定位。
"""
from __future__ import annotations

from ..metrics import ratio, wilson
from ..models import DatasetEntry, Observation, ScoreRecord, TaskSpec
from .base import as_list, first_present, metric_record
from .swebench import SwebenchAdapter


def _patch_paths(patch_text: str) -> list[str]:
    from codeguard.engine.diff import patch_paths

    try:
        return sorted({str(item) for item in patch_paths(patch_text)})
    except Exception:  # noqa: BLE001 - 非法补丁不应拖垮整批聚合
        return []


class BugsInPyAdapter(SwebenchAdapter):
    name = "bugsinpy"

    def normalize_instance(self, raw: dict) -> dict:
        project = str(first_present(raw, "project", "repo", default="")).strip()
        bug_id = first_present(raw, "bug_id", "id", "instance_id", default="")
        instance_id = str(
            first_present(raw, "instance_id", default="") or f"{project}.{bug_id}"
        ).strip()
        if not instance_id or instance_id == ".":
            raise ValueError("BugsInPy 记录缺少 bug_id/project")
        tests = as_list(first_present(raw, "fail_to_pass", "tests", "test_file", default=[]))
        base_commit = str(
            first_present(raw, "buggy_commit", "base_commit", "bug_commit", default="")
        ).strip()
        return {
            "instance_id": instance_id,
            "repo": project,
            "base_commit": base_commit,
            "patch": str(first_present(raw, "patch", "fix_patch", "model_patch", default="")),
            "test_patch": str(first_present(raw, "test_patch", default="")),
            "problem_statement": str(
                first_present(raw, "problem_statement", "report", "description", default="")
            )[:4000],
            "fail_to_pass": [str(item) for item in tests],
            "pass_to_pass": [str(item) for item in as_list(
                first_present(raw, "pass_to_pass", default=[])
            )],
            "version": str(first_present(raw, "version", "python_version", default="")),
            "environment_setup_commit": "",
            "test_command": str(first_present(
                raw, "test_command", default="python -m pytest -q"
            )),
            "image": str(first_present(raw, "image", default="")),
            "prepare_commands": [
                str(item) for item in as_list(
                    first_present(raw, "prepare_commands", "install_commands", default=[])
                )
            ],
        }

    def aggregate(self, entry: DatasetEntry, tasks: list[TaskSpec],
                  observations: list[Observation]) -> list[ScoreRecord]:
        records = super().aggregate(entry, tasks, observations)
        by_id = {task.task_id: task for task in tasks}
        live = [
            obs for obs in observations
            if obs.status == "ok" and obs.task_id in by_id
        ]
        if not live:
            return records
        anchor = by_id[live[0].task_id]

        matched = total = 0
        for obs in live:
            task = by_id[obs.task_id]
            gold = _patch_paths(str(task.input.get("patch") or ""))
            if not gold:
                continue
            probe = obs.payload.get("f1_probe")
            actual = sorted({
                str(item) for item in ((probe or {}).get("actual") or [])
            })
            matched += len(set(gold) & set(actual))
            total += len(set(gold))
        if total:
            records.append(metric_record(
                task=anchor, capability="F2", metric="localization_accuracy",
                numerator=matched, denominator=total, value=ratio(matched, total),
                sample_size=len(live), interval=wilson(matched, total),
            ))
        return records


__all__ = ["BugsInPyAdapter"]
