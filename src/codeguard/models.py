"""CodeGuard 领域模型：PR 审查、混合检测、问题验证、修复与合并。

设计原则：
- 所有跨节点数据都有 Pydantic 模型，节点边界做校验，state 中存 JSON 兼容 dict。
- 确定性检测与 LLM 语义检测使用同一 Finding 结构，但语义 finding 额外要求证据链。
- 终态、合并、审查结论之间有显式一致性校验，避免"无证据合并/拒绝"。
"""
from __future__ import annotations

import hashlib
import re
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


# ────────────────────────────── 枚举 ──────────────────────────────


class Severity(str, Enum):
    critical = "critical"
    major = "major"
    minor = "minor"
    info = "info"


class FindingSource(str, Enum):
    deterministic = "deterministic"
    semantic = "semantic"


class FindingCategory(str, Enum):
    syntax = "syntax"
    security = "security"
    policy = "policy"
    tests = "tests"
    api_contract = "api_contract"
    cross_file = "cross_file"
    data_flow = "data_flow"
    concurrency = "concurrency"
    architecture = "architecture"
    design = "design"
    correctness = "correctness"
    completeness = "completeness"
    style = "style"
    dependency = "dependency"
    ci = "ci"
    scope = "scope"
    other = "other"


class IssueStatus(str, Enum):
    confirmed = "CONFIRMED"
    not_reproduced = "NOT_REPRODUCED"
    insufficient_evidence = "INSUFFICIENT_EVIDENCE"
    out_of_scope = "OUT_OF_SCOPE"


class ReviewVerdict(str, Enum):
    approve = "approve"
    request_changes = "request_changes"


class Outcome(str, Enum):
    merge_ready = "MERGE_READY"  # 门控通过，等待 merge 节点执行
    merged = "MERGED"
    rejected_no_issue = "REJECTED_NO_ISSUE"
    rejected_out_of_scope = "REJECTED_OUT_OF_SCOPE"
    changes_requested = "CHANGES_REQUESTED"
    blocked_human = "BLOCKED_HUMAN"
    infra_failed = "INFRA_FAILED"
    stale = "STALE"
    failed = "FAILED"


class SandboxStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    infra_error = "infra_error"


class MergeMode(str, Enum):
    auto = "auto"
    human = "human"


class FindingLifecycle(str, Enum):
    open = "open"
    resolved = "resolved"
    accepted_risk = "accepted_risk"
    false_positive = "false_positive"


class ReviewProfile(str, Enum):
    change = "change"
    bugfix = "bugfix"


class IntentStatus(str, Enum):
    sufficient = "SUFFICIENT"
    unclear = "UNCLEAR"
    infra_failed = "INFRA_FAILED"


class Tier(str, Enum):
    T0 = "T0"
    T1 = "T1"
    T2 = "T2"


# ────────────────────────────── 工具 ──────────────────────────────


