"""PR 代码审查领域专用内置工具（全部默认仅读，严格受工作区路径与输出预算约束）。"""
from __future__ import annotations

import ast
import difflib
import fnmatch
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..engine.api_contract import extract_signatures
from ..engine.patch import validate_fix_patch
from ..engine.repo_map import build_repo_map, is_test_path, related_files as _repo_related_files
from .base import (
    error_result,
    normalize_rel,
    ok_result,
    read_text_limited,
    safe_resolve,
    truncate,
)
from .spec import ToolResult

_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".tmp",
})
_TEXT_EXTS = frozenset({
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".yml", ".yaml", ".json", ".toml", ".md", ".sql", ".sh",
})


class AstSymbolsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=260)
    symbol: str = Field(default="", max_length=120)


class FindSymbolUsagesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=120)
    glob: str = Field(default="", max_length=120)
    max_results: int = Field(default=40, ge=1, le=100)


class DiffBaseHeadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=260)
    context_lines: int = Field(default=3, ge=0, le=12)


class GetPrMetadataArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_body: bool = True


class InspectTestsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(default="", max_length=260)
    test_glob: str = Field(default="", max_length=120)


class QueryFindingsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str = Field(default="", max_length=260)
    severity: str = Field(default="", max_length=32)
    source: str = Field(default="", max_length=48)
    limit: int = Field(default=30, ge=1, le=100)


class CheckPatchGuardArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patch: str = Field(min_length=1, max_length=200_000)


def _trunc(text: str, max_chars: int) -> str:
    clipped, _ = truncate(text, max_chars)
    return clipped


def _iter_workspace_files(root: Path, glob_pat: str = "") -> list[Path]:
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel_path.parts):
            continue
        rel = rel_path.as_posix()
        if glob_pat and not fnmatch.fnmatch(rel, glob_pat):
            continue
        if path.suffix.lower() in _TEXT_EXTS or not path.suffix:
            out.append(path)
    return out


def _py_ast_symbols(text: str, filter_sym: str) -> list[str]:
    tree = ast.parse(text)
    lines: list[str] = []

    def _visit(nodes: list[ast.stmt], prefix: str = "") -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                qual = f"{prefix}{node.name}"
                if not filter_sym or filter_sym in qual.lower():
                    doc = (ast.get_docstring(node) or "").splitlines()[0:1]
                    doc_part = f" — {doc[0]}" if doc else ""
                    lines.append(
                        f"L{node.lineno}-{node.end_lineno or node.lineno} class {qual}{doc_part}"
                    )
                _visit(node.body, prefix=f"{qual}.")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{node.name}"
                if not filter_sym or filter_sym in qual.lower():
                    params = [a.arg for a in node.args.args]
                    if node.args.vararg:
                        params.append(f"*{node.args.vararg.arg}")
                    if node.args.kwarg:
                        params.append(f"**{node.args.kwarg.arg}")
                    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
                    kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                    lines.append(
                        f"L{node.lineno}-{node.end_lineno or node.lineno} "
                        f"{kind} {qual}({', '.join(params)}){ret}"
                    )

    _visit(tree.body)
    return lines


def ast_symbols(root: Path, args: AstSymbolsArgs, *, max_chars: int = 8000) -> ToolResult:
    """提取工作区文件的类、函数与方法签名大纲及精确行号。"""
    try:
        rel, target = safe_resolve(root, args.path)
    except ValueError as exc:
        return error_result("denied", str(exc))
    if not target.is_file():
        return error_result("failed", f"文件不存在: {args.path}")

    text, read_err = read_text_limited(target)
    if read_err:
        return error_result("failed", read_err)

    filt = args.symbol.strip().lower()
    if target.suffix.lower() in (".py", ".pyi"):
        try:
            items = _py_ast_symbols(text, filt)
        except SyntaxError as exc:
            return error_result("failed", f"{rel} 语法错误 L{exc.lineno}: {exc.msg}")
    else:
        pattern = re.compile(
            r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type|const)\s+([A-Za-z0-9_]+)"
        )
        items = []
        for idx, line in enumerate(text.splitlines(), 1):
            m = pattern.match(line)
            if m and (not filt or filt in m.group(1).lower()):
                items.append(f"L{idx} {line.strip()[:120]}")

    body = f"[{rel}] 共 {len(items)} 个符号:\n" + "\n".join(items[:200])
    return ok_result(_trunc(body, max_chars), meta={"path": rel, "count": len(items)})


