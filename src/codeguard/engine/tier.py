"""确定性风险分级：由变更本身决定等级，作者身份只能把等级抬高、不能降低。

规则顺序（全部确定性，无 LLM）：
1. 基线 = 策略 `tiers.default`；
2. 全部命中 T0 路径且改动量在 T0 上限内、且不涉及依赖/CI/受保护路径 → T0；
3. 命中 T2 路径或改动量达到 T2 下限 → T2；
4. 涉及受保护/CI/依赖路径、公共 API 破坏 → 至少 T1；
5. 作者不可信或由 agent 创作 → 至少 T1（只升不降）；
6. `force_human_paths` 命中 → 至少 T1 且强制一名人工。
"""
from __future__ import annotations

from ..models import RiskTier, Tier
from .policy import Policy, matches_any
from .rules import CI_PATTERNS, DEPENDENCY_PATTERNS

AGENT_MARKERS = (
    "co-authored-by: claude",
    "generated with claude code",
    "co-authored-by: copilot",
    "co-authored-by: codex",
    "generated with codex",
    "co-authored-by: cursor",
    "co-authored-by: deepseek",
)

AGENT_LABELS = {"agent", "ai-generated", "codeguard:agent", "bot"}

_ORDER = {"T0": 0, "T1": 1, "T2": 2}


def detect_agent_authorship(pr_body: str, labels: list[str] | None = None) -> bool:
    """从 PR 正文的 agent trailer 或 label 判断是否 agent 创作。"""
    body = (pr_body or "").lower()
    if any(marker in body for marker in AGENT_MARKERS):
        return True
    return any(str(label).strip().lower() in AGENT_LABELS for label in (labels or []))


def is_trusted_author(author: str, policy: Policy) -> bool:
    """可信作者名单只支持具体 login。

    `team:org/team` 需要 GitHub team API，当前未实现，因此按不可信处理——
    宁可按更高等级审批，也不误用轻量路径。
    """
    if not author:
        return False
    entries = {
        entry.strip().lstrip("@").lower()
        for entry in policy.trusted_authors
        if not entry.strip().lower().startswith("team:")
    }
    return author.strip().lstrip("@").lower() in entries


def classify_tier(
    policy: Policy,
    *,
    changed_paths: list[str],
    changed_lines: int,
    author: str = "",
    pr_body: str = "",
    labels: list[str] | None = None,
    api_break: bool = False,
) -> RiskTier:
    paths = sorted({p for p in changed_paths if p})
    reasons: list[str] = []
    tier = policy.tiers.default

    t0 = policy.tiers.rules.get("T0")
    if t0 and paths:
        allowed = bool(t0.paths) and all(matches_any(p, t0.paths) for p in paths)
        within = t0.max_changed_lines is None or changed_lines <= t0.max_changed_lines
        clean = not any(
            matches_any(p, DEPENDENCY_PATTERNS) or matches_any(p, CI_PATTERNS) or policy.is_protected(p)
            for p in paths
        )
        if allowed and within and clean and tier != "T2":
            tier = "T0"
            reasons.append("全部变更命中 T0 路径且改动量在阈值内")

    t2 = policy.tiers.rules.get("T2")
    if t2:
        hit_paths = [p for p in paths if matches_any(p, t2.paths)]
        if hit_paths:
            tier = "T2"
            reasons.append("命中 T2 路径: " + ", ".join(hit_paths[:5]))
        elif t2.min_changed_lines is not None and changed_lines >= t2.min_changed_lines:
            tier = "T2"
            reasons.append(f"改动行数 {changed_lines} 达到 T2 阈值 {t2.min_changed_lines}")

    risky = [
        p for p in paths
        if policy.is_protected(p) or matches_any(p, CI_PATTERNS) or matches_any(p, DEPENDENCY_PATTERNS)
    ]
    if risky and _ORDER[tier] < _ORDER["T1"]:
        tier = "T1"
        reasons.append("涉及受保护/CI/依赖路径: " + ", ".join(risky[:5]))

    if api_break and _ORDER[tier] < _ORDER["T1"]:
        tier = "T1"
        reasons.append("检测到公共 API 契约变更")

    forced = [p for p in paths if policy.is_force_human(p)]
    if forced and _ORDER[tier] < _ORDER["T1"]:
        tier = "T1"
        reasons.append("命中强制人工路径: " + ", ".join(forced[:5]))

    agent_authored = detect_agent_authorship(pr_body, labels)
    trusted = is_trusted_author(author, policy)
    require_trusted = policy.mode == "external" or bool(policy.trusted_authors)
    if (agent_authored or (require_trusted and not trusted)) and _ORDER[tier] < _ORDER["T1"]:
        tier = "T1"
        reasons.append("agent 创作" if agent_authored else f"作者不在可信名单: {author or '(空)'}")

    minimum = policy.min_approvals(tier)
    if not minimum:
        reasons.append("该等级允许零人工（allow_zero_human 已开启）")

    return RiskTier(
        tier=Tier(tier),
        reasons=reasons,
        min_human_approvals=minimum,
        require_non_author=tier in policy.tiers.require_non_author_for,
        depth=policy.depth_for(tier),
        trusted_author=trusted,
        agent_authored=agent_authored,
    )


__all__ = [
    "AGENT_MARKERS", "AGENT_LABELS", "detect_agent_authorship", "is_trusted_author",
    "classify_tier",
]