def validate_repo_path(path: str) -> str:
    """规范化仓库相对路径；拒绝绝对路径、盘符与 `..` 穿越。"""
    raw = (path or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("路径不能为空")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ValueError(f"不允许绝对路径: {path!r}")
    parts = PurePosixPath(raw).parts
    if any(part == ".." for part in parts):
        raise ValueError(f"路径不得包含 '..': {path!r}")
    return "/".join(p for p in parts if p not in ("", "."))


def _stable_id(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ────────────────────────────── PR 输入 ──────────────────────────────


class FileChange(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    status: str = "modified"  # added/modified/removed/renamed
    additions: int = 0
    deletions: int = 0
    patch: str = ""

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return validate_repo_path(value)


class PullRequestContext(BaseModel):
    """一次 PR 审查任务的最小输入。"""

    model_config = ConfigDict(extra="ignore")

    repo: str
    number: int = Field(gt=0)
    base_ref: str = "main"
    base_sha: str = ""
    head_ref: str = "feature"
    head_sha: str = ""
    title: str = ""
    body: str = ""
    author: str = ""
    is_fork: bool = False
    is_draft: bool = False
    mergeable: bool = True
    changed_files: list[FileChange] = Field(default_factory=list)
    changed_file_count: int = 0
    additions: int = 0
    deletions: int = 0
    default_branch: str = "main"
    html_url: str = ""
    labels: list[str] = Field(default_factory=list)
    created_at: str = ""
    profile: str = ""  # 空 = 由策略 profiles.default 决定

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, value: str) -> str:
        if not _REPO_RE.match(value or ""):
            raise ValueError(f"仓库名必须为 owner/name: {value!r}")
        return value

    @field_validator("base_sha", "head_sha")
    @classmethod
    def _check_sha(cls, value: str) -> str:
        if value and not _SHA_RE.match(value):
            raise ValueError(f"非法 SHA: {value!r}")
        return value

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        return (value or "")[:500]

    @field_validator("body")
    @classmethod
    def _check_body(cls, value: str) -> str:
        return (value or "")[:20000]


# ────────────────────────────── 检测结果 ──────────────────────────────


class Finding(BaseModel):
    """确定性或语义检测发现的单条问题。"""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    source: FindingSource = FindingSource.deterministic
    rule_id: str = ""
    category: FindingCategory = FindingCategory.other
    severity: Severity = Severity.minor
    blocking: bool = False
    auto_fixable: bool = False
    path: str = ""
    line: int = 0
    message: str = ""
    evidence: str = ""
    suggestion: str = ""
    lifecycle: FindingLifecycle = FindingLifecycle.open
    introduced_by_pr: bool = False
    demoted_by: str = ""
    group: str = ""
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    reported_by: list[str] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return validate_repo_path(value) if value else ""

    @model_validator(mode="after")
    def _require_evidence_for_blocking(self):
        if self.blocking and not self.evidence.strip():
            raise ValueError(f"blocking finding 必须提供 evidence: {self.rule_id or self.id}")
        return self

    def model_post_init(self, __context: Any) -> None:
        if not self.id:
            object.__setattr__(
                self, "id",
                _stable_id(self.source.value, self.rule_id, self.path, self.line, self.message),
            )


class SemanticFinding(Finding):
    """LLM 跨文件/整体设计检测问题；必须有可 grounding 的证据。"""

    source: FindingSource = FindingSource.semantic
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    paths: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    call_chain: list[str] = Field(default_factory=list)
    recommendation: str = ""

    @field_validator("paths")
    @classmethod
    def _check_paths(cls, values: list[str]) -> list[str]:
        return [validate_repo_path(v) for v in values]

    @model_validator(mode="after")
    def _require_grounding(self):
        if not self.paths:
            raise ValueError("semantic finding 必须至少引用一个真实路径")
        if not self.evidence.strip():
            raise ValueError("semantic finding 必须提供 evidence")
        if len(self.paths) > 1 and not self.call_chain:
            raise ValueError("跨文件 semantic finding 必须提供 call_chain")
        return self

    def model_post_init(self, __context: Any) -> None:
        if not self.id:
            object.__setattr__(
                self, "id",
                _stable_id(
                    self.category.value, "|".join(sorted(self.paths)),
                    "|".join(sorted(self.symbols)), self.message,
                ),
            )


class DeterministicReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    findings: list[Finding] = Field(default_factory=list)
    tree_hash: str = ""
    scanned_files: int = 0
    notes: list[str] = Field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]


class SemanticReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    findings: list[SemanticFinding] = Field(default_factory=list)
    scanned_scope: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def blocking(self) -> list[SemanticFinding]:
        return [f for f in self.findings if f.blocking]


class HybridScanReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    deterministic: DeterministicReport = Field(default_factory=DeterministicReport)
    semantic: SemanticReport = Field(default_factory=SemanticReport)
    findings: list[Finding] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    tree_hash: str = ""

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]

    @classmethod
    def combine(
        cls,
        deterministic: DeterministicReport,
        semantic: SemanticReport,
        *,
        tree_hash: str = "",
        semantic_block_threshold: float = 0.8,
    ) -> "HybridScanReport":
        merged: dict[str, Finding] = {f.id: f for f in deterministic.findings}
        conflicts: list[str] = []
        for sf in semantic.findings:
            # 语义检测不能推翻确定性 blocking；同一问题已有确定性 finding 时保留确定性版本。
            if sf.id in merged:
                conflicts.append(f"{sf.id}: semantic finding 与 deterministic finding 重复，保留确定性版本")
                continue
            if sf.confidence >= semantic_block_threshold and sf.severity in (Severity.critical, Severity.major):
                sf = sf.model_copy(update={"blocking": True})
            elif sf.confidence < semantic_block_threshold:
                sf = sf.model_copy(update={"blocking": False})
            merged[sf.id] = sf
        return cls(
            deterministic=deterministic,
            semantic=semantic,
            findings=sorted(merged.values(), key=lambda f: f.id),
            conflicts=conflicts,
            tree_hash=tree_hash or deterministic.tree_hash,
        )


