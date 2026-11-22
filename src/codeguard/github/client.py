"""GitHub REST 客户端（httpx）。测试使用 FakeGitHubClient，不触网。"""
from __future__ import annotations

from typing import Any, Callable

import httpx

from ..errors import GitHubError, RetryableError
from ..retry import with_retry


class GitHubClient:
    """GitHub REST 客户端。

    - `token`：PAT 等静态凭据，保持原有位置参数语义；
    - `token_provider(repo)`：GitHub App 模式按仓库解析 installation token；
    - `token_invalidator(repo)`：401 时失效缓存，再由本类重试一次。
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        token_provider: Callable[[str | None], str | None] | None = None,
        token_invalidator: Callable[[str | None], None] | None = None,
        api_url: str = "https://api.github.com",
        timeout: float = 30.0,
    ):
        self.token = token
        self.token_provider = token_provider
        self.token_invalidator = token_invalidator
        self.api_url = api_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.api_url,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _auth(self, repo: str | None) -> str | None:
        if self.token_provider is not None:
            value = self.token_provider(repo)
            if value:
                return value
        return self.token

    def _request(self, method: str, path: str, *, repo: str | None = None,
                 retry_auth: bool = True, **kwargs) -> Any:
        headers = dict(kwargs.pop("headers", None) or {})
        token = self._auth(repo)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = self._client.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise RetryableError(f"GitHub 请求超时: {method} {path}") from exc
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub 请求失败: {method} {path}: {exc}") from exc
        if resp.status_code == 401 and retry_auth and self.token_invalidator is not None:
            self.token_invalidator(repo)
            return self._request(method, path, repo=repo, retry_auth=False, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableError(f"GitHub {resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 400:
            raise GitHubError(f"GitHub {resp.status_code}: {resp.text[:500]}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    @with_retry(max_attempts=3)
    def get_pr(self, repo: str, number: int) -> dict:
        return self._request("GET", f"/repos/{repo}/pulls/{number}", repo=repo)

    @with_retry(max_attempts=3)
    def get_user(self) -> dict:
        return self._request("GET", "/user") or {}

    @with_retry(max_attempts=3)
    def list_repos(
        self,
        *,
        affiliation: str = "owner,collaborator,organization_member",
        per_page: int = 100,
        max_pages: int = 3,
        sort: str = "pushed",
    ) -> list[dict]:
        """当前 token 可见的仓库（用于前端库切换）。"""
        repos: list[dict] = []
        for page in range(1, max_pages + 1):
            batch = self._request(
                "GET", "/user/repos",
                params={
                    "affiliation": affiliation, "per_page": per_page,
                    "page": page, "sort": sort, "direction": "desc",
                },
            ) or []
            repos.extend(batch)
            if len(batch) < per_page:
                break
        return repos

    @with_retry(max_attempts=3)
    def list_files(self, repo: str, number: int, *, per_page: int = 100, max_pages: int = 10) -> list[dict]:
        files: list[dict] = []
        for page in range(1, max_pages + 1):
            batch = self._request(
                "GET", f"/repos/{repo}/pulls/{number}/files",
                repo=repo, params={"per_page": per_page, "page": page},
            )
            files.extend(batch or [])
            if not batch or len(batch) < per_page:
                break
        return files

    @with_retry(max_attempts=3)
    def get_tree(self, repo: str, sha: str) -> dict:
        return self._request("GET", f"/repos/{repo}/git/trees/{sha}", repo=repo, params={"recursive": "1"})

    @with_retry(max_attempts=3)
    def get_contents(self, repo: str, path: str, ref: str) -> dict:
        return self._request("GET", f"/repos/{repo}/contents/{path}", repo=repo, params={"ref": ref})

    @with_retry(max_attempts=3)
    def get_repo(self, repo: str) -> dict:
        return self._request("GET", f"/repos/{repo}", repo=repo)

    @with_retry(max_attempts=3)
    def get_branch_protection(self, repo: str, branch: str) -> dict | None:
        try:
            return self._request("GET", f"/repos/{repo}/branches/{branch}/protection", repo=repo)
        except GitHubError:
            return None

    def comment(self, repo: str, number: int, body: str) -> dict:
        return self._request("POST", f"/repos/{repo}/issues/{number}/comments", repo=repo, json={"body": body})

    def update_comment(self, repo: str, comment_id: int, body: str) -> dict:
        return self._request("PATCH", f"/repos/{repo}/issues/comments/{comment_id}", repo=repo, json={"body": body})

    def create_status(self, repo: str, sha: str, *, state: str, context: str, description: str) -> dict:
        return self._request(
            "POST", f"/repos/{repo}/statuses/{sha}", repo=repo,
            json={"state": state, "context": context, "description": description[:140]},
        )

    def submit_review(self, repo: str, number: int, *, event: str, body: str) -> dict:
        return self._request(
            "POST", f"/repos/{repo}/pulls/{number}/reviews", repo=repo,
            json={"event": event, "body": body},
        )

    def create_review_comment(
        self, repo: str, number: int, *, body: str, commit_id: str,
        path: str, line: int, side: str = "RIGHT",
    ) -> dict:
        return self._request(
            "POST", f"/repos/{repo}/pulls/{number}/comments", repo=repo,
            json={"body": body, "commit_id": commit_id, "path": path, "line": line, "side": side},
        )

    def update_review_comment(self, repo: str, comment_id: int, *, body: str) -> dict:
        return self._request(
            "PATCH", f"/repos/{repo}/pulls/comments/{comment_id}", repo=repo, json={"body": body},
        )

    @with_retry(max_attempts=3)
    def list_review_comments(self, repo: str, number: int, *, per_page: int = 100) -> list[dict]:
        return self._request(
            "GET", f"/repos/{repo}/pulls/{number}/comments", repo=repo,
            params={"per_page": per_page},
        ) or []

    @with_retry(max_attempts=3)
    def get_branch(self, repo: str, branch: str) -> dict:
        return self._request("GET", f"/repos/{repo}/branches/{branch}", repo=repo)

    @with_retry(max_attempts=3)
    def get_combined_status(self, repo: str, ref: str) -> dict:
        return self._request("GET", f"/repos/{repo}/commits/{ref}/status", repo=repo)

    @with_retry(max_attempts=3)
    def list_pulls(self, repo: str, *, state: str = "closed", per_page: int = 50) -> list[dict]:
        return self._request(
            "GET", f"/repos/{repo}/pulls", repo=repo,
            params={"state": state, "per_page": per_page, "sort": "updated", "direction": "desc"},
        ) or []

    @with_retry(max_attempts=3)
    def list_installation_repos(self, *, per_page: int = 100, max_pages: int = 5) -> list[dict]:
        """当前 installation token 可访问的仓库（App 模式；PAT 调用会 403）。"""
        repos: list[dict] = []
        for page in range(1, max_pages + 1):
            data = self._request(
                "GET", "/installation/repositories",
                params={"per_page": per_page, "page": page},
            ) or {}
            batch = data.get("repositories") or []
            repos.extend(batch)
            if len(batch) < per_page:
                break
        return repos

    def update_branch(self, repo: str, number: int, *, expected_head_sha: str) -> dict:
        """merge-queue-lite：把 PR 分支更新到最新 base（GitHub 原生 update-branch）。"""
        return self._request(
            "PUT", f"/repos/{repo}/pulls/{number}/update-branch", repo=repo,
            json={"expected_head_sha": expected_head_sha},
        )

    def merge(self, repo: str, number: int, *, sha: str, method: str, title: str, message: str) -> dict:
        return self._request(
            "PUT", f"/repos/{repo}/pulls/{number}/merge", repo=repo,
            json={
                "sha": sha,
                "merge_method": method,
                "commit_title": title[:250],
                "commit_message": message[:1000],
            },
        )

    def create_pull_request(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        labels: list[str] | None = None,
    ) -> dict:
        """创建关联 PR（用于外部只读 Fork 或 submit_mode=new_pr 时递交修复分支）。"""
        created = self._request(
            "POST",
            f"/repos/{repo}/pulls",
            repo=repo,
            json={
                "title": title[:250],
                "body": body[:60000],
                "head": head,
                "base": base,
            },
        ) or {}
        number = created.get("number")
        if number and labels:
            try:
                self._request(
                    "POST",
                    f"/repos/{repo}/issues/{int(number)}/labels",
                    repo=repo,
                    json={"labels": list(labels)},
                )
            except GitHubError:
                pass
        return created

    def update_ref(self, repo: str, branch: str, *, sha: str, force: bool = False) -> dict:
        return self._request(
            "PATCH", f"/repos/{repo}/git/refs/heads/{branch}", repo=repo,
            json={"sha": sha, "force": force},
        )

    def push_fix(
        self,
        repo: str,
        branch: str,
        *,
        message: str,
        files: dict[str, str],
        expected_head_sha: str,
    ) -> dict:
        """在 PR head 分支上创建一个修复提交（不 force push）。"""
        if not files:
            raise GitHubError("fix files 不能为空")
        ref = self._request("GET", f"/repos/{repo}/git/ref/heads/{branch}", repo=repo)
        current = (ref or {}).get("object", {}).get("sha", "")
        if expected_head_sha and current != expected_head_sha:
            raise GitHubError(
                f"PR head 已变化: expected={expected_head_sha[:8]} current={current[:8]}"
            )
        base_commit = self._request("GET", f"/repos/{repo}/git/commits/{current}", repo=repo)
        base_tree = (base_commit or {}).get("tree", {}).get("sha", "")
        entries = []
        for path, content in files.items():
            blob = self._request(
                "POST", f"/repos/{repo}/git/blobs", repo=repo,
                json={"content": content, "encoding": "utf-8"},
            )
            entries.append({
                "path": path, "mode": "100644", "type": "blob",
                "sha": (blob or {}).get("sha", ""),
            })
        tree = self._request(
            "POST", f"/repos/{repo}/git/trees", repo=repo,
            json={"base_tree": base_tree, "tree": entries},
        )
        commit = self._request(
            "POST", f"/repos/{repo}/git/commits", repo=repo,
            json={
                "message": message[:200],
                "tree": (tree or {}).get("sha", ""),
                "parents": [current],
            },
        )
        new_sha = (commit or {}).get("sha", "")
        self._request(
            "PATCH", f"/repos/{repo}/git/refs/heads/{branch}",
            repo=repo,
            json={"sha": new_sha, "force": False},
        )
        return {"sha": new_sha}


__all__ = ["GitHubClient"]
