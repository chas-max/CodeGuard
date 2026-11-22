"""CodeGuard v2 架构领域模型与结构化契约。

设计原则：
- 统一采用 Pydantic v2 与 Literal + 映射表（不新增 Enum 类）。
- 风险等级采用单调不降约束：LLM 只可抬高重要性等级，绝不允许低于确定性下限。
- 多视角 Reviewer 短期记忆、FixDirective 主题契约、QA Checker 监督报告与本地向量记忆均有严格 Schema。
"""
from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ImportanceLevel = Literal["low", "medium", "critical"]
ReviewerRole = Literal[
    "review_logic",
    "review_security",
    "review_architecture",
    "review_tests",
]

REVIEWER_ROLES: tuple[ReviewerRole, ...] = (
    "review_logic",
    "review_security",
    "review_architecture",
    "review_tests",
)

REVIEWER_LABELS: dict[str, str] = {
    "review_logic": "逻辑错误与边界处理",
    "review_security": "安全规范与漏检",
    "review_architecture": "项目规范与架构设计",
    "review_tests": "项目测试与回归覆盖",
}

IMPORTANCE_RANK: dict[str, int] = {"low": 0, "medium": 1, "critical": 2}
IMPORTANCE_TO_TIER: dict[str, str] = {"low": "T0", "medium": "T1", "critical": "T2"}
TIER_TO_IMPORTANCE: dict[str, ImportanceLevel] = {
    "T0": "low",
    "T1": "medium",
    "T2": "critical",
}
IMPORTANCE_APPROVALS: dict[str, int] = {"low": 0, "medium": 1, "critical": 2}


def normalize_importance(value: str | None, *, default: ImportanceLevel = "medium") -> ImportanceLevel:
    """将 low/medium/critical 或 T0/T1/T2 归一化为标准 ImportanceLevel。"""
    raw = str(value or "").strip().lower()
    if raw in IMPORTANCE_RANK:
        return raw  # type: ignore[return-value]
    upper = raw.upper()
    if upper in TIER_TO_IMPORTANCE:
        return TIER_TO_IMPORTANCE[upper]
    return default


def max_importance(*levels: str | None) -> ImportanceLevel:
    """取多个等级中的最高风险级别（单调只升不降）。"""
    winner: ImportanceLevel = "low"
    for item in levels:
        if not item:
            continue
        norm = normalize_importance(item, default="low")
        if IMPORTANCE_RANK[norm] > IMPORTANCE_RANK[winner]:
            winner = norm
    return winner


