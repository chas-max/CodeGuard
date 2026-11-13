from __future__ import annotations

import hashlib
import hmac


from codeguard.github.webhook import (
    is_supported_action,
    parse_pull_request_event,
    verify_signature,
)


def test_signature_verification():
    body = b'{"a":1}'
    secret = "x" * 32
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, sig)
    assert not verify_signature(secret, body, "sha256=bad")


def test_parse_pr_event():
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/repo", "default_branch": "main"},
        "pull_request": {
            "number": 3, "title": "fix", "body": "bug",
            "user": {"login": "dev"},
            "base": {"ref": "main", "sha": "a" * 40},
            "head": {"ref": "fix", "sha": "b" * 40, "repo": {"full_name": "acme/repo"}},
            "draft": False, "mergeable": True, "additions": 1, "deletions": 1,
            "changed_files": 1,
        },
    }
    ctx = parse_pull_request_event(payload)
    assert ctx.repo == "acme/repo" and ctx.number == 3
    assert not ctx.is_fork
    assert is_supported_action(payload)
    payload["action"] = "closed"
    assert not is_supported_action(payload)