def find_symbol_usages(
    root: Path,
    args: FindSymbolUsagesArgs,
    *,
    max_chars: int = 8000,
) -> ToolResult:
    """跨工作区精确查找符号的定义、导入与调用位置。"""
    sym = args.symbol.strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_\.]*$", sym):
        return error_result("invalid_args", f"非法符号名: {sym}")

    short_sym = sym.rsplit(".", 1)[-1]
    word_re = re.compile(rf"\b{re.escape(short_sym)}\b")
    def_re = re.compile(rf"^\s*(?:async\s+)?(?:def|class|function|interface)\s+{re.escape(short_sym)}\b")
    import_re = re.compile(rf"^\s*(?:from\s+\S+\s+import|import\s+).*\b{re.escape(short_sym)}\b")

    hits: list[str] = []
    for path in _iter_workspace_files(root, args.glob):
        text, read_err = read_text_limited(path)
        if read_err or short_sym not in text:
            continue
        rel = path.relative_to(root).as_posix()
        for lineno, line in enumerate(text.splitlines(), 1):
            if not word_re.search(line):
                continue
            if def_re.match(line):
                kind = "def"
            elif import_re.match(line):
                kind = "import"
            else:
                kind = "ref"
            hits.append(f"{rel}:{lineno} [{kind}] {line.strip()[:160]}")
            if len(hits) >= args.max_results:
                body = "\n".join(hits) + "\n...(已达 max_results 上限)"
                return ok_result(_trunc(body, max_chars), meta={"symbol": sym, "count": len(hits)})

    body = "\n".join(hits) if hits else f"未找到符号 `{sym}` 的引用。"
    return ok_result(_trunc(body, max_chars), meta={"symbol": sym, "count": len(hits)})


def diff_base_head(
    head_root: Path,
    base_root: Path | None,
    args: DiffBaseHeadArgs,
    *,
    max_chars: int = 8000,
) -> ToolResult:
    """对比指定文件在 base 与 head 分支之间的 unified diff 与 Python 公共 API 签名变化。"""
    try:
        rel = normalize_rel(args.path)
    except ValueError as exc:
        return error_result("denied", str(exc))

    head_file = head_root / rel
    head_text = ""
    if head_file.is_file():
        head_text, err = read_text_limited(head_file)
        if err:
            return error_result("failed", err)

    if base_root is None:
        return ok_result(
            f"当前上下文未提供 base 分支快照；head 文件 `{rel}` 共 {len(head_text.splitlines())} 行。",
            meta={"path": rel, "has_base": False},
        )

    base_file = base_root / rel
    base_text = ""
    if base_file.is_file():
        base_text, err = read_text_limited(base_file)
        if err:
            return error_result("failed", err)

    if not head_file.is_file() and not base_file.is_file():
        return error_result("failed", f"base 与 head 均不存在文件: {rel}")

    diff_lines = list(
        difflib.unified_diff(
            base_text.splitlines(),
            head_text.splitlines(),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            n=args.context_lines,
            lineterm="",
        )
    )
    parts: list[str] = []
    if diff_lines:
        parts.append("\n".join(diff_lines))
    else:
        parts.append(f"`{rel}` 在 base 与 head 之间无文本差异。")

    if rel.endswith(".py"):
        base_sigs = extract_signatures(base_root, [rel]) if base_file.is_file() else {}
        head_sigs = extract_signatures(head_root, [rel]) if head_file.is_file() else {}
        changed_sigs: list[str] = []
        for k in sorted(set(base_sigs) | set(head_sigs)):
            b = (base_sigs.get(k) or {}).get("signature", "<removed>")
            h = (head_sigs.get(k) or {}).get("signature", "<removed>")
            if b != h:
                changed_sigs.append(f"  {k}: {b}  ==>  {h}")
        if changed_sigs:
            parts.append("Python 公共签名变化:\n" + "\n".join(changed_sigs[:30]))

    return ok_result(_trunc("\n\n".join(parts), max_chars), meta={"path": rel, "has_base": True})


def get_pr_metadata(
    pr_context: dict[str, Any] | None,
    args: GetPrMetadataArgs,
    *,
    max_chars: int = 8000,
) -> ToolResult:
    """获取当前待审 PR 的脱敏元数据（仓库、PR 号、分支、标题、描述、变更文件）。"""
    ctx = dict(pr_context or {})
    if not ctx:
        return ok_result("当前运行未注入 PR 元数据（可能处于本地目录扫描模式）。")

    summary: dict[str, Any] = {
        "repo": ctx.get("repo", ""),
        "pr_number": ctx.get("pr_number", 0),
        "title": ctx.get("title", ""),
        "author": ctx.get("author", ""),
        "base_ref": ctx.get("base_ref", ""),
        "head_ref": ctx.get("head_ref", ""),
        "tier": ctx.get("tier", ""),
        "changed_files": list(ctx.get("changed_files") or [])[:80],
    }
    if args.include_body:
        summary["body"] = str(ctx.get("body", ""))[:2000]
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    return ok_result(_trunc(text, max_chars), meta={"pr_number": summary["pr_number"]})


