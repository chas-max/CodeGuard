"""知识与运行库 API 反射 MCP Server：仅反射已安装三方库或读取历史审查事故文档，绝不执行 PR 代码。"""
from __future__ import annotations

import importlib
import inspect
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("codeguard-knowledge")


def _is_under_repo(mod_file: str | None) -> bool:
    repo_env = os.environ.get("CODEGUARD_REPO_ROOT", "").strip()
    if not repo_env or not mod_file:
        return False
    try:
        repo_path = Path(repo_env).resolve()
        target = Path(mod_file).resolve()
        target.relative_to(repo_path)
        return True
    except ValueError:
        return False


@mcp.tool()
def inspect_package_api(
    module_name: str,
    symbol: str = "",
) -> dict[str, Any]:
    """安全反射已安装标准库或第三方依赖包的公开签名与文档（禁止导入受测 PR 仓库代码）。"""
    mod_name = (module_name or "").strip()
    if not mod_name or mod_name.startswith("."):
        return {"ok": False, "error": "非法的模块名"}

    repo_root = os.environ.get("CODEGUARD_REPO_ROOT", "").strip()
    if repo_root:
        top_pkg = mod_name.split(".", 1)[0]
        repo_p = Path(repo_root).resolve()
        if (repo_p / top_pkg).exists() or (repo_p / f"{top_pkg}.py").exists() or (repo_p / "src" / top_pkg).exists():
            return {"ok": False, "error": f"安全红线：禁止在宿主机导入待审仓库模块 {mod_name}"}

    try:
        mod = importlib.import_module(mod_name)
    except Exception as exc:
        return {"ok": False, "error": f"导入失败: {exc}"}

    if _is_under_repo(getattr(mod, "__file__", None)):
        return {"ok": False, "error": f"安全红线：模块 {mod_name} 位于待审仓库内，已拦截"}

    sym = (symbol or "").strip()
    if sym:
        obj = getattr(mod, sym, None)
        if obj is None:
            return {"ok": False, "error": f"符号不存在: {mod_name}.{sym}"}
        try:
            sig = str(inspect.signature(obj))
        except Exception:
            sig = ""
        return {
            "ok": True,
            "module": mod_name,
            "symbol": sym,
            "type": type(obj).__name__,
            "signature": sig,
            "doc": (inspect.getdoc(obj) or "")[:1500],
        }

    exports = [name for name in dir(mod) if not name.startswith("_")][:80]
    return {
        "ok": True,
        "module": mod_name,
        "doc": (inspect.getdoc(mod) or "")[:800],
        "exports": exports,
    }


@mcp.tool()
def search_incidents(
    query: str,
    limit: int = 5,
) -> dict[str, Any]:
    """检索团队历史审查事故报告（reports/incidents 或 reports/）与规范说明中的相似案例。"""
    kw = (query or "").strip().lower()
    if not kw:
        return {"ok": False, "error": "query 不能为空", "matches": []}

    search_dirs: list[Path] = []
    reports_env = os.environ.get("CODEGUARD_REPORTS_DIR", "").strip()
    if reports_env:
        search_dirs.append(Path(reports_env))
    repo_env = os.environ.get("CODEGUARD_REPO_ROOT", "").strip()
    if repo_env:
        search_dirs.append(Path(repo_env) / "reports")
        search_dirs.append(Path(repo_env) / "docs")

    seen: set[Path] = set()
    matches: list[dict[str, Any]] = []
    max_items = max(1, min(20, int(limit)))

    for root in search_dirs:
        if not root.is_dir():
            continue
        for file_path in sorted(root.rglob("*")):
            if not file_path.is_file() or file_path.suffix.lower() not in (".md", ".txt", ".json", ".yml", ".yaml"):
                continue
            resolved = file_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            text = file_path.read_text(encoding="utf-8", errors="replace")
            idx = text.lower().find(kw)
            if idx >= 0:
                start = max(0, idx - 80)
                end = min(len(text), idx + 280)
                matches.append({
                    "file": str(file_path.relative_to(root)),
                    "snippet": text[start:end].strip(),
                })
                if len(matches) >= max_items:
                    return {"ok": True, "query": query, "matches": matches}

    return {"ok": True, "query": query, "matches": matches}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
