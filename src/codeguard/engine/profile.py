"""审查 profile 解析：显式指定 > label > 策略默认 > mode 兜底。"""
from __future__ import annotations

from .policy import Policy

VALID_PROFILES = ("change", "bugfix")
BUGFIX_LABELS = {"bugfix", "codeguard:bugfix", "type:bug"}


def resolve_profile(policy: Policy, *, explicit: str = "", labels: list[str] | None = None) -> str:
    """返回 `change` 或 `bugfix`。

    - 显式 profile（API/CLI）优先；
    - label 明确标注 bugfix 时走复现门禁；
    - 否则用策略默认；external 模式下默认必须可复现，故兜底为 bugfix。
    """
    if explicit in VALID_PROFILES:
        return explicit
    normalized = {str(label).strip().lower() for label in (labels or [])}
    if normalized & BUGFIX_LABELS:
        return "bugfix"
    if policy.mode == "external":
        # 外部/不可信 PR 必须能复现问题，不接受 change profile。
        return "bugfix"
    if policy.profiles.default in VALID_PROFILES:
        return policy.profiles.default
    return "change"


__all__ = ["VALID_PROFILES", "BUGFIX_LABELS", "resolve_profile"]
