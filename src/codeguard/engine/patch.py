"""修复补丁校验、测试断言保护与应用。"""
from __future__ import annotations

import ast
from pathlib import Path

from ..models import FixProposal, validate_repo_path
from .diff import apply_patch, patch_paths


def _count_assertions(node: ast.AST) -> int:
    count = 0
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            count += 1
        elif isinstance(child, ast.Call):
            fn = child.func
            if isinstance(fn, ast.Attribute) and (
                fn.attr.startswith("assert") or fn.attr in ("raises", "warns", "fail")
            ):
                count += 1
            elif isinstance(fn, ast.Name) and fn.id.startswith("assert"):
                count += 1
    return count


def _count_skip_decorators(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    skips = 0
    for dec in node.decorator_list:
        text = ast.unparse(dec).lower() if hasattr(ast, "unparse") else ""
        if "skip" in text or "xfail" in text:
            skips += 1
    return skips


def _collect_test_signatures(tree: ast.AST) -> dict[str, tuple[int, int]]:
    sigs: dict[str, tuple[int, int]] = {}

    def _visit(body: list[ast.stmt], prefix: str = "") -> None:
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                _visit(stmt.body, prefix=f"{prefix}{stmt.name}.")
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if stmt.name.startswith("test_"):
                    qual = f"{prefix}{stmt.name}"
                    sigs[qual] = (_count_assertions(stmt), _count_skip_decorators(stmt))

    if isinstance(tree, ast.Module):
        _visit(tree.body)
    return sigs


def preserves_existing_tests(before_code: str, after_code: str, *, path: str = "") -> str:
    """检查对测试文件的补充修改是否保留了全部既有测试函数与断言强度。"""
    if path and not path.endswith(".py"):
        # 非 Python 测试文件：仅阻止删除现有 assert / expect 语句
        before_asserts = sum(
            1 for ln in before_code.splitlines() if "assert" in ln.lower() or "expect(" in ln.lower()
        )
        after_asserts = sum(
            1 for ln in after_code.splitlines() if "assert" in ln.lower() or "expect(" in ln.lower()
        )
        if after_asserts < before_asserts:
            return "禁止削弱或删除既有测试断言"
        return ""

    try:
        after_tree = ast.parse(after_code or "")
    except SyntaxError as exc:
        return f"修改后的测试文件存在语法错误: {exc.msg} (line {exc.lineno})"

    after_sigs = _collect_test_signatures(after_tree)
    if not (before_code or "").strip():
        if not after_sigs:
            return "新增测试文件至少需要包含一个 test_* 测试函数"
        return ""

    try:
        before_tree = ast.parse(before_code)
    except SyntaxError:
        return ""

    before_sigs = _collect_test_signatures(before_tree)
    for name, (before_asserts, before_skips) in before_sigs.items():
        if name not in after_sigs:
            return f"禁止删除或重命名既有测试函数: {name}"
        after_asserts, after_skips = after_sigs[name]
        if after_asserts < before_asserts:
            return f"禁止减少既有测试 {name} 的断言数量（{before_asserts} -> {after_asserts}）"
        if after_skips > before_skips:
            return f"禁止为既有测试 {name} 新增 skip/xfail 跳过标记"
    return ""


def validate_fix_patch(
    root: Path,
    patch_text: str,
    *,
    allowed_files: list[str],
    forbidden_prefixes: tuple[str, ...] = (".github/workflows/", ".codeguard/"),
) -> tuple[FixProposal, str]:
    """校验补丁路径、大小与可应用性；返回 (proposal, error)。"""
    if not patch_text.strip():
        return FixProposal(patch="", files=[], applies_cleanly=False), "补丁为空"
    try:
        paths = patch_paths(patch_text)
    except Exception as exc:  # noqa: BLE001
        return FixProposal(patch=patch_text, applies_cleanly=False), str(exc)
    allowed = {validate_repo_path(p) for p in allowed_files}
    for path in paths:
        if path not in allowed:
            return (
                FixProposal(patch=patch_text, files=paths, applies_cleanly=False),
                f"补丁修改了未授权文件: {path}",
            )
        if any(path.startswith(prefix) for prefix in forbidden_prefixes):
            return (
                FixProposal(patch=patch_text, files=paths, applies_cleanly=False),
                f"补丁修改了受保护路径: {path}",
            )
    if len(patch_text) > 200_000:
        return FixProposal(patch=patch_text, files=paths, applies_cleanly=False), "补丁过大"
    ok, output = apply_patch(root, patch_text, check_only=True)
    proposal = FixProposal(
        patch=patch_text,
        files=paths,
        applies_cleanly=ok,
        explanation=output if not ok else "",
    )
    return proposal, "" if ok else output


__all__ = ["preserves_existing_tests", "validate_fix_patch"]
