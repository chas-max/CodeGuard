"""只读检索工具：read_file / list_dir / grep / related_files。

这些工具只读取 job 工作区副本，不执行任何代码；路径必须落在 root 内。
"""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..engine.diff import IGNORE_DIRS
from ..engine.repo_map import build_repo_map, related_files as _related_files
from .base import error_result, ok_result, read_text_limited, safe_resolve, truncate
from .spec import ToolResult

MAX_READ_LINES = 400
MAX_DIR_ENTRIES = 200
GREP_MAX_SCAN_BYTES = 2_000_000


class ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=0, ge=0, description="0 表示 start_line 起最多 400 行")


class ListDirArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = "."
    depth: int = Field(default=1, ge=1, le=2)


class GrepArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pattern: str = Field(min_length=1, max_length=500)
    glob: str = Field(default="", max_length=200)
    max_matches: int = Field(default=50, ge=1, le=200)
    case_sensitive: bool = True


class RelatedFilesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=50)


def _file_entry(rel: str, path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        size = -1
    return f"{rel} ({size} bytes)"


def read_file(root: Path, args: ReadFileArgs, *, max_chars: int = 8000) -> ToolResult:
    try:
        rel, path = safe_resolve(root, args.path)
    except ValueError as exc:
        return error_result("denied", str(exc))
    if rel == ".":
        return error_result("invalid_args", "read_file 需要文件路径，不能是仓库根")
    if not path.exists():
        return error_result("failed", f"文件不存在: {rel}")
    if not path.is_file():
        return error_result("invalid_args", f"不是文件: {rel}")
    text, error = read_text_limited(path)
    if error:
        return error_result("failed", f"{rel}: {error}")
    lines = text.splitlines()
    total = len(lines)
    start = min(args.start_line, total + 1)
    end = args.end_line if args.end_line >= start else start + MAX_READ_LINES - 1
    end = min(end, total, start + MAX_READ_LINES - 1)
    body = "\n".join(lines[start - 1:end]) if start <= total else ""
    header = f"=== {rel} (lines {start}-{end} of {total}) ==="
    content, truncated = truncate(f"{header}\n{body}", max_chars)
    return ok_result(
        content,
        meta={"path": rel, "start_line": start, "end_line": end, "total_lines": total,
              "truncated": truncated or end < total},
    )


def list_dir(root: Path, args: ListDirArgs, *, max_chars: int = 8000) -> ToolResult:
    try:
        rel, path = safe_resolve(root, args.path)
    except ValueError as exc:
        return error_result("denied", str(exc))
    if not path.is_dir():
        return error_result("invalid_args", f"不是目录: {rel}")
    entries: list[str] = []

    def _walk(current: Path, prefix: str, depth: int) -> None:
        if depth > args.depth or len(entries) >= MAX_DIR_ENTRIES:
            return
        try:
            children = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except OSError as exc:
            entries.append(f"{prefix}<无法读取: {exc}>")
            return
        for child in children:
            if child.name in IGNORE_DIRS or len(entries) >= MAX_DIR_ENTRIES:
                continue
            rel_child = f"{prefix}{child.name}"
            if child.is_dir():
                entries.append(f"{rel_child}/")
                _walk(child, rel_child + "/", depth + 1)
            else:
                entries.append(_file_entry(rel_child, child))

    _walk(path, "", 1)
    content, truncated = truncate("\n".join(entries) or "（空目录）", max_chars)
    return ok_result(content, meta={"path": rel, "entries": len(entries), "truncated": truncated})


def grep(
    root: Path,
    args: GrepArgs,
    *,
    repo_map: dict | None = None,
    max_chars: int = 8000,
) -> ToolResult:
    flags = 0 if args.case_sensitive else re.IGNORECASE
    try:
        pattern = re.compile(args.pattern, flags)
    except re.error as exc:
        return error_result("invalid_args", f"正则非法: {exc}")

    mapping = repo_map or build_repo_map(root)
    candidates = [
        item["path"] for item in mapping.get("files", [])
        if not item.get("is_binary") and (not args.glob or fnmatch.fnmatch(item["path"], args.glob))
    ]
    matches: list[str] = []
    scanned = 0
    exhausted = False
    for rel in candidates:
        if len(matches) >= args.max_matches or scanned >= GREP_MAX_SCAN_BYTES:
            exhausted = True
            break
        try:
            _, path = safe_resolve(root, rel)
        except ValueError:
            continue
        text, error = read_text_limited(path, max_bytes=GREP_MAX_SCAN_BYTES)
        if error:
            continue
        scanned += len(text.encode("utf-8", errors="replace"))
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                matches.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                if len(matches) >= args.max_matches:
                    exhausted = True
                    break
    content = "\n".join(matches) or "（无匹配）"
    content, truncated = truncate(content, max_chars)
    return ok_result(
        content,
        meta={"matches": len(matches), "scanned_bytes": scanned,
              "truncated": truncated or exhausted},
    )


def related_files(
    root: Path,
    args: RelatedFilesArgs,
    *,
    repo_map: dict | None = None,
    max_chars: int = 8000,
) -> ToolResult:
    if not args.paths:
        return error_result("invalid_args", "paths 不能为空")
    normalized: list[str] = []
    for raw in args.paths:
        try:
            rel, _ = safe_resolve(root, raw)
        except ValueError as exc:
            return error_result("denied", str(exc))
        if rel != "." and rel not in normalized:
            normalized.append(rel)
    mapping = repo_map or build_repo_map(root)
    selected = _related_files(root, mapping, normalized, limit=args.limit)
    content, truncated = truncate(json.dumps(selected, ensure_ascii=False), max_chars)
    return ok_result(content, meta={"files": len(selected), "truncated": truncated})


__all__ = [
    "ReadFileArgs", "ListDirArgs", "GrepArgs", "RelatedFilesArgs",
    "read_file", "list_dir", "grep", "related_files",
]
