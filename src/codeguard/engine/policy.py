"""仓库策略：从 base 分支读取，PR 对策略的修改不生效。

`mode` 区分两种部署形态：
- `internal`：公司内部 PR，默认走 `change` profile，按变更风险分级审批；
- `external`：外部/不可信 PR，保持 bugfix 复现门禁的原有行为。
"""
from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..errors import PolicyError

TierName = Literal["T0", "T1", "T2"]
TIER_NAMES: tuple[str, ...] = ("T0", "T1", "T2")

DEFAULT_PROTECTED_PATHS = [".github/workflows/**", "CODEOWNERS", ".codeguard/**"]
DEFAULT_HIGH_RISK_PATHS = [
    "**/auth/**", "**/security/**", "**/migrations/**", "**/crypto/**",
    "pyproject.toml", "requirements*.txt",
]
DEFAULT_T0_PATHS = ["docs/**", "**/*.md", "**/*.rst", "**/*.txt", "**/*.adoc"]


def path_matches(path: str, pattern: str) -> bool:
    """glob 匹配：支持 `docs/**`、`**/auth/**`、`**/*.md` 与普通文件名。"""
    if not path or not pattern:
        return False
    if fnmatch(path, pattern):
        return True
    if pattern.startswith("**/"):
        return fnmatch(path, pattern[3:])
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    return False


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(path_matches(path, p) for p in patterns)


class Supported(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pr_types: list[str] = Field(default_factory=lambda: ["bugfix"])
    languages: list[str] = Field(default_factory=lambda: ["python"])


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_files: int = 50
    max_changed_lines: int = 3000
    max_diff_bytes: int = 2_000_000


class Gates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # None = 由 mode 决定：internal 不要求复现，external 要求复现
    require_reproduction: bool | None = None
    require_existing_tests_green: bool = True
    reject_confidence: float = 0.85
    merge_mode: Literal["auto", "human"] = "auto"
    merge_method: Literal["squash", "merge", "rebase"] = "squash"
    semantic_block_threshold: float = 0.8


class Profiles(BaseModel):
    """审查 profile：change 面向任意变更，bugfix 面向带复现声明的修复。"""

    model_config = ConfigDict(extra="forbid")
    default: Literal["change", "bugfix"] = "change"
    allow: list[str] = Field(
        default_factory=lambda: ["bugfix", "feature", "refactor", "docs", "chore"]
    )


class TierRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(default_factory=list)
    max_changed_lines: int | None = None
    min_changed_lines: int | None = None


class Tiers(BaseModel):
    """确定性风险分级：由变更本身决定，不由作者决定。"""

    model_config = ConfigDict(extra="forbid")

    rules: dict[str, TierRule] = Field(
        default_factory=lambda: {
            "T0": TierRule(paths=list(DEFAULT_T0_PATHS), max_changed_lines=200),
            "T2": TierRule(
                paths=list(DEFAULT_HIGH_RISK_PATHS) + [
                    ".github/**", "dockerfiles/**", "**/billing/**", "**/payments/**",
                ],
                min_changed_lines=1000,
            ),
        }
    )
    default: TierName = "T1"
    min_human_approvals: dict[str, int] = Field(
        default_factory=lambda: {"T0": 0, "T1": 1, "T2": 2}
    )
    require_non_author_for: list[str] = Field(default_factory=lambda: ["T2"])
    force_human_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PROTECTED_PATHS) + ["**/auth/**", "**/billing/**"]
    )

    @field_validator("rules")
    @classmethod
    def _rule_names(cls, value: dict[str, TierRule]) -> dict[str, TierRule]:
        unknown = set(value) - set(TIER_NAMES)
        if unknown:
            raise ValueError(f"未知 tier: {sorted(unknown)}")
        return value

    @field_validator("min_human_approvals")
    @classmethod
    def _approval_counts(cls, value: dict[str, int]) -> dict[str, int]:
        for name, count in value.items():
            if name not in TIER_NAMES:
                raise ValueError(f"未知 tier: {name}")
            if count < 0 or count > 2:
                raise ValueError(f"{name} 的人工审批数必须在 0..2")
        ordered = [value.get(name, 0) for name in TIER_NAMES]
        if ordered != sorted(ordered):
            raise ValueError("min_human_approvals 必须随风险等级单调不减")
        return value

    @field_validator("require_non_author_for")
    @classmethod
    def _non_author_tiers(cls, value: list[str]) -> list[str]:
        unknown = set(value) - set(TIER_NAMES)
        if unknown:
            raise ValueError(f"未知 tier: {sorted(unknown)}")
        return value


class ReviewSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guidance_path: str = ".codeguard/review.md"
    max_nits: int = Field(default=5, ge=0, le=50)
    depth_by_tier: dict[str, Literal["quick", "standard", "deep"]] = Field(
        default_factory=lambda: {"T0": "quick", "T1": "standard", "T2": "deep"}
    )
    max_findings_verified: int = Field(default=5, ge=0, le=20)

    @field_validator("depth_by_tier")
    @classmethod
    def _depth_tiers(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value) - set(TIER_NAMES)
        if unknown:
            raise ValueError(f"未知 tier: {sorted(unknown)}")
        return value


class MergeQueueSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    update_branch_on_stale: bool = False
    max_attempts: int = Field(default=1, ge=0, le=3)


class MergeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_zero_human: bool = True
    queue: MergeQueueSettings = Field(default_factory=MergeQueueSettings)


class SandboxCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    command: str
    required: bool = False


class SandboxPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: str = "codeguard-sandbox-python:3.12"
    test_commands: list[str] = Field(default_factory=lambda: ["python -m pytest -q"])
    test_command_template: str = "python -m pytest -q {tests}"
    timeout_sec: int = 300
    prepare_commands: list[str] = Field(default_factory=list)
    checks: list[SandboxCheck] = Field(default_factory=list)
    prepare_network: Literal["none", "bridge"] = "none"


CapabilityName = Literal["none", "read", "exec", "write"]

DEFAULT_AGENT_COMMANDS = [
    "python -m pytest",
    "python -m ruff",
    "python -m mypy",
    "python -m compileall",
    "pytest",
    "ruff",
    "mypy",
]


class AgentRoleTools(BaseModel):
    """每个 Agent 的工具能力档位；实际生效值还会被 tier/mode/env 继续收敛。"""

    model_config = ConfigDict(extra="forbid")

    pr_reader: CapabilityName = "read"
    review_logic: CapabilityName = "read"
    review_security: CapabilityName = "read"
    review_architecture: CapabilityName = "read"
    review_tests: CapabilityName = "read"
    coder: CapabilityName = "write"
    qa_checker: CapabilityName = "read"
    # 兼容保留旧角色配置字段
    semantic_detector: CapabilityName = "read"
    finding_verifier: CapabilityName = "read"
    issue_verifier: CapabilityName = "read"
    reviewer: CapabilityName = "exec"
    fixer: CapabilityName = "write"


class AgentsToolsSettings(BaseModel):
    """Agent 工具总开关与单次循环限额。默认开启，只能被收紧。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    protocol: Literal["auto", "native", "json"] = "auto"
    max_steps: int = Field(default=6, ge=1, le=12)
    max_tool_calls: int = Field(default=6, ge=0, le=12)
    max_wall_sec: int = Field(default=120, ge=10, le=900)
    max_output_chars: int = Field(default=8000, ge=500, le=40000)
    sandbox_timeout_sec: int = Field(default=120, ge=5, le=300)
    allow_commands: list[str] = Field(default_factory=lambda: list(DEFAULT_AGENT_COMMANDS))
    external_max_capability: CapabilityName = "read"
    roles: AgentRoleTools = Field(default_factory=AgentRoleTools)

    @field_validator("allow_commands")
    @classmethod
    def _commands(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            text = (item or "").strip()
            if not text:
                raise ValueError("allow_commands 不能包含空项")
            if text not in cleaned:
                cleaned.append(text)
        return cleaned


McpTransportType = Literal["stdio", "streamable_http", "sse"]
ToolProtocolMode = Literal["auto", "native", "json"]


class McpServerPolicy(BaseModel):
    """单个外部/本地 MCP Server 连接配置。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=48)
    enabled: bool = True
    transport: McpTransportType = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    env_vars: list[str] = Field(default_factory=list)
    timeout_sec: int = Field(default=15, ge=1, le=120)
    max_output_chars: int = Field(default=8000, ge=500, le=32000)
    capability: CapabilityName = "read"
    allowed_roles: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    blocked_tools: list[str] = Field(default_factory=list)


def default_mcp_servers() -> list[McpServerPolicy]:
    """内置默认启用的 4 个 CodeGuard 业务 MCP Server。"""
    return [
        McpServerPolicy(
            name="osv_security",
            enabled=True,
            transport="stdio",
            command="python",
            args=["-m", "codeguard.mcp.servers.osv_server"],
            timeout_sec=15,
            capability="read",
            allowed_roles=["review_security", "semantic_detector", "reviewer"],
        ),
        McpServerPolicy(
            name="git_history",
            enabled=True,
            transport="stdio",
            command="python",
            args=["-m", "codeguard.mcp.servers.git_server"],
            timeout_sec=15,
            capability="read",
            allowed_roles=[
                "pr_reader",
                "review_logic",
                "review_security",
                "review_architecture",
                "review_tests",
                "coder",
                "semantic_detector",
                "finding_verifier",
                "reviewer",
                "fixer",
            ],
        ),
        McpServerPolicy(
            name="knowledge",
            enabled=True,
            transport="stdio",
            command="python",
            args=["-m", "codeguard.mcp.servers.knowledge_server"],
            timeout_sec=15,
            capability="read",
            allowed_roles=[
                "review_security",
                "review_architecture",
                "coder",
                "semantic_detector",
                "finding_verifier",
                "reviewer",
                "fixer",
            ],
        ),
        McpServerPolicy(
            name="github_context",
            enabled=True,
            transport="stdio",
            command="python",
            args=["-m", "codeguard.mcp.servers.github_server"],
            env_vars=["GITHUB_TOKEN", "GH_TOKEN"],
            timeout_sec=15,
            capability="read",
            allowed_roles=["pr_reader", "issue_verifier", "reviewer"],
        ),
    ]


