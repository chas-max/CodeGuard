"""样本去重：diff 归一化、uid 稳定性、重复归属与重叠矩阵。"""
from __future__ import annotations

from eval.bench.dedup import (
    attach_uids,
    dedup_tasks,
    diff_norm_hash,
    normalize_diff,
    sample_uid,
    task_patch,
)
from eval.bench.models import TaskIdentity, TaskSpec


def _task(task_id: str, dataset: str, *, patch: str = "", commit: str = "a" * 40,
          repo: str = "demo/repo", source_layer: str = "fix") -> TaskSpec:
    return TaskSpec(
        task_id=task_id, dataset=dataset, capabilities=("F2",),
        identity={"repo": repo, "base_commit": commit, "pr_or_issue": "pr:1"},
        input={"patch": patch}, expected={}, env={},
        license="MIT", quality_grade="A", source_layer=source_layer,
    )


SINGLE = (
    "diff --git a/src/a.py b/src/a.py\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-x = 1 \n"
    "+x = 2\n"
)
SINGLE_SHIFTED = (
    "diff --git a/src/a.py b/src/a.py\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -10,2 +20,2 @@\n"
    "-x = 1\n"
    "+x = 2\n"
)
TWO_FILES = (
    "diff --git a/src/a.py b/src/a.py\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-x = 1\n"
    "+x = 2\n"
    "diff --git a/src/b.py b/src/b.py\n"
    "--- a/src/b.py\n"
    "+++ b/src/b.py\n"
    "@@ -1 +1 @@\n"
    "-y = 1\n"
    "+y = 2\n"
)
TWO_FILES_REORDERED = (
    "diff --git a/src/b.py b/src/b.py\n"
    "--- a/src/b.py\n"
    "+++ b/src/b.py\n"
    "@@ -1 +1 @@\n"
    "-y = 1\n"
    "+y = 2\n"
    "diff --git a/src/a.py b/src/a.py\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-x = 1\n"
    "+x = 2\n"
)


def test_normalize_diff_ignores_hunk_offsets_and_trailing_space():
    assert normalize_diff(SINGLE) == normalize_diff(SINGLE_SHIFTED)
    assert normalize_diff(SINGLE) != ""


def test_normalize_diff_sorts_files():
    assert normalize_diff(TWO_FILES) == normalize_diff(TWO_FILES_REORDERED)
    assert normalize_diff(TWO_FILES) != normalize_diff(SINGLE)


def test_sample_uid_stable_and_identity_sensitive():
    first = sample_uid({"repo": "Demo/Repo.git", "base_commit": "A" * 40,
                        "pr_or_issue": "pr:1"}, SINGLE)
    second = sample_uid({"repo": "demo/repo", "base_commit": "a" * 40,
                         "pr_or_issue": "pr:1"}, SINGLE_SHIFTED)
    assert first == second
    other_commit = sample_uid({"repo": "demo/repo", "base_commit": "b" * 40,
                               "pr_or_issue": "pr:1"}, SINGLE)
    assert first != other_commit


def test_attach_uids_fills_frozen_tasks():
    tasks = [ _task("a", "ds_a", patch=SINGLE) ]
    filled = attach_uids(tasks)
    assert filled[0].sample_uid
    assert filled[0].task_id == tasks[0].task_id
    assert tasks[0].sample_uid == ""


def test_dedup_keeps_first_and_reports_overlap():
    first = _task("a", "ds_a", patch=SINGLE)
    duplicate = _task("b", "ds_b", patch=SINGLE_SHIFTED)
    different = _task("c", "ds_b", patch=SINGLE, commit="b" * 40)
    result = dedup_tasks([first, duplicate, different])
    assert [task.task_id for task in result.kept] == ["a", "c"]
    assert len(result.duplicates) == 1
    assert result.duplicates[0][0].task_id == "b"
    assert result.duplicates[0][1] == "a"
    assert result.overlap["ds_a|ds_b"] == 1
    assert result.to_dict()["duplicates"][0]["task_id"] == "b"


def test_diff_norm_hash_is_short_and_stable():
    digest = diff_norm_hash(TWO_FILES)
    assert digest == diff_norm_hash(TWO_FILES_REORDERED)
    assert len(digest) == 16


def test_duplicate_count_can_be_restored_from_report():
    from eval.bench.dedup import DedupResult

    restored = DedupResult(kept=[], duplicate_count=2).to_dict()
    assert restored["duplicate_count"] == 2
    assert restored["duplicates"] == []


def test_sample_uid_accepts_task_identity_and_dict():
    uid_dict = sample_uid(
        {"repo": "Org/Repo", "base_commit": "ABC", "pr_or_issue": "pr:1"},
        "+x = 1\n",
    )
    uid_model = sample_uid(
        TaskIdentity(repo="Org/Repo", base_commit="ABC", pr_or_issue="pr:1"),
        "+x = 1\n",
    )
    assert uid_dict == uid_model


def test_dedup_works_with_review_and_security_synthetic_diff():
    s1 = TaskSpec(
        task_id="sec.1", dataset="devign", capabilities=("F2",),
        identity={"repo": "r", "base_commit": "a" * 40, "instance_id": "1"},
        input={"code": "old()", "fixed_code": "new()", "diff": "-old()\n+new()\n"},
        expected={"vulnerable": True}, env={}, license="MIT", quality_grade="B",
        source_layer="security",
    )
    s2 = TaskSpec(
        task_id="sec.2", dataset="primevul", capabilities=("F2",),
        identity={"repo": "r", "base_commit": "a" * 40, "instance_id": "1"},
        input={"code": "old()", "fixed_code": "new()", "diff": "-old()\n+new()\n"},
        expected={"vulnerable": True}, env={}, license="MIT", quality_grade="B",
        source_layer="security",
    )
    r1 = TaskSpec(
        task_id="rev.1", dataset="reviewbench", capabilities=("F2", "F3"),
        identity={"repo": "r", "base_commit": "b" * 40, "pr_or_issue": "pr:2"},
        input={"diff": "--- a/x.py\n+++ b/x.py\n+y = 1\n"},
        expected={"gold_findings": []}, env={}, license="MIT", quality_grade="B",
        source_layer="review",
    )
    assert task_patch(s1) == "-old()\n+new()\n"
    assert task_patch(r1).startswith("--- a/x.py")
    result = dedup_tasks([s1, s2, r1])
    assert len(result.kept) == 2
    assert len(result.duplicates) == 1
