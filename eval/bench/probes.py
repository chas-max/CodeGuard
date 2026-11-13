"""F1 探针：不依赖 LLM，验证 Diff/路径/统计这些“输入理解”能力。

探针只在本地字符串上运行，不读仓库、不触网，因此适合作为 PR 理解维度的
确定性基线；它不衡量模型质量，只保证后续检测拿到的是正确的变更集。
"""
from __future__ import annotations

from .models import TaskSpec

PROBE_INSTANCES: tuple[dict, ...] = (
    {
        "layer": "probe",
        "id": "probe.diff.single_path",
        "probe": "diff_paths",
        "diff": (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n"
            "+++ b/src/a.py\n"
            "@@ -1 +1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
        ),
        "expected_paths": ["src/a.py"],
    },
    {
        "layer": "probe",
        "id": "probe.diff.multi_path",
        "probe": "diff_paths",
        "diff": (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n"
            "+++ b/src/a.py\n"
            "@@ -1 +1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
            "diff --git a/docs/guide.md b/docs/guide.md\n"
            "--- a/docs/guide.md\n"
            "+++ b/docs/guide.md\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
        "expected_paths": ["src/a.py", "docs/guide.md"],
    },
    {
        "layer": "probe",
        "id": "probe.diff.empty",
        "probe": "diff_paths",
        "diff": "",
        "expected_paths": [],
    },
    {
        "layer": "probe",
        "id": "probe.diff.deleted_file",
        "probe": "diff_paths",
        "diff": (
            "diff --git a/src/old.py b/src/old.py\n"
            "--- a/src/old.py\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-def old():\n"
            "-    return 1\n"
        ),
        "expected_paths": ["src/old.py"],
    },
    {
        "layer": "probe",
        "id": "probe.diff.multi_hunk",
        "probe": "diff_paths",
        "diff": (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n"
            "+++ b/src/a.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-x = 1\n"
            "+x = 2\n"
            "@@ -10,2 +10,2 @@\n"
            "-y = 1\n"
            "+y = 2\n"
        ),
        "expected_paths": ["src/a.py"],
    },
)


def probe_instances() -> list[dict]:
    return [dict(item) for item in PROBE_INSTANCES]


def probe_tasks(dataset: str = "core") -> list[TaskSpec]:
    tasks = []
    for item in PROBE_INSTANCES:
        tasks.append(TaskSpec(
            task_id=item["id"], dataset=dataset, capabilities=("F1",),
            identity={"repo": "core", "base_commit": "", "pr_or_issue": item["id"]},
            input={"probe": item["probe"], "diff": item["diff"]},
            expected={"paths": list(item["expected_paths"])},
            env={}, license="internal", quality_grade="A", source_layer="probe",
        ))
    return tasks


__all__ = ["PROBE_INSTANCES", "probe_instances", "probe_tasks"]
