"""基于 Python 标准库 AST 的仓库符号图谱提取与跨文件故障定位器（MCP 工具内核）。

两遍扫描：
- Pass 1：收集所有函数/类定义签名（SymbolContract，私有函数不冻结）。
- Pass 2：遍历函数体内 ast.Call 节点，反向填充跨文件 callers（调用图）。

故障定位 rank_suspect_files：词法匹配（文件名/符号名命中 issue 文本）+ 调用链
1-hop 邻域传播，解决"报错在调用方但根因在被调方（或反之）"的定位难题。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from .models import SymbolContract

IGNORE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache"}


def _iter_py_files(repo_dir: Path) -> list[Path]:
    files: list[Path] = []
    for p in sorted(repo_dir.rglob("*.py")):
        if any(part in IGNORE_DIRS for part in p.parts):
            continue
        files.append(p)
    return files


def extract_repo_symbols(repo_dir: Path) -> dict[str, SymbolContract]:
    """扫描仓库所有 Python 文件，构建函数/类签名契约与跨文件调用关系图（Call Graph）。"""
    contracts: dict[str, SymbolContract] = {}
    name_to_ids: dict[str, list[str]] = {}
    file_trees: list[tuple[str, ast.AST]] = []

    py_files = _iter_py_files(repo_dir)
    idx = 1

    # Pass 1: 收集所有函数与类定义签名
    for fpath in py_files:
        rel_path = fpath.relative_to(repo_dir).as_posix()
        try:
            source = fpath.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=rel_path)
        except Exception:
            continue  # 语法损坏文件跳过（machine_review 才负责拦截语法错误）
        file_trees.append((rel_path, tree))

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sym_id = f"SYM_{idx:03d}"
                idx += 1
                args = [a.arg for a in node.args.args if a.arg != "self"]
                ret_ann = ast.unparse(node.returns) if node.returns else ""
                contract = SymbolContract(
                    symbol_id=sym_id,
                    file_path=rel_path,
                    symbol_name=node.name,
                    kind="function",
                    arg_names=args,
                    return_annotation=ret_ann,
                    callers=[],
                    frozen_signature=not node.name.startswith("_"),
                )
                contracts[sym_id] = contract
                name_to_ids.setdefault(node.name, []).append(sym_id)

    # Pass 2: 遍历函数体内的 ast.Call 节点，反向填充跨文件 callers
    for rel_path, tree in file_trees:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            caller_label = f"{rel_path}:{node.name}"
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    callee_name = None
                    if isinstance(child.func, ast.Name):
                        callee_name = child.func.id
                    elif isinstance(child.func, ast.Attribute):
                        callee_name = child.func.attr
                    if callee_name and callee_name in name_to_ids:
                        for target_id in name_to_ids[callee_name]:
                            if caller_label not in contracts[target_id].callers:
                                contracts[target_id].callers.append(caller_label)

    return contracts


def rank_suspect_files(repo_dir: Path, issue_text: str, top_k: int = 3) -> list[str]:
    """结合词法匹配 + AST 符号命中 + 调用图 1-hop 邻域传播，对疑似故障文件排序。"""
    contracts = extract_repo_symbols(repo_dir)
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", issue_text))
    lower_tokens = {t.lower() for t in tokens}

    scores: dict[str, float] = {}
    for fpath in _iter_py_files(repo_dir):
        rel = fpath.relative_to(repo_dir).as_posix()
        scores[rel] = 0.0
        stem = fpath.stem.lower()
        if stem in lower_tokens:
            scores[rel] += 3.0

    # AST 符号精确命中与调用链传播加分
    for sym in contracts.values():
        if sym.symbol_name in tokens or sym.symbol_name.lower() in lower_tokens:
            scores[sym.file_path] = scores.get(sym.file_path, 0.0) + 5.0
            # 向调用方传播权重：报错在被调函数，调用方文件同样可疑（反之亦然）
            for caller in sym.callers:
                caller_file = caller.split(":")[0]
                if caller_file in scores:
                    scores[caller_file] += 2.5

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f for f, sc in ranked[:top_k] if sc > 0] or [f for f, _ in ranked[:top_k]]


def find_repo_test_files(repo_dir: Path) -> list[str]:
    """收集仓库既有测试文件（相对路径），供 P2P 回归门控使用。"""
    return [
        p.relative_to(repo_dir).as_posix()
        for p in _iter_py_files(repo_dir)
        if p.name.startswith("test_") or p.name.endswith("_test.py")
    ]