def stable_content_hash(*parts: object) -> str:
    """生成稳定的 SHA-256 摘要，用于幂等去重。"""
    joined = "|".join(str(p or "") for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


class CodePartition(BaseModel):
    """PR 变更文件的细粒度分类划分。"""

    model_config = ConfigDict(extra="ignore")

    source_files: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    config_dep_files: list[str] = Field(default_factory=list)
    doc_files: list[str] = Field(default_factory=list)
    ci_infra_files: list[str] = Field(default_factory=list)
    other_files: list[str] = Field(default_factory=list)


class PRBrief(BaseModel):
    """Node 2 (pr_reader) 的结构化输出：PR 背景、前后行为对比与单调风险等级。"""

    model_config = ConfigDict(extra="ignore")

    problem_summary: str = ""
    change_intent: str = ""
    before_behavior: str = ""
    after_behavior: str = ""
    background_context: str = ""
    code_partition: CodePartition = Field(default_factory=CodePartition)
    deterministic_level: ImportanceLevel = "medium"
    llm_suggested_level: ImportanceLevel = "medium"
    final_level: ImportanceLevel = "medium"
    escalation_reasons: list[str] = Field(default_factory=list)
    affected_modules: list[str] = Field(default_factory=list)
    risk_highlights: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_monotonic_level(self) -> "PRBrief":
        self.final_level = max_importance(self.deterministic_level, self.llm_suggested_level)
        return self


class ReviewerMemory(BaseModel):
    """单个视角 Reviewer Agent 持有的独立短期记忆，防止跨轮次重复提问。"""

    model_config = ConfigDict(extra="ignore")

    role: ReviewerRole
    rounds_completed: int = 0
    raised_fingerprints: list[str] = Field(default_factory=list)
    resolved_fingerprints: list[str] = Field(default_factory=list)
    dismissed_fingerprints: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PerspectiveReviewOutput(BaseModel):
    """Node 3 四个并行视角 Reviewer 的统一结构化输出。"""

    model_config = ConfigDict(extra="ignore")

    role: ReviewerRole
    needs_modification: bool = False
    summary: str = ""
    findings: list[dict[str, Any]] = Field(default_factory=list)
    memory_notes: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class FixDirective(BaseModel):
    """Node 4 综合产出并同时交付给 Coder 与 QA Checker 的不可变修复契约。"""

    model_config = ConfigDict(extra="ignore")

    directive_id: str = ""
    round_no: int = 1
    needs_modification: bool = False
    target_findings: list[dict[str, Any]] = Field(default_factory=list)
    non_fixable_blockers: list[dict[str, Any]] = Field(default_factory=list)
    allowed_source_files: list[str] = Field(default_factory=list)
    allowed_test_addition_files: list[str] = Field(default_factory=list)
    forbidden_prefixes: list[str] = Field(
        default_factory=lambda: [".github/workflows/", ".codeguard/"],
    )
    acceptance_criteria: list[str] = Field(default_factory=list)
    summary: str = ""


class FixExecutionResult(BaseModel):
    """Node 5 (coder_agent) 的结构化修复产出。"""

    model_config = ConfigDict(extra="ignore")

    round_no: int = 1
    addressed_finding_ids: list[str] = Field(default_factory=list)
    unfixable_finding_ids: list[str] = Field(default_factory=list)
    patch: str = ""
    modified_files: list[str] = Field(default_factory=list)
    explanation: str = ""
    rag_memory_ids_used: list[str] = Field(default_factory=list)
    self_check_passed: bool = False
    tree_hash: str = ""


class QAItemCheck(BaseModel):
    """QA Checker 对单条 FixDirective 问题项的对账结论。"""

    model_config = ConfigDict(extra="ignore")

    finding_id: str
    addressed: bool = False
    aligned: bool = True
    note: str = ""


class QACheckReport(BaseModel):
    """Node 6 (qa_checker) 的结构化一致性与防偏航监督报告。"""

    model_config = ConfigDict(extra="ignore")

    verdict: Literal["pass", "revise", "escalate"] = "pass"
    is_aligned: bool = True
    has_scope_drift: bool = False
    alignment_score: float = Field(default=1.0, ge=0.0, le=1.0)
    item_checks: list[QAItemCheck] = Field(default_factory=list)
    drift_details: list[str] = Field(default_factory=list)
    redirect_instructions: list[str] = Field(default_factory=list)
    summary: str = ""

    @model_validator(mode="after")
    def _enforce_pass_invariants(self) -> "QACheckReport":
        # 若检测到偏移或未对齐，不允许保留 pass 结论，确定性降级为 revise
        if self.verdict == "pass" and (not self.is_aligned or self.has_scope_drift):
            self.verdict = "revise"
        return self


class ReviewSummary(BaseModel):
    """Node 7 (summary_and_submit) 的结构化 PR 提交与证据摘要。"""

    model_config = ConfigDict(extra="ignore")

    headline: str = ""
    inherited_level: ImportanceLevel = "medium"
    effective_level: ImportanceLevel = "medium"
    level_escalated: bool = False
    changes_made: list[str] = Field(default_factory=list)
    verification_evidence: list[str] = Field(default_factory=list)
    residual_risks: list[str] = Field(default_factory=list)
    submitted_mode: Literal["none", "push_commit", "new_pr"] = "none"
    submitted_ref: str = ""
    submitted_pr_number: int | None = None
    submitted_sha: str = ""
    markdown_body: str = ""

    @model_validator(mode="after")
    def _enforce_inherited_floor(self) -> "ReviewSummary":
        merged = max_importance(self.inherited_level, self.effective_level)
        if IMPORTANCE_RANK[merged] > IMPORTANCE_RANK[self.inherited_level]:
            self.level_escalated = True
        self.effective_level = merged
        return self


class ProceduralMemoryRecord(BaseModel):
    """存入本地向量库的程序化记忆条目（同时保存语义文本与结构化元数据）。"""

    model_config = ConfigDict(extra="ignore")

    memory_id: str = ""
    content_hash: str = ""
    repo: str = ""
    pr_number: int = 0
    importance_level: ImportanceLevel = "medium"
    reviewer_roles: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    file_patterns: list[str] = Field(default_factory=list)
    problem_pattern: str = ""
    fix_strategy: str = ""
    patch_summary: str = ""
    qa_verdict: str = ""
    ci_passed: bool = False
    human_approved: bool | None = None
    final_outcome: str = ""
    memory_type: Literal["fix_pattern", "anti_pattern", "review_lesson"] = "review_lesson"
    content_text: str = ""
    created_at: str = ""

    def model_post_init(self, __context: Any) -> None:
        if not self.content_text:
            text_parts = [
                f"repo={self.repo}",
                f"type={self.memory_type}",
                f"categories={','.join(self.categories)}",
                f"files={','.join(self.file_patterns)}",
                f"problem={self.problem_pattern}",
                f"fix={self.fix_strategy}",
                f"patch={self.patch_summary}",
                f"outcome={self.final_outcome}",
            ]
            object.__setattr__(self, "content_text", "\n".join(text_parts))
        if not self.content_hash:
            digest = stable_content_hash(
                self.repo,
                self.memory_type,
                self.problem_pattern,
                self.fix_strategy,
                self.patch_summary,
                self.final_outcome,
            )
            object.__setattr__(self, "content_hash", digest)
        if not self.memory_id:
            object.__setattr__(self, "memory_id", f"mem_{self.content_hash[:16]}")


__all__ = [
    "ImportanceLevel",
    "ReviewerRole",
    "REVIEWER_ROLES",
    "REVIEWER_LABELS",
    "IMPORTANCE_RANK",
    "IMPORTANCE_TO_TIER",
    "TIER_TO_IMPORTANCE",
    "IMPORTANCE_APPROVALS",
    "normalize_importance",
    "max_importance",
    "stable_content_hash",
    "CodePartition",
    "PRBrief",
    "ReviewerMemory",
    "PerspectiveReviewOutput",
    "FixDirective",
    "FixExecutionResult",
    "QAItemCheck",
    "QACheckReport",
    "ReviewSummary",
    "ProceduralMemoryRecord",
]
