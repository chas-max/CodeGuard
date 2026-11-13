"""本地 GitHub 记录器：让流水线在不关联 GitHub 的前提下走到终态。

用途只有伪 PR 模拟：`merge` / `push_fix` / `comment` 等写操作只记录不发送，
读操作返回最小可用数据。**它不发任何网络请求，也不持有凭据**。

注意：这不是「测试替身」，而是「离线运行时的本地实现」；`tests/fakes.py`
的 `FakeGitHub` 直接复用它，避免两套行为漂移。
"""
from __future__ import annotations

LOCAL_MERGE_SHA = "1" * 40
LOCAL_PUSH_SHA = "c" * 40


class LocalGitHub:
    def __init__(self, merge_sha: str = LOCAL_MERGE_SHA):
        self.merge_sha = merge_sha
        self.comments: list[tuple[str, int, str]] = []
        self.merged: list[dict] = []
        self.pushed: list[dict] = []
        self.review_comments: list[dict] = []
        self.reviews: list[dict] = []
        self.statuses: list[dict] = []
        self.updated_branches: list[dict] = []
        self.branch_sha = ""
        self.ci_state = "success"
        self.pulls: list[dict] = []
        self.repos: list[dict] = []
        self._next_comment_id = 0

    # ── 读 ──

    def get_user(self) -> dict:
        return {"login": "local-runner"}

    def list_repos(self, **kwargs) -> list[dict]:  # noqa: ARG002
        return list(self.repos)

    def list_installation_repos(self, **kwargs) -> list[dict]:  # noqa: ARG002
        return list(self.repos)

    def list_review_comments(self, repo: str, number: int) -> list[dict]:  # noqa: ARG002
        return list(self.review_comments)

    def get_branch(self, repo: str, branch: str) -> dict:  # noqa: ARG002
        return {"commit": {"sha": self.branch_sha}}

    def get_combined_status(self, repo: str, ref: str) -> dict:  # noqa: ARG002
        return {"state": self.ci_state, "sha": ref}

    def list_pulls(self, repo: str, *, state: str = "closed", per_page: int = 50) -> list[dict]:  # noqa: ARG002
        return list(self.pulls)

    # ── 写（仅记录） ──

    def comment(self, repo: str, number: int, body: str) -> dict:
        self.comments.append((repo, number, body))
        return {"id": len(self.comments)}

    def submit_review(self, repo: str, number: int, *, event: str, body: str) -> dict:
        self.reviews.append({"repo": repo, "number": number, "event": event, "body": body})
        return {"id": len(self.reviews)}

    def create_review_comment(self, repo: str, number: int, *, body: str, commit_id: str,
                              path: str, line: int, side: str = "RIGHT") -> dict:  # noqa: ARG002
        self._next_comment_id += 1
        item = {"id": self._next_comment_id, "body": body, "commit_id": commit_id,
                "path": path, "line": line, "side": side}
        self.review_comments.append(item)
        return item

    def update_review_comment(self, repo: str, comment_id: int, *, body: str) -> dict:  # noqa: ARG002
        for item in self.review_comments:
            if item["id"] == comment_id:
                item["body"] = body
                return item
        raise RuntimeError(f"review comment 不存在: {comment_id}")

    def create_status(self, repo: str, sha: str, *, state: str, context: str, description: str) -> dict:  # noqa: ARG002
        self.statuses.append({"sha": sha, "state": state, "context": context})
        return {"id": len(self.statuses)}

    def update_branch(self, repo: str, number: int, *, expected_head_sha: str) -> dict:  # noqa: ARG002
        self.updated_branches.append({"number": number, "expected": expected_head_sha})
        return {"message": "Updating pull request branch."}

    def push_fix(self, repo: str, branch: str, *, message: str, files: dict[str, str],
                 expected_head_sha: str) -> dict:  # noqa: ARG002
        self.pushed.append({"repo": repo, "branch": branch, "files": files,
                            "expected": expected_head_sha})
        return {"sha": LOCAL_PUSH_SHA}

    def merge(self, repo: str, number: int, *, sha: str, method: str, title: str, message: str) -> dict:  # noqa: ARG002
        self.merged.append({"repo": repo, "number": number, "sha": sha, "method": method})
        return {"sha": self.merge_sha, "merged": True}

    def close(self):
        pass


__all__ = ["LocalGitHub", "LOCAL_MERGE_SHA", "LOCAL_PUSH_SHA"]