class AgentsMcpSettings(BaseModel):
    """MCP Client 接入治理策略。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_capability: CapabilityName = "read"
    external_max_capability: CapabilityName = "read"
    tool_name_prefix: bool = True
    servers: list[McpServerPolicy] = Field(default_factory=default_mcp_servers)


class AgentsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools: AgentsToolsSettings = Field(default_factory=AgentsToolsSettings)
    mcp: AgentsMcpSettings = Field(default_factory=AgentsMcpSettings)


class RuleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disable: list[str] = Field(default_factory=list)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    mode: Literal["internal", "external"] = "internal"
    supported: Supported = Field(default_factory=Supported)
    profiles: Profiles = Field(default_factory=Profiles)
    limits: Limits = Field(default_factory=Limits)
    gates: Gates = Field(default_factory=Gates)
    tiers: Tiers = Field(default_factory=Tiers)
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    merge: MergeSettings = Field(default_factory=MergeSettings)
    trusted_authors: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_PROTECTED_PATHS))
    high_risk_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_HIGH_RISK_PATHS))
    sandbox: SandboxPolicy = Field(default_factory=SandboxPolicy)
    agents: AgentsSettings = Field(default_factory=AgentsSettings)
    rules: RuleSettings = Field(default_factory=RuleSettings)

    @field_validator("protected_paths", "high_risk_paths", "trusted_authors")
    @classmethod
    def _patterns(cls, values: list[str]) -> list[str]:
        for pattern in values:
            if not pattern:
                raise ValueError("模式/作者项不能为空")
            if pattern.startswith("/") or ".." in pattern.split("/"):
                raise ValueError(f"非法模式: {pattern!r}")
        return values

    # ── 判定辅助 ──

    @property
    def require_reproduction(self) -> bool:
        if self.gates.require_reproduction is None:
            return self.mode == "external"
        return self.gates.require_reproduction

    def is_protected(self, path: str) -> bool:
        return matches_any(path, self.protected_paths)

    def is_high_risk(self, path: str) -> bool:
        return matches_any(path, self.high_risk_paths)

    def is_force_human(self, path: str) -> bool:
        return matches_any(path, self.tiers.force_human_paths)

    def disabled(self, rule_id: str) -> bool:
        return rule_id in self.rules.disable

    def min_approvals(self, tier: str) -> int:
        """生效的人工审批数：未显式开启零人工时，任何等级至少 1 人。"""
        count = int(self.tiers.min_human_approvals.get(tier, 1))
        if count == 0 and not self.merge.allow_zero_human:
            return 1
        return count

    def depth_for(self, tier: str) -> str:
        return self.review.depth_by_tier.get(tier, "standard")


def default_policy() -> Policy:
    return Policy()


def load_policy(base_root: Path | str | None, policy_path: str = ".codeguard/policy.yml") -> Policy:
    """从 base 树读取策略；不存在时返回默认策略。非法策略 fail closed。"""
    if not base_root:
        return default_policy()
    root = Path(base_root)
    path = root / policy_path
    if not path.is_file():
        return default_policy()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"读取策略失败: {exc}") from exc
    try:
        return Policy.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - 统一转策略错误
        raise PolicyError(f"策略文件非法: {exc}") from exc


def load_policy_text(text: str) -> Policy:
    try:
        raw = yaml.safe_load(text) or {}
        return Policy.model_validate(raw)
    except PolicyError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PolicyError(f"策略文件非法: {exc}") from exc


__all__ = [
    "Supported", "Limits", "Gates", "Profiles", "TierRule", "Tiers",
    "ReviewSettings", "MergeQueueSettings", "MergeSettings", "SandboxCheck",
    "SandboxPolicy", "RuleSettings", "Policy", "TierName", "TIER_NAMES",
    "CapabilityName", "AgentRoleTools", "AgentsToolsSettings", "AgentsSettings",
    "McpTransportType", "ToolProtocolMode", "McpServerPolicy", "AgentsMcpSettings",
    "default_mcp_servers", "DEFAULT_AGENT_COMMANDS",
    "path_matches", "matches_any", "default_policy", "load_policy", "load_policy_text",
]