# ────────────────────────────── 问题验证 ──────────────────────────────


class IssueClaim(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["bugfix", "feature", "refactor", "docs", "chore", "unknown"] = "unknown"
    statement: str = ""
    expected: str = ""
    actual: str = ""
    repro_steps: list[str] = Field(default_factory=list)
    affected_paths: list[str] = Field(default_factory=list)
    linked_issue: int | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("affected_paths")
    @classmethod
    def _check_paths(cls, values: list[str]) -> list[str]:
        return [validate_repo_path(v) for v in values]

    @model_validator(mode="after")
    def _bugfix_needs_claim(self):
        if self.kind == "bugfix":
            filled = sum(bool(x.strip()) for x in (self.statement, self.expected, self.actual))
            if filled < 2:
                raise ValueError("bugfix claim 至少需要 statement/expected/actual 中两项")
        return self


class ReproEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    test_source: str = "pr"  # pr/generated/existing
    test_file: str = ""
    command: str = ""
    base_exit_code: int | None = None
    head_exit_code: int | None = None
    base_failed: bool = False
    head_passed: bool = False
    log: str = ""


class IssueVerification(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: IssueStatus = IssueStatus.insufficient_evidence
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    evidence: list[ReproEvidence] = Field(default_factory=list)
    test_source: str = ""

    @model_validator(mode="after")
    def _require_evidence(self):
        if self.status == IssueStatus.confirmed:
            # 问题存在只需要“base 上能复现失败”；head 是否修好由 validate 阶段判断。
            if not any(e.base_failed for e in self.evidence):
                raise ValueError("CONFIRMED 需要 base 失败 的复现证据")
        if self.status == IssueStatus.not_reproduced:
            if not any(e.base_exit_code == 0 for e in self.evidence):
                raise ValueError("NOT_REPRODUCED 需要至少一条 base 通过 的证据")
            if self.confidence < 0.5:
                raise ValueError("NOT_REPRODUCED 置信度过低，应使用 INSUFFICIENT_EVIDENCE")
        return self


# ────────────────────────────── 验证 / 审查 / 修复 / 合并 ──────────────────────────────


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: SandboxStatus = SandboxStatus.infra_error
    commands: list[str] = Field(default_factory=list)
    repro_passed: bool = False
    regression_passed: bool = False
    new_failures: list[str] = Field(default_factory=list)
    duration_sec: float = 0.0
    logs: str = ""
    tree_hash: str = ""
    error: str = ""
    checks: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def _passed_means_both(self):
        if self.status == SandboxStatus.passed and not (self.repro_passed and self.regression_passed):
            raise ValueError("ValidationReport=passed 需要 repro_passed 与 regression_passed 同时为真")
        return self


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: ReviewVerdict = ReviewVerdict.request_changes
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    fix_instructions: list[str] = Field(default_factory=list)
    residual_risks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _approve_no_blocking(self):
        if self.verdict == ReviewVerdict.approve:
            bad = [
                f for f in self.findings
                if f.blocking and f.severity in (Severity.critical, Severity.major)
            ]
            if bad:
                raise ValueError("reviewer approve 时不得存在 critical/major blocking finding")
        return self


class RiskTier(BaseModel):
    """确定性风险分级结果：决定审批人数与审查深度。"""

    model_config = ConfigDict(extra="ignore")

    tier: Tier = Tier.T1
    reasons: list[str] = Field(default_factory=list)
    min_human_approvals: int = Field(default=1, ge=0, le=2)
    require_non_author: bool = False
    depth: str = "standard"
    trusted_author: bool = False
    agent_authored: bool = False


class Approval(BaseModel):
    """一次人工审批：绑定具体 commit 与工作区树哈希。"""

    model_config = ConfigDict(extra="ignore")

    approver: str = ""
    sha: str = ""
    tree_hash: str = ""
    at: str = ""
    finding_outcomes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _needs_approver(self):
        if not self.approver.strip():
            raise ValueError("审批必须记录 approver")
        return self


class IntentVerification(BaseModel):
    """change profile 的意图/证据判定：advisory，不阻断合并。"""

    model_config = ConfigDict(extra="ignore")

    status: IntentStatus = IntentStatus.unclear
    claimed_kind: str = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)


class FixProposal(BaseModel):
    model_config = ConfigDict(extra="ignore")

    patch: str = ""
    files: list[str] = Field(default_factory=list)
    explanation: str = ""
    round: int = 1
    applies_cleanly: bool = False
    tree_hash: str = ""

    @field_validator("files")
    @classmethod
    def _check_files(cls, values: list[str]) -> list[str]:
        return [validate_repo_path(v) for v in values]


class MergeDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    outcome: Outcome = Outcome.blocked_human
    reason: str = ""
    merge_commit_sha: str = ""
    requires_human: bool = False
    actions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _merged_needs_commit(self):
        if self.outcome == Outcome.merged and not self.merge_commit_sha:
            raise ValueError("MERGED 必须提供 merge_commit_sha")
        return self


from .models_v2 import (
    IMPORTANCE_APPROVALS,
    IMPORTANCE_RANK,
    IMPORTANCE_TO_TIER,
    REVIEWER_LABELS,
    REVIEWER_ROLES,
    TIER_TO_IMPORTANCE,
    CodePartition,
    FixDirective,
    FixExecutionResult,
    ImportanceLevel,
    PerspectiveReviewOutput,
    PRBrief,
    ProceduralMemoryRecord,
    QACheckReport,
    QAItemCheck,
    ReviewerMemory,
    ReviewerRole,
    ReviewSummary,
    max_importance,
    normalize_importance,
    stable_content_hash,
)

__all__ = [
    "Severity", "FindingSource", "FindingCategory", "IssueStatus", "ReviewVerdict",
    "Outcome", "SandboxStatus", "MergeMode", "FindingLifecycle", "ReviewProfile",
    "IntentStatus", "Tier", "FileChange", "PullRequestContext",
    "Finding", "SemanticFinding", "DeterministicReport", "SemanticReport",
    "HybridScanReport", "IssueClaim", "ReproEvidence", "IssueVerification",
    "ValidationReport", "ReviewDecision", "RiskTier", "Approval", "IntentVerification",
    "FixProposal", "MergeDecision", "validate_repo_path",
    "ImportanceLevel", "ReviewerRole", "REVIEWER_ROLES", "REVIEWER_LABELS",
    "IMPORTANCE_RANK", "IMPORTANCE_TO_TIER", "TIER_TO_IMPORTANCE", "IMPORTANCE_APPROVALS",
    "normalize_importance", "max_importance", "stable_content_hash",
    "CodePartition", "PRBrief", "ReviewerMemory", "PerspectiveReviewOutput",
    "FixDirective", "FixExecutionResult", "QAItemCheck", "QACheckReport",
    "ReviewSummary", "ProceduralMemoryRecord",
]
