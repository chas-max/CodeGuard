"""评测平台契约：TaskSpec / Observation / ScoreRecord / DatasetEntry / RunMeta。

三层解耦：
- 适配层只产出 `TaskSpec`；
- 执行层只产出 `Observation`；
- 评分层只消费 `ScoreRecord`（以及 registry 的权重）。
任何一层都不需要知道另外两层的实现细节。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

CAPABILITIES = ("F1", "F2", "F3", "F4", "F5", "F6")
CAPABILITY_NAMES = {
    "F1": "PR 理解与变更分析",
    "F2": "缺陷检测与定位",
    "F3": "代码审查质量",
    "F4": "自动代码修复",
    "F5": "自动测试与回归",
    "F6": "Agent 工作流与工具",
}
# 功能层权重：F1 是前置诊断维，不占综合分权重，不达标时不输出主分。
CAPABILITY_WEIGHTS = {"F2": 0.25, "F3": 0.25, "F4": 0.25, "F5": 0.15, "F6": 0.10}
DIAGNOSTIC_CAPABILITIES = ("F1",)
# 三轨口径：主分只接受外部已核准数据集；自建集只做补充分；诊断独立报告。
ROLES = ("primary", "supplementary", "diagnostic")
DEFAULT_ROLE = "supplementary"
# 主分要求维度：F1 是门禁维（不占权重），F6 单独作为 S_integration。
PRIMARY_REQUIRED = ("F1", "F2", "F3", "F4", "F5")
PRIMARY_WEIGHTED = ("F2", "F3", "F4", "F5")
INTEGRATION = ("F6",)
SUPPLEMENTARY_WEIGHTED = ("F2", "F3", "F5")
# 外部主分要能输出时的目标样本量（不足即 small_sample）。
COVERAGE_TARGETS = {"F1": 10, "F2": 20, "F3": 20, "F4": 20, "F5": 20, "F6": 10}
OBSERVATION_STATUSES = ("ok", "skipped", "error")
DATASET_STATUSES = ("candidate", "approved", "quarantined", "rejected")
QUALITY_GRADES = ("A", "B", "C")
GRADE_WEIGHTS = {"A": 1.0, "B": 0.7, "C": 0.0}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_UNKNOWN_LICENSES = {"", "unknown", "n/a", "none", "unlicensed"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def license_is_clear(license_text: str) -> bool:
    return str(license_text or "").strip().lower() not in _UNKNOWN_LICENSES


@dataclass(frozen=True)
class DatasetEntry:
    """数据集登记项。capabilities 的值是该数据集对某能力的覆盖权重（0..1）。

    `role` 决定该数据集进入哪条轨道：`primary`（外部主分）、`supplementary`
    （自建补充分）、`diagnostic`（诊断）。缺省 `supplementary`，避免旧 registry
    因为新增字段突然进入主分。
    """

    name: str
    adapter: str
    kind: str
    license: str
    status: str
    quality_grade: str
    capabilities: dict[str, float] = field(default_factory=dict)
    primary_metrics: dict[str, str] = field(default_factory=dict)
    snapshot_required: bool = False
    snapshot_root: str = "snapshots"
    image: str = ""
    test_command: str = "python -m pytest -q"
    instances_file: str = ""
    manifest_file: str = ""
    fixtures: str = ""
    repo_url_template: str = "https://github.com/{repo}.git"
    size: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    verification: str = "pending"
    role: str = DEFAULT_ROLE

    def __post_init__(self) -> None:
        _require(bool(_NAME_RE.match(self.name or "")), f"非法数据集名: {self.name!r}")
        _require(bool(self.adapter), f"{self.name}: 缺少 adapter")
        _require(self.kind in ("core", "external"), f"{self.name}: kind 必须是 core/external")
        _require(self.role in ROLES, f"{self.name}: 未知 role {self.role!r}")
        _require(self.status in DATASET_STATUSES, f"{self.name}: 未知 status {self.status!r}")
        _require(self.quality_grade in QUALITY_GRADES,
                 f"{self.name}: 未知 quality_grade {self.quality_grade!r}")
        _require(isinstance(self.capabilities, dict), f"{self.name}: capabilities 必须是对象")
        for cap, weight in self.capabilities.items():
            _require(cap in CAPABILITIES, f"{self.name}: 未知能力 {cap!r}")
            _require(0.0 <= float(weight) <= 1.0,
                     f"{self.name}: {cap} 覆盖权重必须在 0..1")
        for cap, metric in self.primary_metrics.items():
            _require(cap in CAPABILITIES, f"{self.name}: 主指标引用了未知能力 {cap!r}")
            _require(bool(str(metric).strip()), f"{self.name}: {cap} 主指标为空")

    @property
    def grade_weight(self) -> float:
        return GRADE_WEIGHTS[self.quality_grade]

    @property
    def is_scorable(self) -> bool:
        """只有已核准、非 C 级、许可证清晰的数据集进入功能层分母。"""
        return (
            self.status == "approved"
            and self.grade_weight > 0
            and (license_is_clear(self.license) or self.kind == "core")
        )

    @property
    def is_primary(self) -> bool:
        """外部主分：role=primary + approved + 许可证清晰 + 非 C 级。"""
        return (
            self.role == "primary"
            and self.status == "approved"
            and self.grade_weight > 0
            and license_is_clear(self.license)
        )

    @property
    def is_supplementary(self) -> bool:
        """自建补充分：role=supplementary 且可评分。"""
        return self.role == "supplementary" and self.is_scorable

    @property
    def is_diagnostic(self) -> bool:
        """诊断轨：F1 探针与 F6 工作流，不进入主分。"""
        return self.role == "diagnostic"

    def coverage(self, capability: str) -> float:
        return float(self.capabilities.get(capability, 0.0)) if self.is_scorable else 0.0

    def primary_metric(self, capability: str) -> str:
        return str(self.primary_metrics.get(capability, ""))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TaskSpec:
    """统一任务。`input`/`expected` 是适配器私有载荷，执行层按 `source_layer` 分派。"""

    task_id: str
    dataset: str
    capabilities: tuple[str, ...]
    identity: dict[str, str]
    input: dict[str, Any]
    expected: dict[str, Any]
    env: dict[str, Any]
    license: str
    quality_grade: str
    source_layer: str = ""
    sample_uid: str = ""
    role: str = DEFAULT_ROLE

    def __post_init__(self) -> None:
        _require(bool(str(self.task_id).strip()), "TaskSpec.task_id 不能为空")
        _require(bool(str(self.dataset).strip()), "TaskSpec.dataset 不能为空")
        caps = tuple(self.capabilities)
        _require(bool(caps), f"{self.task_id}: capabilities 不能为空")
        _require(len(set(caps)) == len(caps), f"{self.task_id}: capabilities 有重复")
        for cap in caps:
            _require(cap in CAPABILITIES, f"{self.task_id}: 未知能力 {cap!r}")
        _require(self.role in ROLES, f"{self.task_id}: 未知 role {self.role!r}")
        _require(self.quality_grade in QUALITY_GRADES,
                 f"{self.task_id}: 未知 quality_grade {self.quality_grade!r}")
        for name, value in (
            ("identity", self.identity), ("input", self.input),
            ("expected", self.expected), ("env", self.env),
        ):
            _require(isinstance(value, dict), f"{self.task_id}: {name} 必须是对象")

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["capabilities"] = list(self.capabilities)
        return payload

    @classmethod
    def from_dict(cls, raw: dict) -> "TaskSpec":
        data = dict(raw)
        data["capabilities"] = tuple(data.get("capabilities") or ())
        return cls(**data)


@dataclass(frozen=True)
class Observation:
    task_id: str
    dataset: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    cost_cny: float = 0.0
    wall_ms: int = 0
    sample_uid: str = ""
    skip_reason: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        _require(self.status in OBSERVATION_STATUSES,
                 f"{self.task_id}: 未知 observation status {self.status!r}")
        _require(isinstance(self.payload, dict), f"{self.task_id}: payload 必须是对象")
        _require(self.cost_cny >= 0, f"{self.task_id}: cost_cny 不能为负")
        _require(self.wall_ms >= 0, f"{self.task_id}: wall_ms 不能为负")
        for cap in self.capabilities:
            _require(cap in CAPABILITIES, f"{self.task_id}: 未知能力 {cap!r}")

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["capabilities"] = list(self.capabilities)
        return payload

    @classmethod
    def from_dict(cls, raw: dict) -> "Observation":
        data = dict(raw)
        data["capabilities"] = tuple(data.get("capabilities") or ())
        return cls(**data)


@dataclass(frozen=True)
class ScoreRecord:
    """一条可计分记录。`value` 已归一化到 0..1，聚合层只做加权。"""

    task_id: str
    dataset: str
    capability: str
    metric: str
    numerator: float
    denominator: float
    value: float
    sample_uid: str = ""
    sample_size: int = 1
    interval: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        _require(self.capability in CAPABILITIES,
                 f"{self.task_id}: 未知能力 {self.capability!r}")
        _require(bool(str(self.metric).strip()), f"{self.task_id}: metric 不能为空")
        _require(self.numerator >= 0, f"{self.task_id}: numerator 不能为负")
        _require(self.denominator >= 0, f"{self.task_id}: denominator 不能为负")
        _require(0.0 <= float(self.value) <= 1.0,
                 f"{self.task_id}: value 必须在 0..1（当前 {self.value}）")
        _require(self.sample_size >= 0, f"{self.task_id}: sample_size 不能为负")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunMeta:
    run_id: str
    started_at: str
    git_sha: str = ""
    registry_sha256: str = ""
    models: dict[str, str] = field(default_factory=dict)
    repeat: int = 1
    offline: bool = True
    budget_cny: float = 0.0
    notes: str = ""
    profile: str = ""
    patch_source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


__all__ = [
    "CAPABILITIES", "CAPABILITY_NAMES", "CAPABILITY_WEIGHTS", "DIAGNOSTIC_CAPABILITIES",
    "ROLES", "DEFAULT_ROLE", "PRIMARY_REQUIRED", "PRIMARY_WEIGHTED", "INTEGRATION",
    "SUPPLEMENTARY_WEIGHTED", "COVERAGE_TARGETS",
    "OBSERVATION_STATUSES", "DATASET_STATUSES",
    "QUALITY_GRADES", "GRADE_WEIGHTS", "license_is_clear",
    "DatasetEntry", "TaskSpec", "Observation", "ScoreRecord", "RunMeta",
]
