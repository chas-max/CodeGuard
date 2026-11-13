"""GitHub webhook 验签与 pull_request 事件解析。"""
from __future__ import annotations

import hashlib
import hmac

from ..errors import GitHubError
from ..models import PullRequestContext

SUPPORTED_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


def verify_signature(secret: str, body: bytes | str, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    if isinstance(body, str):
        body = body.encode("utf-8")
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def is_supported_action(payload: dict) -> bool:
    return payload.get("action") in SUPPORTED_ACTIONS


def parse_pull_request_event(payload: dict) -> PullRequestContext:
    """从 pull_request webhook payload 提取最小 PR 上下文。"""
    try:
        pr = payload["pull_request"]
        repo = payload["repository"]
        base = pr["base"]
        head = pr["head"]
        user = pr.get("user") or {}
        ctx = PullRequestContext(
            repo=repo["full_name"],
            number=int(pr["number"]),
            base_ref=base["ref"],
            base_sha=base.get("sha", ""),
            head_ref=head["ref"],
            head_sha=head.get("sha", ""),
            title=pr.get("title", ""),
            body=pr.get("body") or "",
            author=user.get("login", ""),
            is_fork=bool(head.get("repo") and head["repo"].get("full_name") != repo["full_name"]),
            is_draft=bool(pr.get("draft")),
            mergeable=bool(pr.get("mergeable", True)),
            additions=int(pr.get("additions", 0)),
            deletions=int(pr.get("deletions", 0)),
            changed_file_count=int(pr.get("changed_files", 0)),
            default_branch=repo.get("default_branch", "main"),
            html_url=pr.get("html_url", ""),
        )
        return ctx
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubError(f"pull_request payload 非法: {exc}") from exc


__all__ = ["SUPPORTED_ACTIONS", "verify_signature", "is_supported_action", "parse_pull_request_event"]
