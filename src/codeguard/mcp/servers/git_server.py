"""Git 历史上下文 MCP Server：提供只读的 blame / file log / commit show 工具。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("codeguard-git-history")


def _resolve_repo_path(repo_root: str, rel_path: str = "") -> tuple[Path, Path | None, str]:
    base = Path(repo_root or os.environ.get("CODEGUARD_REPO_ROOT") or ".").resolve()
    if not base.exists():
        return base, None, f"仓库路径不存在: {base}"
    if not rel_path:
        return base, None, ""
    target = (base / rel_path).resolve()
    try:
        rel = target.relative_to(base)
    except ValueError:
        return base, None, f"拒绝越界路径: {rel_path}"
    return base, rel, ""


def _run_git(base: Path, args: list[str]) -> tuple[bool, str]:
    proc = subprocess.run(
        ["git", "-C", str(base), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or f"git exit {proc.returncode}").strip()
    return True, proc.stdout


@mcp.tool()
def git_blame(
    path: str,
    start_line: int = 1,
    end_line: int = 50,
    repo_root: str = "",
) -> dict[str, Any]:
    """查看文件指定行范围的 Git Blame 作者、提交与摘要，用于判断历史设计意图。"""
    base, rel, err = _resolve_repo_path(repo_root, path)
    if err or rel is None:
        return {"ok": False, "error": err or "缺少 path"}
    s_line = max(1, int(start_line))
    e_line = max(s_line, min(s_line + 120, int(end_line)))
    ok, out = _run_git(
        base,
        ["blame", "--line-porcelain", "-L", f"{s_line},{e_line}", "--", rel.as_posix()],
    )
    if not ok:
        return {"ok": False, "error": out}

    entries: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for line in out.splitlines():
        if line.startswith("\t"):
            cur["code"] = line[1:]
            entries.append(cur)
            cur = {}
        elif not cur and len(line.split()) >= 3 and len(line.split()[0]) >= 8:
            parts = line.split()
            cur = {"commit": parts[0][:10], "line": int(parts[2])}
        elif line.startswith("author "):
            cur["author"] = line[7:]
        elif line.startswith("summary "):
            cur["summary"] = line[8:]

    return {"ok": True, "path": rel.as_posix(), "entries": entries[:120]}


@mcp.tool()
def git_file_log(
    path: str,
    limit: int = 10,
    repo_root: str = "",
) -> dict[str, Any]:
    """查看单个文件最近 N 次提交记录。"""
    base, rel, err = _resolve_repo_path(repo_root, path)
    if err or rel is None:
        return {"ok": False, "error": err or "缺少 path"}
    n = max(1, min(30, int(limit)))
    ok, out = _run_git(
        base,
        ["log", "-n", str(n), "--pretty=format:%h|%an|%ad|%s", "--date=short", "--", rel.as_posix()],
    )
    if not ok:
        return {"ok": False, "error": out}
    commits = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({"hash": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]})
    return {"ok": True, "path": rel.as_posix(), "commits": commits}


@mcp.tool()
def git_show_commit(
    commit: str,
    stat_only: bool = True,
    repo_root: str = "",
) -> dict[str, Any]:
    """查看指定 commit 的元数据与变更统计（默认 stat_only=True 控制输出体积）。"""
    ref = (commit or "").strip()
    if not ref or ref.startswith("-"):
        return {"ok": False, "error": "非法 commit 引用"}
    base, _, err = _resolve_repo_path(repo_root, "")
    if err:
        return {"ok": False, "error": err}
    args = ["show", "--stat" if stat_only else "--patch", "--no-color", ref]
    ok, out = _run_git(base, args)
    if not ok:
        return {"ok": False, "error": out}
    return {"ok": True, "commit": ref, "output": out[:6000]}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
