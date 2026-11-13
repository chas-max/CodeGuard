"""样本去重：同一 repo/commit/PR/Issue/diff 只计一次，避免重复加权。

`diff_norm_hash` 刻意忽略 hunk 行号与行尾空白、按路径排序，这样同一个补丁
在不同数据集里的格式差异不会逃过去重；但变更内容本身必须一致。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace

from .models import TaskSpec


def _clean_path(path: str) -> str:
    text = str(path or "").replace("\\", "/").strip()
    for prefix in ("a/", "b/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.lstrip("/")


def normalize_diff(patch_text: str) -> str:
    """把 unified diff 归一化成「路径排序 + 只保留 +/- 行」的稳定文本。"""
    text = str(patch_text or "")
    if not text.strip():
        return ""
    files: dict[str, list[str]] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("diff --git "):
            parts = line.split()
            current = _clean_path(parts[3]) if len(parts) >= 4 else ""
            files.setdefault(current, [])
            continue
        if line.startswith("+++ ") or line.startswith("--- "):
            candidate = line[4:].strip()
            if candidate and candidate != "/dev/null":
                current = _clean_path(candidate)
                files.setdefault(current, [])
            continue
        if line.startswith("@@"):
            continue
        if line.startswith(("+", "-")):
            files.setdefault(current, []).append(line)
    if not files:
        fallback = [line.rstrip() for line in text.splitlines() if line.strip()]
        return "\n".join(fallback)
    chunks: list[str] = []
    for path in sorted(files):
        chunks.append(f"{path}\n" + "\n".join(files[path]))
    return "\n".join(chunks)


def diff_norm_hash(patch_text: str) -> str:
    return hashlib.sha256(normalize_diff(patch_text).encode("utf-8")).hexdigest()[:16]


def _normalize_repo(repo: str) -> str:
    text = str(repo or "").strip().lower().replace("\\", "/")
    if text.startswith("https://github.com/"):
        text = text[len("https://github.com/"):]
    if text.endswith(".git"):
        text = text[:-4]
    return text.strip("/")


def _subject(identity: dict) -> str:
    for key in ("pr_or_issue", "instance_id", "pr", "issue"):
        value = identity.get(key)
        if value not in (None, ""):
            if key == "pr":
                return f"pr:{value}"
            if key == "issue":
                return f"issue:{value}"
            return str(value).strip().lower()
    return ""


def sample_uid(identity: dict, patch_text: str = "") -> str:
    material = "|".join((
        _normalize_repo(str(identity.get("repo") or "")),
        str(identity.get("base_commit") or identity.get("commit") or "").strip().lower(),
        _subject(identity),
        diff_norm_hash(patch_text),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def task_patch(task: TaskSpec) -> str:
    payload = task.input or {}
    candidate = payload.get("patch") or payload.get("diff")
    if candidate:
        return str(candidate)
    instance = payload.get("instance")
    if isinstance(instance, dict):
        return str(instance.get("patch") or instance.get("diff") or "")
    return ""


def attach_uids(tasks: list[TaskSpec]) -> list[TaskSpec]:
    filled: list[TaskSpec] = []
    for task in tasks:
        uid = task.sample_uid or sample_uid(task.identity, task_patch(task))
        filled.append(task if task.sample_uid == uid else replace(task, sample_uid=uid))
    return filled


@dataclass
class DedupResult:
    kept: list[TaskSpec] = field(default_factory=list)
    duplicates: list[tuple[TaskSpec, str]] = field(default_factory=list)
    index: dict[str, list[str]] = field(default_factory=dict)
    overlap: dict[str, int] = field(default_factory=dict)
    duplicate_count: int | None = None

    def to_dict(self) -> dict:
        duplicates = [
            {"task_id": task.task_id, "dataset": task.dataset, "owner": owner}
            for task, owner in self.duplicates
        ]
        return {
            "kept": len(self.kept),
            "duplicate_count": (
                self.duplicate_count if self.duplicate_count is not None else len(self.duplicates)
            ),
            "duplicates": duplicates,
            "index": {uid: list(ids) for uid, ids in sorted(self.index.items())},
            "overlap": dict(sorted(self.overlap.items())),
        }


def dedup_tasks(tasks: list[TaskSpec]) -> DedupResult:
    result = DedupResult()
    owners: dict[str, TaskSpec] = {}
    for task in attach_uids(tasks):
        uid = task.sample_uid
        result.index.setdefault(uid, []).append(task.task_id)
        if uid in owners:
            result.duplicates.append((task, owners[uid].task_id))
            continue
        owners[uid] = task
        result.kept.append(task)

    datasets_by_uid: dict[str, set[str]] = {}
    for uid, task_ids in result.index.items():
        for task in result.kept:
            if task.sample_uid == uid:
                datasets_by_uid.setdefault(uid, set()).add(task.dataset)
        for task, _ in result.duplicates:
            if task.sample_uid == uid:
                datasets_by_uid.setdefault(uid, set()).add(task.dataset)
    for datasets in datasets_by_uid.values():
        ordered = sorted(datasets)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1:]:
                key = f"{left}|{right}"
                result.overlap[key] = result.overlap.get(key, 0) + 1
    return result


__all__ = [
    "normalize_diff", "diff_norm_hash", "sample_uid", "task_patch",
    "attach_uids", "DedupResult", "dedup_tasks",
]
