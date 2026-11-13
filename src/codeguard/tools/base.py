"""工具层公共能力：路径安全、输出截断、参数摘要与结果构造。"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from ..engine.diff import IGNORE_DIRS
from ..models import validate_repo_path
from .spec import ToolResult

MAX_TEXT_FILE_BYTES = 1_000_000


def normalize_rel(path: str) -> str:
    """规范化仓库相对路径；'.' 表示仓库根。拒绝绝对路径、`..` 与忽略目录。"""
    raw = (path or "").strip().replace("\\", "/")
    if raw in ("", ".", "./"):
        return "."
    rel = validate_repo_path(raw)
    parts = PurePosixPath(rel).parts
    if any(part in IGNORE_DIRS for part in parts):
        raise ValueError(f"路径位于忽略目录: {path!r}")
    return rel


def safe_resolve(root: Path, path: str) -> tuple[str, Path]:
    """把仓库相对路径解析到 root 内；拒绝越界与符号链接逃逸。"""
    rel = normalize_rel(path)
    root_resolved = root.resolve()
    target = root_resolved if rel == "." else root_resolved / rel
    resolved = target.resolve()
    if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
        raise ValueError(f"路径越界: {path!r}")
    return rel, resolved


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def summarize_args(args: dict[str, Any] | None, *, max_chars: int = 400) -> str:
    """审计用参数摘要：不落完整文件内容。"""
    safe: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if isinstance(value, str) and len(value) > 120:
            safe[key] = f"<{len(value)} chars>"
        elif isinstance(value, list) and len(value) > 10:
            safe[key] = f"<{len(value)} items>"
        else:
            safe[key] = value
    text = json.dumps(safe, ensure_ascii=False, default=str)
    return text[:max_chars]


def ok_result(
    content: str,
    *,
    status: str = "ok",
    meta: dict[str, Any] | None = None,
    error: str = "",
) -> ToolResult:
    return ToolResult(
        status=status,  # type: ignore[arg-type]
        content=content,
        error=error,
        output_chars=len(content),
        meta=meta or {},
    )


def error_result(status: str, message: str, *, meta: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(
        status=status,  # type: ignore[arg-type]
        error=message,
        meta=meta or {},
    )


def read_text_limited(path: Path, *, max_bytes: int = MAX_TEXT_FILE_BYTES) -> tuple[str, str]:
    """读取文本文件；返回 (text, error)。二进制或超大文件返回 error。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return "", f"无法读取文件: {exc}"
    if size > max_bytes:
        return "", f"文件过大（{size} bytes > {max_bytes}）"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return "", f"无法读取文件: {exc}"
    if b"\x00" in raw[:8192]:
        return "", "二进制文件不可读取"
    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), ""


__all__ = [
    "MAX_TEXT_FILE_BYTES", "normalize_rel", "safe_resolve", "truncate",
    "summarize_args", "ok_result", "error_result", "read_text_limited",
]