def inspect_tests(
    root: Path,
    args: InspectTestsArgs,
    *,
    repo_map: dict[str, Any] | None = None,
    max_chars: int = 8000,
) -> ToolResult:
    """静态发现并解析测试文件与测试用例名称（绝不执行测试代码）。"""
    candidates: list[str] = []
    if args.path:
        try:
            rel = normalize_rel(args.path)
        except ValueError as exc:
            return error_result("denied", str(exc))
        if is_test_path(rel):
            candidates.append(rel)
        else:
            rmap = repo_map if repo_map else build_repo_map(root)
            rel_list = _repo_related_files(root, rmap, [rel])
            candidates.extend(p for p in rel_list if is_test_path(p))
    if not candidates:
        for path in _iter_workspace_files(root, args.test_glob):
            rel = path.relative_to(root).as_posix()
            if is_test_path(rel):
                candidates.append(rel)
            if len(candidates) >= 30:
                break

    seen: set[str] = set()
    blocks: list[str] = []
    case_re = re.compile(r"^\s*(?:async\s+def|def)\s+(test_[A-Za-z0-9_]+)|^\s*(?:it|test)\(\s*['\"]([^'\"]+)")
    for rel in candidates:
        if rel in seen:
            continue
        seen.add(rel)
        target = root / rel
        if not target.is_file():
            continue
        text, err = read_text_limited(target)
        if err:
            continue
        cases: list[str] = []
        for idx, line in enumerate(text.splitlines(), 1):
            m = case_re.match(line)
            if m:
                name = m.group(1) or m.group(2)
                cases.append(f"  L{idx}: {name}")
        blocks.append(f"{rel} ({len(cases)} cases):\n" + ("\n".join(cases[:25]) or "  <无显式用例函数>"))

    body = "\n\n".join(blocks) if blocks else "未发现匹配测试文件。"
    return ok_result(_trunc(body, max_chars), meta={"test_files": len(seen)})


def query_findings(
    findings: list[dict[str, Any]] | None,
    args: QueryFindingsArgs,
    *,
    max_chars: int = 8000,
) -> ToolResult:
    """检索前序静态扫描或验证阶段产出的告警列表。"""
    items = list(findings or [])
    if not items:
        return ok_result("当前审查状态尚无 findings 记录。")

    file_f = args.file.strip().lower()
    sev_f = args.severity.strip().upper()
    src_f = args.source.strip().lower()

    matched: list[dict[str, Any]] = []
    for f in items:
        if not isinstance(f, dict):
            continue
        fpath = str(f.get("file", "")).lower()
        fsev = str(f.get("severity", "")).upper()
        fsrc = str(f.get("source", "")).lower()
        if file_f and file_f not in fpath:
            continue
        if sev_f and sev_f != fsev:
            continue
        if src_f and src_f != fsrc:
            continue
        matched.append({
            "id": f.get("id", ""),
            "severity": f.get("severity", ""),
            "source": f.get("source", ""),
            "rule_id": f.get("rule_id", ""),
            "file": f.get("file", ""),
            "line": f.get("line", 0),
            "title": f.get("title", ""),
        })
        if len(matched) >= args.limit:
            break

    text = json.dumps({"total": len(items), "matched": len(matched), "items": matched}, ensure_ascii=False, indent=2)
    return ok_result(_trunc(text, max_chars), meta={"matched": len(matched)})


def check_patch_guard(
    root: Path,
    allowed_files: list[str],
    args: CheckPatchGuardArgs,
    *,
    max_chars: int = 8000,
) -> ToolResult:
    """只读预检修复补丁是否符合 allowed_files、禁改测试与受保护路径红线。"""
    del max_chars
    norm_allowed: list[str] = []
    for item in allowed_files:
        try:
            norm = normalize_rel(item)
            if norm not in norm_allowed:
                norm_allowed.append(norm)
        except ValueError:
            continue

    proposal, guard_err = validate_fix_patch(
        root,
        args.patch,
        allowed_files=norm_allowed,
        forbidden_prefixes=(".github/workflows/", ".codeguard/"),
    )
    if guard_err:
        return ok_result(
            json.dumps({"ok": False, "reason": guard_err}, ensure_ascii=False),
            meta={"allowed": False},
        )
    test_hits = [p for p in (proposal.files if proposal else []) if is_test_path(p)]
    if test_hits:
        return ok_result(
            json.dumps(
                {"ok": False, "reason": f"禁止修改测试文件: {', '.join(test_hits)}"},
                ensure_ascii=False,
            ),
            meta={"allowed": False},
        )
    return ok_result(
        json.dumps(
            {
                "ok": True,
                "files": proposal.files if proposal else [],
                "reason": "补丁通过修复护栏预检",
            },
            ensure_ascii=False,
        ),
        meta={"allowed": True},
    )


__all__ = [
    "AstSymbolsArgs",
    "FindSymbolUsagesArgs",
    "DiffBaseHeadArgs",
    "GetPrMetadataArgs",
    "InspectTestsArgs",
    "QueryFindingsArgs",
    "CheckPatchGuardArgs",
    "ast_symbols",
    "find_symbol_usages",
    "diff_base_head",
    "get_pr_metadata",
    "inspect_tests",
    "query_findings",
    "check_patch_guard",
]
