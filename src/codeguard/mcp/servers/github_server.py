"""GitHub 上下文 MCP Server：获取 Issue、PR 评论与提交状态检查（离线优雅降级）。"""
from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("codeguard-github-context")


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "CodeGuard-MCP-Client",
    }
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _resolve_repo(repo: str) -> str:
    return (repo or os.environ.get("CODEGUARD_PR_REPO") or "").strip()


def _github_get(path: str) -> dict[str, Any]:
    url = f"https://api.github.com{path}"
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(url, headers=_github_headers())
        if resp.status_code >= 400:
            return {"ok": False, "status": resp.status_code, "error": resp.text[:300]}
        return {"ok": True, "data": resp.json()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:240]}


@mcp.tool()
def get_issue(issue_number: int, repo: str = "") -> dict[str, Any]:
    """获取关联 GitHub Issue 标题、状态、标签与正文摘要。"""
    target_repo = _resolve_repo(repo)
    if not target_repo:
        return {"ok": False, "error": "缺少 repo（owner/name）参数或 CODEGUARD_PR_REPO"}
    res = _github_get(f"/repos/{target_repo}/issues/{int(issue_number)}")
    if not res.get("ok"):
        return res
    data = res["data"]
    return {
        "ok": True,
        "repo": target_repo,
        "number": data.get("number"),
        "title": data.get("title", ""),
        "state": data.get("state", ""),
        "labels": [lb.get("name", "") for lb in (data.get("labels") or [])],
        "body": (data.get("body") or "")[:2000],
    }


@mcp.tool()
def list_pr_comments(pr_number: int, repo: str = "", limit: int = 20) -> dict[str, Any]:
    """获取指定 PR 的讨论评论，避免重复提出已解决的问题。"""
    target_repo = _resolve_repo(repo)
    if not target_repo:
        return {"ok": False, "error": "缺少 repo（owner/name）参数或 CODEGUARD_PR_REPO"}
    n = max(1, min(50, int(limit)))
    res = _github_get(f"/repos/{target_repo}/issues/{int(pr_number)}/comments?per_page={n}")
    if not res.get("ok"):
        return res
    items = [
        {
            "id": c.get("id"),
            "user": (c.get("user") or {}).get("login", ""),
            "body": (c.get("body") or "")[:500],
            "created_at": c.get("created_at", ""),
        }
        for c in (res.get("data") or [])
    ]
    return {"ok": True, "repo": target_repo, "pr_number": pr_number, "comments": items}


@mcp.tool()
def get_commit_status(ref: str, repo: str = "") -> dict[str, Any]:
    """获取指定 commit SHA 或分支的 GitHub 外部 CI 组合状态。"""
    target_repo = _resolve_repo(repo)
    if not target_repo or not ref:
        return {"ok": False, "error": "缺少 repo 或 ref"}
    res = _github_get(f"/repos/{target_repo}/commits/{ref.strip()}/status")
    if not res.get("ok"):
        return res
    data = res["data"]
    statuses = [
        {
            "context": s.get("context", ""),
            "state": s.get("state", ""),
            "description": s.get("description", ""),
        }
        for s in (data.get("statuses") or [])[:15]
    ]
    return {
        "ok": True,
        "repo": target_repo,
        "sha": data.get("sha", ""),
        "state": data.get("state", ""),
        "statuses": statuses,
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
