"""公共 Python API 契约快照与破坏检测。"""
from __future__ import annotations

import ast
from pathlib import Path

from ..models import Finding, FindingCategory, FindingSource, Severity
from .diff import read_text

PY_EXTS = {".py"}


def _iter_py(root: Path) -> list[Path]:
    return [p for p in sorted(root.rglob("*.py")) if p.is_file() and ".git" not in p.parts]


def extract_signatures(root: Path, paths: list[str] | None = None) -> dict[str, dict]:
    result: dict[str, dict] = {}
    targets = [root / p for p in paths] if paths else _iter_py(root)
    for path in targets:
        if not path.is_file() or path.suffix not in PY_EXTS:
            continue
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(read_text(path), filename=rel)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if node.name.startswith("_"):
                continue
            if isinstance(node, ast.ClassDef):
                sig = f"class {node.name}"
            else:
                args = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
                ret = ast.unparse(node.returns) if node.returns else "Any"
                sig = f"{node.name}({', '.join(args)}) -> {ret}"
            result[f"{rel}:{node.name}"] = {"path": rel, "name": node.name, "signature": sig}
    return result


def _callers(root: Path, names: set[str], exclude: set[str]) -> list[str]:
    hits: list[str] = []
    for path in _iter_py(root):
        rel = path.relative_to(root).as_posix()
        if rel in exclude:
            continue
        try:
            tree = ast.parse(read_text(path), filename=rel)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                    if name in names:
                        label = f"{rel}:{node.name}"
                        if label not in hits:
                            hits.append(label)
    return hits


def check_api_contract(
    base_root: Path,
    head_root: Path,
    changed_paths: list[str],
    *,
    head_files: list[str] | None = None,
) -> list[Finding]:
    """只检查 base 中已存在的公共符号在 head 中是否被删除/改签名。"""
    if not base_root.is_dir() or not head_root.is_dir():
        return []
    base_sigs = extract_signatures(base_root)
    head_scope = head_files or changed_paths
    head_sigs = extract_signatures(head_root, head_scope) if head_scope else extract_signatures(head_root)
    findings: list[Finding] = []
    changed = set(changed_paths)
    for key, base in base_sigs.items():
        if base["path"] not in changed:
            continue
        head = head_sigs.get(key)
        if head is None:
            callers = _callers(head_root, {base["name"]}, changed)
            findings.append(Finding(
                source=FindingSource.deterministic,
                rule_id="API-001",
                category=FindingCategory.api_contract,
                severity=Severity.major,
                blocking=True,
                auto_fixable=True,
                path=base["path"],
                message=f"公共符号被删除: {base['signature']}",
                evidence=f"{base['path']} 中的 {base['signature']} 在 head 中不存在",
                suggestion="恢复该公共符号，或同步更新所有调用方",
            ))
            if callers:
                findings[-1] = findings[-1].model_copy(update={
                    "evidence": findings[-1].evidence + "；调用方: " + ", ".join(callers[:5])
                })
        elif head["signature"] != base["signature"]:
            callers = _callers(head_root, {base["name"]}, changed)
            findings.append(Finding(
                source=FindingSource.deterministic,
                rule_id="API-001",
                category=FindingCategory.api_contract,
                severity=Severity.major,
                blocking=True,
                auto_fixable=True,
                path=base["path"],
                message=f"公共 API 签名破坏: {base['signature']} -> {head['signature']}",
                evidence=(
                    f"{base['path']} 签名由 {base['signature']} 变为 {head['signature']}"
                    + ("；调用方: " + ", ".join(callers[:5]) if callers else "")
                ),
                suggestion="保持签名兼容，或同步修改调用方",
            ))
    return findings


__all__ = ["extract_signatures", "check_api_contract"]
