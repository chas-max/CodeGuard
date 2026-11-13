"""写工具：apply_patch / write_file。仅 fixer 在 job 私有工作区副本中可用。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..engine.diff import apply_patch as _apply_patch
from ..engine.diff import tree_hash
from ..engine.patch import validate_fix_patch
from ..engine.repo_map import is_test_path
from .base import error_result, normalize_rel, ok_result, safe_resolve
from .spec import ToolResult

MAX_FILE_BYTES = 200_000
MAX_TOTAL_WRITE_BYTES = 500_000
FORBIDDEN_PREFIXES = (".github/workflows/", ".codeguard/")


class ApplyPatchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patch: str = Field(min_length=1, max_length=200_000)


class WriteFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    content: str = Field(default="", max_length=MAX_FILE_BYTES)


@dataclass
class EditEnv:
    root: Path
    allowed_files: list[str] = field(default_factory=list)
    written_bytes: int = 0

    def __post_init__(self) -> None:
        normalized: set[str] = set()
        for item in self.allowed_files:
            try:
                normalized.add(normalize_rel(item))
            except ValueError:
                continue
        self._allowed = normalized

    def allowed(self, rel: str) -> bool:
        return rel in self._allowed


def _check_target(env: EditEnv, rel: str) -> str:
    if not env.allowed(rel):
        return f"文件不在允许修改列表内: {rel}"
    if is_test_path(rel):
        return f"禁止修改测试文件: {rel}"
    if any(rel.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
        return f"禁止修改受保护路径: {rel}"
    return ""


def apply_patch(env: EditEnv, args: ApplyPatchArgs) -> ToolResult:
    proposal, error = validate_fix_patch(
        env.root, args.patch, allowed_files=env.allowed_files,
        forbidden_prefixes=FORBIDDEN_PREFIXES,
    )
    if error:
        return error_result("failed", error)
    ok, output = _apply_patch(env.root, args.patch)
    if not ok:
        return error_result("failed", f"补丁应用失败: {output}")
    env.written_bytes += len(args.patch.encode("utf-8"))
    digest = tree_hash(env.root)
    return ok_result(
        f"补丁已应用，修改文件: {', '.join(proposal.files)}；tree_hash={digest[:12]}",
        meta={"files": proposal.files, "tree_hash": digest},
    )


def write_file(env: EditEnv, args: WriteFileArgs) -> ToolResult:
    try:
        rel, path = safe_resolve(env.root, args.path)
    except ValueError as exc:
        return error_result("denied", str(exc))
    if rel == ".":
        return error_result("invalid_args", "path 必须是文件路径")
    problem = _check_target(env, rel)
    if problem:
        return error_result("denied", problem)
    if path.exists() and path.is_dir():
        return error_result("invalid_args", f"目标是目录: {rel}")
    payload = args.content.encode("utf-8")
    if len(payload) > MAX_FILE_BYTES:
        return error_result("denied", f"单文件超过 {MAX_FILE_BYTES} bytes")
    if env.written_bytes + len(payload) > MAX_TOTAL_WRITE_BYTES:
        return error_result("denied", f"单轮累计写入超过 {MAX_TOTAL_WRITE_BYTES} bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(args.content, encoding="utf-8")
    except OSError as exc:
        return error_result("failed", f"写入失败: {exc}")
    env.written_bytes += len(payload)
    digest = tree_hash(env.root)
    return ok_result(
        f"已写入 {rel}（{len(payload)} bytes）；tree_hash={digest[:12]}",
        meta={"path": rel, "bytes": len(payload), "tree_hash": digest},
    )


__all__ = [
    "ApplyPatchArgs", "WriteFileArgs", "EditEnv",
    "MAX_FILE_BYTES", "MAX_TOTAL_WRITE_BYTES",
    "apply_patch", "write_file",
]
