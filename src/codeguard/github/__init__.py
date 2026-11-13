"""GitHub 集成：REST 客户端、App 鉴权、webhook 验签、合并门控。"""

from .app_auth import AppCredentials, CredentialStore, GitHubAppAuth
from .client import GitHubClient
from .merge import merge_blockers
from .webhook import parse_pull_request_event, verify_signature

__all__ = [
    "AppCredentials", "CredentialStore", "GitHubAppAuth", "GitHubClient",
    "merge_blockers", "parse_pull_request_event", "verify_signature",
]
