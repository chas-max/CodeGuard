"""写工具：apply_patch / write_file / edit_file。仅 Coder/Fixer 在隔离副本中可用。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..engine.diff import apply_patch as _apply_patch
from ..engine.diff import tree_hash, unified_diff
from ..engine.patch import preserves_existing_tests, validate_fix_patch
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


class EditFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=500)
    old_text: str = Field(min_length=1, max_length=MAX_FILE_BYTES)
    new_text: str = Field(default="", max_length=MAX_FILE_BYTES)


@dataclass
class EditEnv:
    root: Path
    allowed_files: list[str] = field(default_factory=list)
    allowed_test_addition_files: list[str] = field(default_factory=list)
    written_bytes: int = 0

    def __post_init__(self) -> None:
        self._allowed = _normalize_set(self.allowed_files)
        self._allowed_tests = _normalize_set(self.allowed_test_addition_files)

    def allowed(self, rel: str) -> bool:
        return rel in self._allowed

    def test_addition_allowed(self, rel: str) -> bool:
        return rel in self._allowed_tests

    def all_allowed_paths(self) -> list[str]:
        combined = set(self._allowed) | set(self._allowed_tests)
        return sorted(combined)


def _normalize_set(items: list[str]) -> set[str]:
    normalized: set[str] = set()
    for item in items:
        try:
            rel = normalize_rel(item)
            if rel and rel != ".":
                normalized.add(rel)
        except ValueError:
            continue
    return normalized


def _check_target(env: EditEnv, rel: str) -> str:
    if any(rel.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
        return f"禁止修改受保护路径: {rel}"
    if is_test_path(rel):
        if env.test_addition_allowed(rel):
            return ""
        return f"禁止修改测试文件: {rel}"
    if not env.allowed(rel):
        return f"文件不在允许修改列表内: {rel}"
    return ""


def _check_test_preservation(rel: str, before_code: str, after_code: str) -> str:
    if not is_test_path(rel):
        return ""
    return preserves_existing_tests(before_code, after_code, path=rel)


def apply_patch(env: EditEnv, args: ApplyPatchArgs) -> ToolResult:
    proposal, error = validate_fix_patch(
        env.root, args.patch, allowed_files=env.all_allowed_paths(),
        forbidden_prefixes=FORBIDDEN_PREFIXES,
    )
    if error:
        return error_result("failed", error)

    for rel in proposal.files:
        problem = _check_target(env, rel)
        if problem:
            return error_result("denied", problem)

    # 若补丁涉及被授权补充用例的测试文件，应用前后做原子断言保护校验
    test_snapshots: dict[str, tuple[bool, str]] = {}
    for rel in proposal.files:
        if is_test_path(rel):
            target = env.root / rel
            existed = target.is_file()
            text = target.read_text(encoding="utf-8", errors="replace") if existed else ""
            test_snapshots[rel] = (existed, text)

    ok, output = _apply_patch(env.root, args.patch)
    if not ok:
        return error_result("failed", f"补丁应用失败: {output}")

    for rel, (existed, before_code) in test_snapshots.items():
        target = env.root / rel
        after_code = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        guard_err = _check_test_preservation(rel, before_code, after_code)
        if guard_err:
            # 原子回滚测试文件改动
            for r_rel, (r_existed, r_before) in test_snapshots.items():
                r_path = env.root / r_rel
                if r_existed:
                    r_path.write_text(r_before, encoding="utf-8")
                elif r_path.exists():
                    r_path.unlink()
            return error_result("denied", guard_err)

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

    if is_test_path(rel):
        before_code = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        guard_err = _check_test_preservation(rel, before_code, args.content)
        if guard_err:
            return error_result("denied", guard_err)

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


def _match_line_numbers(content: str, needle: str) -> list[int]:
    lines: list[int] = []
    start = 0
    while True:
        idx = content.find(needle, start)
        if idx < 0:
            break
        lines.append(content.count("\n", 0, idx) + 1)
        start = idx + max(1, len(needle))
    return lines


def _closest_line_hint(content: str, needle: str) -> str:
    first_line = next((line.strip() for line in needle.splitlines() if line.strip()), "")
    if len(first_line) < 3:
        return ""
    hits = [
        idx + 1 for idx, line in enumerate(content.splitlines())
        if first_line in line
    ][:5]
    return f"；首行相近出现在行 {', '.join(map(str, hits))}" if hits else ""


def edit_file(env: EditEnv, args: EditFileArgs) -> ToolResult:
    """对允许范围内的单个文件做精确唯一 Search-and-Replace 局部编辑。"""
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
    if not path.is_file():
        return error_result("failed", f"目标文件不存在: {rel}")

    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return error_result("failed", f"读取失败: {exc}")

    needle = args.old_text.replace("\r\n", "\n")
    replacement = args.new_text.replace("\r\n", "\n")
    if needle == replacement:
        return error_result("invalid_args", f"old_text 与 new_text 相同，未对 {rel} 做修改")

    matches = original.count(needle)
    if matches == 0:
        hint = _closest_line_hint(original, needle)
        return error_result(
            "failed",
            f"未找到完全匹配的 old_text（文件: {rel}{hint}）；请先用 read_file 核对精确行内容与缩进",
        )
    if matches > 1:
        line_nums = _match_line_numbers(original, needle)[:6]
        return error_result(
            "failed",
            f"old_text 在 {rel} 中匹配到 {matches} 处（行号 {', '.join(map(str, line_nums))}），"
            "请补充前后上下文使其唯一",
        )

    updated = original.replace(needle, replacement, 1)
    guard_err = _check_test_preservation(rel, original, updated)
    if guard_err:
        return error_result("denied", guard_err)

    payload = updated.encode("utf-8")
    if len(payload) > MAX_FILE_BYTES:
        return error_result("denied", f"单文件超过 {MAX_FILE_BYTES} bytes")
    diff_text = unified_diff(original, updated, rel)
    delta_bytes = max(len(diff_text.encode("utf-8")), len(replacement.encode("utf-8")))
    if env.written_bytes + delta_bytes > MAX_TOTAL_WRITE_BYTES:
        return error_result("denied", f"单轮累计写入超过 {MAX_TOTAL_WRITE_BYTES} bytes")

    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return error_result("failed", f"写入失败: {exc}")

    env.written_bytes += delta_bytes
    digest = tree_hash(env.root)
    return ok_result(
        f"已精确编辑 {rel}；tree_hash={digest[:12]}\n{diff_text}",
        meta={"path": rel, "diff": diff_text, "bytes": delta_bytes, "tree_hash": digest},
    )


__all__ = [
    "ApplyPatchArgs", "WriteFileArgs", "EditFileArgs", "EditEnv",
    "MAX_FILE_BYTES", "MAX_TOTAL_WRITE_BYTES", "FORBIDDEN_PREFIXES",
    "apply_patch", "write_file", "edit_file",
]
